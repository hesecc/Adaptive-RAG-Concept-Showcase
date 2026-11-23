"""Tenant-serialized document management shared by both knowledge stores."""

from datetime import datetime, timedelta, timezone
from itertools import groupby
import json
from uuid import uuid4

from .config import Principal
from .domain import ConflictError, ForbiddenError, NotFoundError, ValidationError, identifier
from .mineru_formats import ORIGINAL_MIME


def _source_extension(meta):
    if not meta.get('source_object_key'):
        return None
    parser = meta.get('source_parser') or ''
    if parser.startswith('mineru:'):
        ext = parser.partition(':')[2]
        if ext not in ORIGINAL_MIME:
            raise ValidationError('unknown archived document format')
        return ext
    return 'pdf'  # Prior releases archived PDF originals only.


LIFECYCLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_access_epochs (
    tenant_id TEXT PRIMARY KEY, epoch BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS retired_document_ids (
    tenant_id TEXT NOT NULL, document_id TEXT NOT NULL, PRIMARY KEY (tenant_id,document_id)
);
CREATE TABLE IF NOT EXISTS document_events (
    tenant_id TEXT NOT NULL, document_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
    action TEXT NOT NULL, actor_id TEXT, created_at TEXT NOT NULL, revision_id TEXT,
    PRIMARY KEY (tenant_id, document_id, ordinal),
    FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, document_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS document_trash (
    tenant_id TEXT NOT NULL, document_id TEXT NOT NULL, trash_id TEXT NOT NULL,
    deleted_by TEXT, deleted_at TEXT NOT NULL, purge_after TEXT NOT NULL,
    admin_hold INTEGER NOT NULL CHECK (admin_hold IN (0,1)), published_revision_id TEXT,
    PRIMARY KEY (tenant_id, document_id), UNIQUE (tenant_id, trash_id),
    FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, document_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS document_trash_by_expiry ON document_trash (purge_after);
CREATE VIEW IF NOT EXISTS active_documents AS
    SELECT d.tenant_id,d.document_id,d.allowed_subjects,d.data_domains,d.revoked FROM documents d
    WHERE NOT EXISTS (SELECT 1 FROM document_trash t WHERE t.tenant_id=d.tenant_id AND t.document_id=d.document_id);
"""


def _now():
    return datetime.now(timezone.utc)


def _principal(value, tenant=None):
    if not isinstance(value, Principal):
        raise ForbiddenError("authenticated principal required")
    if tenant is not None and value.tenant_id != tenant:
        raise ForbiddenError("document belongs to another tenant")
    return value


class DocumentLifecycle:
    @staticmethod
    def _admin_actor(principal, tenant):
        if principal is not None and not _principal(principal, tenant).admin:
            raise ForbiddenError("administrator required")
        return principal.subject_id if principal else None

    def _event(self, conn, tenant, doc, action, actor, revision=None):
        if action in {"set_access", "trash", "restore"}:
            conn.execute(self._sql("INSERT INTO document_access_epochs VALUES (?,1) ON CONFLICT(tenant_id) "
                                   "DO UPDATE SET epoch=document_access_epochs.epoch+1"), (tenant,))
        conn.execute(self._sql("INSERT INTO document_events(tenant_id,document_id,ordinal,action,actor_id,created_at,revision_id) "
            "SELECT ?,?,COALESCE(MAX(ordinal),0)+1,?,?,?,? FROM document_events WHERE tenant_id=? AND document_id=?"),
            (tenant, doc, action, actor, _now().isoformat(), revision, tenant, doc))

    def access_epoch(self, tenant):
        with self._snapshot_read() as conn:
            row = conn.execute(self._sql("SELECT epoch FROM document_access_epochs WHERE tenant_id=?"), (tenant,)).fetchone()
            return row["epoch"] if row else 0

    def check_stage_owner(self, principal, doc):
        with self._snapshot_read() as conn:
            self._stage_owner(conn, _principal(principal).tenant_id, identifier(doc, 'document_id'), principal)

    def _stage_owner(self, conn, tenant, doc, principal):
        if principal is not None:
            _principal(principal, tenant)
        row = conn.execute(self._sql(
            "SELECT d.owner_id,t.trash_id FROM documents d LEFT JOIN document_trash t "
            "USING(tenant_id,document_id) WHERE d.tenant_id=? AND d.document_id=?"), (tenant, doc)).fetchone()
        if row:
            if principal is not None and not principal.admin and row["owner_id"] != principal.subject_id:
                raise ConflictError("document identifier is unavailable")
            if row["trash_id"]:
                raise ConflictError("restore document before staging")
        elif conn.execute(self._sql("SELECT 1 FROM retired_document_ids WHERE tenant_id=? AND document_id=?"),
                          (tenant, doc)).fetchone():
            raise ConflictError("document identifier is unavailable")
        return principal.subject_id if principal else None

    def _managed_rows(self, conn, principal, *, doc=None, view=None, detail=False):
        tenant = _principal(principal).tenant_id
        filters, params = ["d.tenant_id=?"], [tenant]
        if not principal.admin or view == "mine":
            filters.append("d.owner_id=?")
            params.append(principal.subject_id)
        if doc is not None:
            filters.append("d.document_id=?")
            params.append(identifier(doc, "document_id"))
        if view is not None:
            filters.append("t.trash_id IS NOT NULL" if view == "trash" else "t.trash_id IS NULL")
        generation = self._current(conn, tenant)
        rows = conn.execute(self._sql(
            "SELECT d.document_id,d.owner_id,d.revoked,d.allowed_subjects,d.data_domains,t.trash_id,t.deleted_at,t.deleted_by,t.purge_after,"
            "t.admin_hold,t.published_revision_id,r.version,r.revision_id,r.metadata_json,r.created_at,j.state,j.job_id,"
            "e.created_at AS updated_at,e.actor_id AS updated_by,"
            "s.revision_id AS current_revision_id FROM documents d JOIN revisions r USING(tenant_id,document_id) "
            "JOIN jobs j USING(tenant_id,document_id,revision_id) LEFT JOIN document_trash t USING(tenant_id,document_id) "
            "LEFT JOIN snapshot_revisions s ON s.tenant_id=d.tenant_id AND s.document_id=d.document_id AND s.generation=? "
            "LEFT JOIN document_events e ON e.tenant_id=d.tenant_id AND e.document_id=d.document_id AND e.ordinal="
            "(SELECT MAX(last.ordinal) FROM document_events last WHERE last.tenant_id=d.tenant_id AND last.document_id=d.document_id) "
            "WHERE " + " AND ".join(filters) + " ORDER BY d.document_id,r.created_at DESC,r.revision_id DESC"),
            (generation, *params))
        now = _now()
        result = []
        for doc_id, group in groupby(rows, lambda r: r["document_id"]):
            history = list(group)
            selected = history[0]["current_revision_id"] or history[0]["published_revision_id"]
            row = next((r for r in history if r["revision_id"] == selected), history[0])
            trash = row["trash_id"] is not None
            item = {k: row[k] for k in ("owner_id", "version", "revision_id", "trash_id", "deleted_by",
                                         "deleted_at", "purge_after", "created_at")}
            item.update(document_id=doc_id, title=json.loads(row["metadata_json"])["title"], generation=generation,
                        state="trash" if trash else "active", revoked=bool(row["revoked"]),
                        published=row["current_revision_id"] is not None, admin_hold=bool(row["admin_hold"]),
                        can_restore=trash and now < datetime.fromisoformat(row["purge_after"]) and
                        (principal.admin or (not row["admin_hold"] and row["deleted_by"] == principal.subject_id)))
            item.update(kind="document", id=doc_id, source_extension=_source_extension(json.loads(row['metadata_json'])),
                        updated_at=row["updated_at"] or history[0]["created_at"],
                        updated_by=row["updated_by"], allowed_subjects=json.loads(row["allowed_subjects"]),
                        data_domains=json.loads(row["data_domains"]),
                        status=("trashed" if trash else "revoked" if item["revoked"] else
                                "published" if item["published"] else "staged"),
                        staged_jobs=[] if trash else [{k: r[k] for k in ("job_id", "version", "revision_id", "created_at")}
                                                      for r in history if r["state"] == "staged"])
            if detail:
                item["events"] = [dict(e) for e in conn.execute(self._sql(
                    "SELECT action,actor_id,created_at,revision_id FROM document_events "
                    "WHERE tenant_id=? AND document_id=? ORDER BY ordinal DESC"), (tenant, doc_id))]
                authors = {e["revision_id"]: e["actor_id"] for e in item["events"] if e["action"] == "stage"}
                item["versions"] = [{"revision_id": r["revision_id"], "version": r["version"], "state": r["state"],
                                     "created_by": authors.get(r["revision_id"]),
                                     "source_extension": _source_extension(json.loads(r['metadata_json'])),
                                     "created_at": r["created_at"], "title": json.loads(r["metadata_json"])["title"]}
                                    for r in history]
            result.append(item)
        return result

    def _managed_one(self, conn, principal, doc, *, detail=False):
        identifier(doc, "document_id")
        rows = self._managed_rows(conn, principal, doc=doc, detail=detail)
        if not rows:
            raise NotFoundError("document not found")
        return rows[0]

    def managed_documents(self, principal, view="mine"):
        if view not in ("all", "mine", "trash"):
            raise ValidationError("view must be all, mine or trash")
        if view == "all" and not _principal(principal).admin:
            raise ForbiddenError("administrator required for all documents")
        with self._snapshot_read() as conn:
            return self._managed_rows(conn, principal, view=view)

    def managed_document(self, principal, doc_id):
        with self._snapshot_read() as conn:
            return self._managed_one(conn, principal, doc_id, detail=True)

    def _own_unclassified_draft(self, conn, principal, doc, revision, policy):
        # Only a newly uploaded, unclassified draft. A policy change or any published
        # revision permanently ends this exception, even after rollback/unrevocation.
        if policy["revoked"] or json.loads(policy["allowed_subjects"]) or json.loads(policy["data_domains"]):
            return False
        return conn.execute(self._sql(
            "SELECT 1 FROM document_events e WHERE e.tenant_id=? AND e.document_id=? "
            "AND e.action='stage' AND e.actor_id=? AND e.revision_id=? "
            "AND NOT EXISTS (SELECT 1 FROM document_events a WHERE a.tenant_id=e.tenant_id "
            "AND a.document_id=e.document_id AND a.action='set_access') "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.tenant_id=e.tenant_id "
            "AND j.document_id=e.document_id AND j.state='published')"),
            (principal.tenant_id, doc, principal.subject_id, revision)).fetchone() is not None

    def document_source(self, principal, doc_id, revision=None, *, original=False):
        with self._snapshot_read() as conn:
            tenant = _principal(principal).tenant_id
            identifier(doc_id, "document_id")
            policy = conn.execute(self._sql("SELECT d.owner_id,a.revoked,a.allowed_subjects,a.data_domains "
                "FROM active_documents a JOIN documents d USING(tenant_id,document_id) WHERE a.tenant_id=? AND a.document_id=?"),
                (tenant, doc_id)).fetchone()
            if not policy or (not principal.admin and policy["owner_id"] != principal.subject_id):
                raise NotFoundError("document source not available")
            if revision is None:
                revision = self._managed_one(conn, principal, doc_id)["revision_id"]
            else:
                identifier(revision, "revision_id")
            if (not principal.admin and not self._allowed(policy, principal.access) and
                    not self._own_unclassified_draft(conn, principal, doc_id, revision, policy)):
                raise NotFoundError("document source not available")
            row = conn.execute(self._sql("SELECT raw_key,metadata_json FROM revisions WHERE "
                "tenant_id=? AND document_id=? AND revision_id=?"), (tenant, doc_id, revision)).fetchone()
            if not row:
                raise NotFoundError("document revision not found")
            meta = json.loads(row["metadata_json"])
            if original and meta.get("source_object_key"):
                return meta["source_object_key"], ORIGINAL_MIME[_source_extension(meta)], revision
            return row["raw_key"], meta["media_type"], revision

    def _trash(self, conn, tenant, doc, expected, *, actor, admin, published_only=False):
        current = self._compare_generation(conn, tenant, expected)
        revisions = dict(self._snapshot(conn, tenant, current).revisions) if current else {}
        if published_only and doc not in revisions:
            raise NotFoundError("document is not in the current snapshot")
        previous = revisions.pop(doc, None)
        now = _now()
        # Outlives physical purge. A restored document still exists and may be updated normally.
        conn.execute(self._sql("INSERT INTO retired_document_ids VALUES (?,?) ON CONFLICT DO NOTHING"), (tenant, doc))
        self._event(conn, tenant, doc, "trash", actor, previous)
        conn.execute(self._sql("INSERT INTO document_trash(tenant_id,document_id,trash_id,deleted_by,deleted_at,"
            "purge_after,admin_hold,published_revision_id) VALUES (?,?,?,?,?,?,?,?)"),
            (tenant, doc, uuid4().hex, actor, now.isoformat(), (now + timedelta(hours=168)).isoformat(), int(admin), previous))
        return self._create_snapshot(conn, tenant, revisions)

    def trash_document(self, principal, doc_id, expected_generation):
        tenant = _principal(principal).tenant_id
        with self._snapshot_write(tenant) as conn:
            item = self._managed_one(conn, principal, doc_id)
            if item["trash_id"]:
                raise ConflictError("document already trashed")
            self._trash(conn, tenant, doc_id, expected_generation, actor=principal.subject_id, admin=principal.admin)
            return self._managed_one(conn, principal, doc_id)

    def delete_document(self, tenant_id, document_id, expected_generation, *, principal=None):
        identifier(tenant_id, "tenant_id")
        identifier(document_id, "document_id")
        actor = self._admin_actor(principal, tenant_id)
        with self._snapshot_write(tenant_id) as conn:
            return self._trash(conn, tenant_id, document_id, expected_generation, published_only=True,
                               actor=actor, admin=True)

    def restore_document(self, principal, doc_id, trash_id, expected_generation):
        tenant = _principal(principal).tenant_id
        identifier(trash_id, "trash_id")
        with self._snapshot_write(tenant) as conn:
            item = self._managed_one(conn, principal, doc_id)
            if item["trash_id"] != trash_id:
                raise ConflictError("trash id no longer current")
            if not principal.admin and (item["admin_hold"] or item["deleted_by"] != principal.subject_id):
                raise ForbiddenError("document requires administrator restore")
            if not item["can_restore"]:
                raise ConflictError("restore period expired")
            current = self._compare_generation(conn, tenant, expected_generation)
            revisions = dict(self._snapshot(conn, tenant, current).revisions) if current else {}
            previous = conn.execute(self._sql("SELECT published_revision_id FROM document_trash "
                "WHERE tenant_id=? AND document_id=?"), (tenant, doc_id)).fetchone()["published_revision_id"]
            if previous is not None:
                revisions[doc_id] = previous
            conn.execute(self._sql("DELETE FROM document_trash WHERE tenant_id=? AND document_id=? AND trash_id=?"),
                         (tenant, doc_id, trash_id))
            self._event(conn, tenant, doc_id, "restore", principal.subject_id, previous)
            self._create_snapshot(conn, tenant, revisions)
            return self._managed_one(conn, principal, doc_id)
