"""PostgreSQL adapters preserving immutable revisions, CAS, and live access.

Credentials are supplied explicitly, never discovered from files. Every operation
owns a connection and a transaction. The schema argument isolates test/migration
targets and is an identifier, never interpolated unquoted into SQL.
"""

from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Sequence

from .domain import (
    AccessContext, CapabilityError, ChildChunk, ConflictError, ForbiddenError,
    IngestResult, NotFoundError, ParentChunk, PreparedDocument, RagError,
    Snapshot, ValidationError, identifier, stable_id,
)
from .config import Principal
from .document_lifecycle import LIFECYCLE_SCHEMA
from .data_access import domains_allowed, normalize_data_domains
from .trace_history import STORAGE_VERSION, TraceHistory, backfill_history, index_trace, initialize_history, with_conversation
from .storage import (
    SQLiteKnowledgeStore, SQLiteTraceSink, _CANDIDATE_SCHEMA, _SnapshotStore,
    _document_listing, _json,
)


def validate_prepared_document(document: PreparedDocument) -> None:
    """Use the existing storage contract's pure validation for both backends."""
    SQLiteKnowledgeStore._validate(document)


_TABLES = (
    "documents", "revisions", "parents", "children", "jobs", "snapshots",
    "snapshot_revisions", "current_snapshots", "traces", "candidate_snapshots", "candidate_revisions", "candidate_jobs",
    "document_trash", "document_events", "retired_document_ids",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_metadata (
    id INTEGER PRIMARY KEY CHECK (id=1), version INTEGER NOT NULL
);
INSERT INTO schema_metadata VALUES (1, 1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS documents (
    tenant_id TEXT NOT NULL, document_id TEXT NOT NULL,
    allowed_subjects TEXT NOT NULL, revoked INTEGER NOT NULL CHECK (revoked IN (0,1)),
    data_domains TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (tenant_id, document_id)
);
CREATE TABLE IF NOT EXISTS revisions (
    tenant_id TEXT NOT NULL, document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
    version TEXT NOT NULL, processing_fingerprint TEXT NOT NULL, content_hash TEXT NOT NULL,
    raw_key TEXT NOT NULL, metadata_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, document_id, revision_id),
    FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, document_id)
);
CREATE TABLE IF NOT EXISTS parents (
    tenant_id TEXT NOT NULL, document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
    parent_id TEXT NOT NULL, value_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, document_id, revision_id, parent_id),
    UNIQUE (tenant_id, parent_id),
    FOREIGN KEY (tenant_id, document_id, revision_id)
        REFERENCES revisions (tenant_id, document_id, revision_id)
);
CREATE TABLE IF NOT EXISTS children (
    tenant_id TEXT NOT NULL, document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL, parent_id TEXT NOT NULL, value_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, document_id, revision_id, chunk_id),
    UNIQUE (tenant_id, chunk_id),
    FOREIGN KEY (tenant_id, document_id, revision_id, parent_id)
        REFERENCES parents (tenant_id, document_id, revision_id, parent_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    tenant_id TEXT NOT NULL, job_id TEXT NOT NULL, document_id TEXT NOT NULL,
    revision_id TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('staged','published')),
    parent_count INTEGER NOT NULL, child_count INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, job_id),
    FOREIGN KEY (tenant_id, document_id, revision_id)
        REFERENCES revisions (tenant_id, document_id, revision_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    tenant_id TEXT NOT NULL, generation TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, generation)
);
CREATE TABLE IF NOT EXISTS snapshot_revisions (
    tenant_id TEXT NOT NULL, generation TEXT NOT NULL, document_id TEXT NOT NULL,
    revision_id TEXT NOT NULL, PRIMARY KEY (tenant_id, generation, document_id),
    FOREIGN KEY (tenant_id, generation) REFERENCES snapshots (tenant_id, generation),
    FOREIGN KEY (tenant_id, document_id, revision_id)
        REFERENCES revisions (tenant_id, document_id, revision_id)
);
CREATE TABLE IF NOT EXISTS current_snapshots (
    tenant_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
    FOREIGN KEY (tenant_id, generation) REFERENCES snapshots (tenant_id, generation)
);
CREATE TABLE IF NOT EXISTS traces (
    tenant_id TEXT NOT NULL, trace_id TEXT NOT NULL, value_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, trace_id)
);
"""


def _driver():
    try:
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
    except ImportError:
        raise CapabilityError("PostgreSQL support requires the remote optional dependency") from None
    return psycopg, sql, dict_row


def _schema_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value):
        raise ValidationError("PostgreSQL schema must be a lowercase SQL identifier")
    return value


def _lock_id(namespace: str, tenant: str) -> int:
    return int.from_bytes(sha256(f"{namespace}\0{tenant}".encode()).digest()[:8], "big", signed=True)


class _PostgresDatabase:
    def __init__(self, dsn: str, *, schema: str = "adaptive_rag", connect_timeout: int = 10):
        if not isinstance(dsn, str) or not dsn.strip() or "\x00" in dsn:
            raise ValidationError("PostgreSQL DSN must be explicitly provided")
        if type(connect_timeout) is not int or not 1 <= connect_timeout <= 120:
            raise ValidationError("PostgreSQL connect_timeout must be between 1 and 120")
        self._dsn = dsn
        self.schema = _schema_name(schema)
        self.connect_timeout = connect_timeout
        with self._connection(initialize=True) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_id(self.schema, "schema"),))
            for statement in (_SCHEMA + _CANDIDATE_SCHEMA).split(";"):
                if statement.strip():
                    connection.execute(statement)
            version = connection.execute("SELECT version FROM schema_metadata WHERE id=1").fetchone()["version"]
            if version not in (1, STORAGE_VERSION):
                raise ConflictError("unsupported PostgreSQL storage schema version")
            connection.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS data_domains TEXT NOT NULL DEFAULT '[]'")
            connection.execute("ALTER TABLE revisions ADD COLUMN IF NOT EXISTS created_at TEXT NOT NULL DEFAULT ''")
            connection.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS owner_id TEXT")
            for statement in LIFECYCLE_SCHEMA.replace("CREATE VIEW IF NOT EXISTS", "CREATE OR REPLACE VIEW").split(";"):
                if statement.strip():
                    connection.execute(statement)
            initialize_history(connection, version, postgres=True)

    def __repr__(self):
        return f"{type(self).__name__}(schema={self.schema!r}, dsn=<redacted>)"

    @contextmanager
    def _connection(self, *, initialize: bool = False, tenant: str | None = None, consistent_read: bool = False) -> Iterator[Any]:
        psycopg, sql, dict_row = _driver()
        try:
            with psycopg.connect(self._dsn, connect_timeout=self.connect_timeout, row_factory=dict_row) as connection:
                # Qualify through a transaction-local search_path; pg_catalog is
                # implicit. No public/user schema can shadow adapter relations.
                if consistent_read:
                    # Manifest verification and content selection must see the same lifecycle state.
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                elif tenant is not None or initialize:
                    # The first read after an advisory-lock wait must see the prior writer's commit.
                    connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                if initialize:
                    connection.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_id(self.schema, "schema"),))
                    connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
                connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(self.schema)))
                if tenant is not None:
                    connection.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_id(self.schema, tenant),))
                yield connection
        except RagError:
            raise
        except psycopg.IntegrityError:
            raise ConflictError("PostgreSQL record conflicts with an existing identity or relation") from None
        except psycopg.Error:
            raise CapabilityError("PostgreSQL operation failed; check database availability and configuration") from None


class PostgresKnowledgeStore(_SnapshotStore, _PostgresDatabase):
    """KnowledgeStore backed by tenant-serialized PostgreSQL write transactions."""

    def stage(self, document: PreparedDocument, *, principal: Principal | None = None) -> IngestResult:
        validate_prepared_document(document)
        request = document.request
        tenant, doc, revision = request.tenant_id, request.document_id, document.revision_id
        job_id = stable_id("ingest", tenant, doc, revision)
        canonical = asdict(document)
        canonical["request"]["sku_ids"] = sorted(set(request.sku_ids))
        canonical["request"]["allowed_subjects"] = sorted(set(request.allowed_subjects))
        if not request.data_domains:
            canonical["request"].pop("data_domains")
        canonical["parents"] = sorted(canonical["parents"], key=lambda p: p["parent_id"])
        canonical["children"] = sorted(canonical["children"], key=lambda c: c["chunk_id"])
        for child in canonical["children"]:
            child["sku_ids"] = sorted(set(child["sku_ids"]))
        payload_hash = sha256(_json(canonical).encode()).hexdigest()
        metadata = asdict(request)
        metadata.pop("content")
        with self._connection(tenant=tenant) as connection:
            owner = self._stage_owner(connection, tenant, doc, principal)
            existing = connection.execute(
                "SELECT payload_hash FROM revisions WHERE tenant_id=%s AND document_id=%s AND revision_id=%s",
                (tenant, doc, revision),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise ConflictError("revision_id already belongs to different prepared content")
                return self._ingest_result(self._job(connection, tenant, job_id), duplicate=True)
            connection.execute("INSERT INTO documents(tenant_id,document_id,allowed_subjects,revoked,data_domains,owner_id) "
                               "VALUES (%s,%s,%s,0,%s,%s) ON CONFLICT(tenant_id,document_id) DO NOTHING",
                               (tenant, doc, _json(request.allowed_subjects), _json(request.data_domains), owner))
            connection.execute("INSERT INTO revisions(tenant_id,document_id,revision_id,version,processing_fingerprint,content_hash,raw_key,metadata_json,payload_hash,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (tenant, doc, revision, request.version, document.processing_fingerprint,
                 document.content_hash, document.raw_key, _json(metadata), payload_hash, datetime.now(timezone.utc).isoformat()))
            with connection.cursor() as cursor:
                cursor.executemany("INSERT INTO parents VALUES (%s,%s,%s,%s,%s)",
                    [(tenant, doc, revision, p.parent_id, _json(asdict(p))) for p in document.parents])
                cursor.executemany("INSERT INTO children VALUES (%s,%s,%s,%s,%s,%s)",
                    [(tenant, doc, revision, c.chunk_id, c.parent_id, _json(asdict(c))) for c in document.children])
            connection.execute("INSERT INTO jobs VALUES (%s,%s,%s,%s,'staged',%s,%s)",
                (tenant, job_id, doc, revision, len(document.parents), len(document.children)))
            self._event(connection, tenant, doc, "stage", owner, revision)
            return IngestResult(job_id, revision, doc, request.version, "staged", len(document.parents), len(document.children))

    @staticmethod
    def _job(connection, tenant: str, job_id: str):
        row = connection.execute(
            "SELECT j.*, r.version FROM jobs j JOIN revisions r USING(tenant_id,document_id,revision_id) "
            "WHERE j.tenant_id=%s AND j.job_id=%s", (tenant, job_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("ingestion job not found")
        return row

    @staticmethod
    def _ingest_result(row, duplicate=False):
        return IngestResult(row["job_id"], row["revision_id"], row["document_id"], row["version"],
            row["state"], row["parent_count"], row["child_count"], duplicate)

    def get_job(self, tenant_id: str, job_id: str) -> dict[str, Any]:
        identifier(tenant_id, "tenant_id")
        identifier(job_id, "job_id")
        with self._connection() as connection:
            return dict(self._job(connection, tenant_id, job_id))

    def list_documents(self, tenant_id: str) -> list[dict[str, Any]]:
        identifier(tenant_id, "tenant_id")
        with self._connection() as connection:
            generation = self._current(connection, tenant_id)
            rows = connection.execute(
                "SELECT d.document_id,d.allowed_subjects,d.revoked,d.data_domains,r.revision_id,r.version,r.metadata_json,r.created_at,j.job_id,j.state "
                "FROM active_documents d JOIN revisions r USING(tenant_id,document_id) "
                "JOIN jobs j USING(tenant_id,document_id,revision_id) WHERE d.tenant_id=%s "
                "ORDER BY d.document_id,r.created_at DESC,r.revision_id DESC", (tenant_id,),
            ).fetchall()
            published = dict(self._snapshot(connection, tenant_id, generation).revisions) if generation else {}
            return _document_listing(rows, generation, published)

    @staticmethod
    def _current(connection, tenant: str) -> str | None:
        row = connection.execute("SELECT generation FROM current_snapshots WHERE tenant_id=%s", (tenant,)).fetchone()
        return row["generation"] if row else None

    _placeholder = "%s"

    def _snapshot_read(self):
        return self._connection(consistent_read=True)

    def _snapshot_write(self, tenant_id):
        return self._connection(tenant=tenant_id)

    @classmethod
    def _compare_generation(cls, connection, tenant, expected):
        if expected is not None:
            identifier(expected, "expected_generation")
        current = cls._current(connection, tenant)
        if current != expected:
            raise ConflictError("current generation changed; reopen snapshot before retrying")
        return current

    def publish(self, tenant_id: str, job_id: str, expected_generation: str | None, *,
                principal: Principal | None = None) -> Snapshot:
        actor = self._admin_actor(principal, tenant_id)
        identifier(tenant_id, "tenant_id")
        identifier(job_id, "job_id")
        with self._connection(tenant=tenant_id) as connection:
            current = self._compare_generation(connection, tenant_id, expected_generation)
            job = self._job(connection, tenant_id, job_id)
            self._reject_candidate_job(connection, tenant_id, job)
            previous = self._snapshot(connection, tenant_id, current) if current else None
            revisions = dict(previous.revisions) if previous else {}
            if previous and revisions.get(job["document_id"]) == job["revision_id"]:
                snapshot = previous
            else:
                revisions[job["document_id"]] = job["revision_id"]
                snapshot = self._create_snapshot(connection, tenant_id, revisions)
                self._event(connection, tenant_id, job["document_id"], "publish", actor, job["revision_id"])
            connection.execute("UPDATE jobs SET state='published' WHERE tenant_id=%s AND job_id=%s", (tenant_id, job_id))
            return snapshot

    @staticmethod
    def _allowed(row, access: AccessContext) -> bool:
        subjects = json.loads(row["allowed_subjects"])
        return (not row["revoked"] and domains_allowed(json.loads(row["data_domains"]), access.data_domains)
                and (not subjects or bool(access.subjects.intersection(subjects))))

    def list_chunks(self, snapshot: Snapshot, access: AccessContext, sku: str | None = None, *,
                    allow_candidate: bool = False) -> tuple[ChildChunk, ...]:
        if sku is not None:
            identifier(sku, "sku")
        with self._snapshot_read() as connection:
            actual = self._trusted_snapshot(connection, snapshot, access, allow_candidate=allow_candidate)
            rows = connection.execute("SELECT c.value_json,d.allowed_subjects,d.revoked,d.data_domains FROM children c "
                f"JOIN {self._revision_source(allow_candidate)} s USING(tenant_id,document_id,revision_id) "
                "JOIN active_documents d USING(tenant_id,document_id) WHERE s.tenant_id=%s AND s.generation=%s "
                "ORDER BY c.document_id,c.chunk_id", (actual.tenant_id, actual.generation)).fetchall()
            chunks = []
            for row in rows:
                if self._allowed(row, access):
                    value = json.loads(row["value_json"])
                    value["sku_ids"] = tuple(value["sku_ids"])
                    child = ChildChunk(**value)
                    if child.scope == "store" or (sku is not None and sku in child.sku_ids):
                        chunks.append(child)
            return tuple(chunks)

    def get_parents(self, snapshot: Snapshot, parent_ids: Sequence[str], access: AccessContext, *,
                    allow_candidate: bool = False) -> tuple[ParentChunk, ...]:
        if isinstance(parent_ids, (str, bytes)):
            raise ValidationError("parent_ids must be a sequence of identifiers")
        wanted = tuple(dict.fromkeys(identifier(v, "parent_id") for v in parent_ids))
        with self._snapshot_read() as connection:
            actual = self._trusted_snapshot(connection, snapshot, access, allow_candidate=allow_candidate)
            rows = connection.execute("SELECT p.value_json,p.parent_id,d.allowed_subjects,d.revoked,d.data_domains FROM parents p "
                f"JOIN {self._revision_source(allow_candidate)} s USING(tenant_id,document_id,revision_id) JOIN active_documents d USING(tenant_id,document_id) "
                "WHERE s.tenant_id=%s AND s.generation=%s AND p.parent_id=ANY(%s)",
                (actual.tenant_id, actual.generation, list(wanted))).fetchall()
            found = {}
            for row in rows:
                if self._allowed(row, access):
                    value = json.loads(row["value_json"])
                    value["heading_path"] = tuple(value["heading_path"])
                    found[row["parent_id"]] = ParentChunk(**value)
            return tuple(found[v] for v in wanted if v in found)


    def rollback(self, tenant_id: str, generation: str, expected_generation: str | None) -> Snapshot:
        identifier(tenant_id, "tenant_id")
        identifier(generation, "generation")
        with self._connection(tenant=tenant_id) as connection:
            self._compare_generation(connection, tenant_id, expected_generation)
            target = self._snapshot(connection, tenant_id, generation)
            return self._rollback_manifest(connection, target)

    def set_access(self, tenant_id: str, document_id: str, allowed_subjects: tuple[str, ...] | None = None,
                   revoked: bool | None = None,
                   data_domains: tuple[str, ...] | None = None, *, principal: Principal | None = None) -> None:
        actor = self._admin_actor(principal, tenant_id)
        identifier(tenant_id, "tenant_id")
        identifier(document_id, "document_id")
        if ((allowed_subjects is not None and not isinstance(allowed_subjects, tuple))
                or (revoked is not None and type(revoked) is not bool)):
            raise ValidationError("allowed_subjects must be a tuple and revoked a boolean, or None to preserve")
        for subject in allowed_subjects or ():
            identifier(subject, "allowed_subject")
        if data_domains is not None:
            data_domains = normalize_data_domains(data_domains)
        with self._connection(tenant=tenant_id) as connection:
            result = connection.execute(
                "UPDATE documents SET allowed_subjects=COALESCE(%s,allowed_subjects),"
                "revoked=COALESCE(%s,revoked),data_domains=COALESCE(%s,data_domains) "
                "WHERE tenant_id=%s AND document_id=%s",
                (_json(allowed_subjects) if allowed_subjects is not None else None,
                 int(revoked) if revoked is not None else None,
                 _json(data_domains) if data_domains is not None else None, tenant_id, document_id))
            if result.rowcount != 1:
                raise NotFoundError("document not found")
            self._event(connection, tenant_id, document_id, "set_access", actor)


class PostgresTraceSink(TraceHistory, _PostgresDatabase):
    """Immutable tenant-scoped traces using the existing secret-field policy."""

    _placeholder = "%s"

    def save(self, trace: dict[str, Any], *, conversation_id: str | None = None) -> None:
        if not isinstance(trace, dict):
            raise ValidationError("trace must be a dictionary")
        trace = with_conversation(trace, conversation_id)
        tenant = identifier(trace.get("tenant_id"), "tenant_id")
        trace_id = identifier(trace.get("trace_id"), "trace_id")
        SQLiteTraceSink._reject_secrets(trace)
        value = _json(trace)
        with self._connection(tenant=tenant) as connection:
            existing = connection.execute("SELECT envelope_json FROM traces WHERE tenant_id=%s AND trace_id=%s", (tenant, trace_id)).fetchone()
            if existing is not None:
                if existing["envelope_json"] != value:
                    raise ConflictError("trace_id already belongs to a different trace")
                return
            connection.execute("INSERT INTO traces(tenant_id,trace_id,envelope_json) VALUES (%s,%s,%s)", (tenant, trace_id, value))
            index_trace(connection, trace, self._placeholder)

    def get(self, tenant_id: str, trace_id: str) -> dict[str, Any]:
        identifier(tenant_id, "tenant_id")
        identifier(trace_id, "trace_id")
        with self._connection() as connection:
            row = connection.execute("SELECT envelope_json FROM traces WHERE tenant_id=%s AND trace_id=%s", (tenant_id, trace_id)).fetchone()
            if row is None:
                raise NotFoundError("trace not found")
            return json.loads(row["envelope_json"])


def migrate_sqlite_to_postgres(source: Path, dsn: str, *, schema: str = "adaptive_rag") -> dict[str, int]:
    """Explicitly copy an offline SQLite knowledge DB into an empty PG target.

    Accounts, files, and vector caches are outside this migration. The caller
    must stop writers and back up the source. No source writes are performed;
    all target rows are committed together, or none are. Repeated migrations to
    a nonempty target are rejected rather than merged or overwritten.
    """
    source = Path(source).resolve()
    if not source.is_file():
        raise ValidationError("migration source must be an existing SQLite knowledge database")
    target = PostgresKnowledgeStore(dsn, schema=schema)
    _, sql, _ = _driver()
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            original.row_factory = sqlite3.Row
            original.execute("BEGIN")
            if original.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or original.execute("PRAGMA foreign_key_check").fetchone():
                raise ConflictError("migration source failed SQLite integrity checks")
            version = original.execute("SELECT version FROM schema_metadata WHERE id=1").fetchone()
            if version is None or version[0] not in (1, STORAGE_VERSION):
                raise ConflictError("unsupported migration source schema version")
            with target._connection() as connection:
                connection.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
                    sql.SQL(", ").join(sql.Identifier(t) for t in _TABLES)))
                if any(connection.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(t))).fetchone() for t in _TABLES):
                    raise ConflictError("PostgreSQL migration target must be empty")
                tables = {r[0] for r in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                candidate_tables = {"candidate_snapshots", "candidate_revisions", "candidate_jobs"}
                legacy = tables.isdisjoint(candidate_tables)
                counts = {}
                for table in _TABLES:
                    if ((legacy and table in candidate_tables) or
                            (table in {"document_trash", "document_events", "retired_document_ids"} and table not in tables)):
                        counts[table] = 0
                        continue
                    rows = original.execute(f'SELECT * FROM "{table}"').fetchall()
                    counts[table] = len(rows)
                    if rows:
                        columns = tuple(rows[0].keys())
                        if table == "traces" and version[0] == 1:
                            columns = tuple("envelope_json" if c == "value_json" else c for c in columns)
                        statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                            sql.Identifier(table), sql.SQL(", ").join(sql.Identifier(c) for c in columns),
                            sql.SQL(", ").join(sql.Placeholder() for _ in columns))
                        with connection.cursor() as cursor:
                            cursor.executemany(statement, [tuple(row) for row in rows])
                backfill_history(connection, postgres=True,
                                 indexed_source=original if version[0] == STORAGE_VERSION else None)
                return counts
    except sqlite3.Error:
        raise ValidationError("cannot read a compatible SQLite migration source") from None
