"""Local persistence adapters: immutable revisions, CAS publication, and live ACLs.

SQLite connections belong to one operation, not one application or HTTP thread.
Snapshots retain their manifest forever; access is deliberately *not* snapshotted.
"""

from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import tempfile
from typing import Any, Iterator, Sequence
from uuid import uuid4

from .domain import (
    AccessContext, ChildChunk, ConflictError, ForbiddenError, IngestResult,
    NotFoundError, ParentChunk, PreparedDocument, Snapshot, ValidationError,
    identifier, nonempty, stable_id,
)
from .config import Principal
from .document_lifecycle import DocumentLifecycle, LIFECYCLE_SCHEMA
from .data_access import domains_allowed, normalize_data_domains
from .trace_history import STORAGE_VERSION, TraceHistory, index_trace, initialize_history, with_conversation


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError("value must be finite JSON data") from exc


def _digest(content: bytes) -> str:
    return sha256(content).hexdigest()


def _hash(value: str, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValidationError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _document_listing(rows, generation, published) -> list[dict[str, Any]]:
    """Describe live policies without returning any document content."""
    grouped = {}
    for row in rows:
        grouped.setdefault(row["document_id"], []).append(row)
    result = []
    for document_id, versions in grouped.items():
        versions = sorted(versions, key=lambda row: (row["created_at"], row["revision_id"]), reverse=True)
        current = next((row for row in versions if row["revision_id"] == published.get(document_id)), versions[0])
        metadata = json.loads(current["metadata_json"])
        result.append({
            "document_id": document_id, "title": metadata.get("title", document_id),
            "version": current["version"], "revision_id": current["revision_id"],
            "generation": generation, "published": document_id in published,
            "revoked": bool(current["revoked"]),
            "allowed_subjects": json.loads(current["allowed_subjects"]),
            "data_domains": json.loads(current["data_domains"]),
            "staged_jobs": [{"job_id": row["job_id"], "revision_id": row["revision_id"],
                             "version": row["version"], "title": json.loads(row["metadata_json"]).get("title", document_id),
                             "created_at": row["created_at"]}
                            for row in versions if row["state"] == "staged"],
        })
    return result


class FileObjectStore:
    """Content-addressed bytes with atomic replacement and integrity checks."""

    def __init__(self, data_dir: Path):
        data_dir = Path(data_dir).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.root = data_dir / "objects"
        if self.root.is_symlink():
            raise ValidationError("object directory must not be a symlink")
        self.root.mkdir(exist_ok=True)
        self._resolved_root = self.root.resolve()
        if self._resolved_root != self.root:
            raise ValidationError("object directory must not be a linked directory")

    def _path(self, key: str, create: bool = False) -> Path:
        _hash(key, "object key")
        bucket = self.root / key[:2]
        if (self.root.is_symlink() or bucket.is_symlink()
                or self.root.resolve() != self._resolved_root
                or bucket.resolve().parent != self._resolved_root):
            raise ValidationError("object path must not contain symlinks")
        if create:
            bucket.mkdir(exist_ok=True)
        path = bucket / key
        if path.is_symlink() or not path.resolve().is_relative_to(self._resolved_root):
            raise ValidationError("object path escapes its storage directory")
        return path

    def put(self, content: bytes) -> str:
        if not isinstance(content, bytes):
            raise ValidationError("object content must be bytes")
        key = _digest(content)
        path = self._path(key, create=True)
        if path.exists():
            if self.get(key) != content:
                raise ConflictError("existing object does not match its content hash")
            return key
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            # Recheck after writing; never follow an existing symlink target.
            self._path(key)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.get(key)
        return key

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(path, flags), "rb") as handle:
                content = handle.read()
        except FileNotFoundError as exc:
            raise NotFoundError("object not found") from exc
        if _digest(content) != key:
            raise ConflictError("stored object failed SHA-256 integrity verification")
        return content


_CANDIDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate_snapshots (
    tenant_id TEXT NOT NULL, generation TEXT NOT NULL, base_generation TEXT,
    request_key TEXT NOT NULL, created_at TEXT NOT NULL,
    activated INTEGER NOT NULL DEFAULT 0 CHECK (activated IN (0,1)),
    PRIMARY KEY (tenant_id, generation), UNIQUE (tenant_id, request_key),
    FOREIGN KEY (tenant_id, base_generation) REFERENCES snapshots (tenant_id, generation)
);
CREATE TABLE IF NOT EXISTS candidate_revisions (
    tenant_id TEXT NOT NULL, generation TEXT NOT NULL, document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, generation, document_id),
    FOREIGN KEY (tenant_id, generation) REFERENCES candidate_snapshots (tenant_id, generation) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, document_id, revision_id) REFERENCES revisions (tenant_id, document_id, revision_id)
);
CREATE TABLE IF NOT EXISTS candidate_jobs (
    tenant_id TEXT NOT NULL, generation TEXT NOT NULL, job_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, generation, job_id),
    FOREIGN KEY (tenant_id, generation) REFERENCES candidate_snapshots (tenant_id, generation) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, job_id) REFERENCES jobs (tenant_id, job_id)
);
CREATE INDEX IF NOT EXISTS candidate_jobs_by_job ON candidate_jobs (tenant_id, job_id);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_metadata (
    id INTEGER PRIMARY KEY CHECK (id=1), version INTEGER NOT NULL
);
INSERT OR IGNORE INTO schema_metadata VALUES (1, 1);
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


class _Database:
    def __init__(self, data_dir: Path):
        data_dir = Path(data_dir).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "knowledge.sqlite3"
        if self.path.is_symlink():
            raise ValidationError("database must not be a symlink")
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            # Changing a legacy database to WAL can report BUSY immediately
            # during concurrent startup, even with a busy timeout configured.
            deadline = time.monotonic() + 10
            while True:
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    code = getattr(exc, "sqlite_errorcode", 0) & 255
                    if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
            connection.executescript("BEGIN IMMEDIATE;" + _SCHEMA + _CANDIDATE_SCHEMA)
            version = connection.execute("SELECT version FROM schema_metadata WHERE id=1").fetchone()[0]
            if version not in (1, STORAGE_VERSION):
                raise ConflictError("unsupported storage schema version")
            # Serialize the read/ALTER sequence across concurrent API/worker
            # startup. Existing unclassified documents stay restricted.
            for table, column, definition in (
                ("documents", "data_domains", "TEXT NOT NULL DEFAULT '[]'"),
                ("documents", "owner_id", "TEXT"),
                ("revisions", "created_at", "TEXT NOT NULL DEFAULT ''"),
            ):
                columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            for statement in LIFECYCLE_SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            initialize_history(connection, version)
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def _connection(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        if self.path.is_symlink():
            raise ValidationError("database must not be a symlink")
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            if connection.in_transaction:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()


class _SnapshotStore(DocumentLifecycle):
    """Shared snapshot lifecycle. Each adapter serializes writes for a tenant."""

    _placeholder = "?"

    @classmethod
    def _sql(cls, statement):
        return statement.replace("?", cls._placeholder)

    @staticmethod
    def _revision_source(allow_candidate):
        if allow_candidate is True:
            return "(SELECT * FROM snapshot_revisions UNION ALL SELECT * FROM candidate_revisions)"
        return "snapshot_revisions"

    @classmethod
    def _snapshot(cls, connection, tenant, generation, *, allow_candidate=False):
        query = "SELECT created_at FROM snapshots WHERE tenant_id=? AND generation=?"
        params = (tenant, generation)
        if allow_candidate is True:
            query += " UNION ALL SELECT created_at FROM candidate_snapshots WHERE tenant_id=? AND generation=? AND activated=0"
            params += params
        row = connection.execute(cls._sql(query), params).fetchone()
        if row is None:
            raise NotFoundError("snapshot not found")
        source = cls._revision_source(allow_candidate)
        revisions = connection.execute(cls._sql(
            f"SELECT document_id,revision_id FROM {source} AS manifest WHERE tenant_id=? AND generation=?"),
            (tenant, generation))
        # Match manifest creation order, independent of database collation.
        entries = tuple(sorted((r["document_id"], r["revision_id"]) for r in revisions))
        return Snapshot(tenant, generation, entries, row["created_at"])

    @classmethod
    def _set_current(cls, connection, tenant, generation):
        connection.execute(cls._sql("INSERT INTO current_snapshots VALUES (?,?) ON CONFLICT(tenant_id) "
                                    "DO UPDATE SET generation=excluded.generation"), (tenant, generation))

    @classmethod
    def _write_manifest(cls, connection, tenant, generation, revisions):
        created_at = datetime.now(timezone.utc).isoformat()
        entries = tuple(sorted(revisions.items()))
        connection.execute(cls._sql("INSERT INTO snapshots VALUES (?,?,?)"), (tenant, generation, created_at))
        with closing(connection.cursor()) as cursor:
            cursor.executemany(cls._sql("INSERT INTO snapshot_revisions VALUES (?,?,?,?)"),
                               [(tenant, generation, doc, rev) for doc, rev in entries])
        return Snapshot(tenant, generation, entries, created_at)

    @classmethod
    def _create_snapshot(cls, connection, tenant, revisions):
        snapshot = cls._write_manifest(connection, tenant, uuid4().hex, revisions)
        cls._set_current(connection, tenant, snapshot.generation)
        return snapshot

    def open_snapshot(self, tenant_id: str, generation: str | None = None, *, allow_candidate: bool = False) -> Snapshot | None:
        identifier(tenant_id, "tenant_id")
        if generation is not None:
            identifier(generation, "generation")
        with self._snapshot_read() as connection:
            generation = generation if generation is not None else self._current(connection, tenant_id)
            return self._snapshot(connection, tenant_id, generation, allow_candidate=allow_candidate) if generation else None

    @classmethod
    def _trusted_snapshot(cls, connection, snapshot: Snapshot, access: AccessContext, *, allow_candidate=False) -> Snapshot:
        if not isinstance(snapshot, Snapshot) or not isinstance(access, AccessContext):
            raise ValidationError("snapshot and access context are required")
        identifier(snapshot.tenant_id, "tenant_id")
        identifier(snapshot.generation, "generation")
        access.__post_init__()
        if snapshot.tenant_id != access.tenant_id:
            raise ForbiddenError("snapshot belongs to a different tenant")
        return cls._verify_manifest(connection, snapshot, allow_candidate=allow_candidate)

    @classmethod
    def _verify_manifest(cls, connection, snapshot, *, allow_candidate):
        actual = cls._snapshot(connection, snapshot.tenant_id, snapshot.generation, allow_candidate=allow_candidate)
        if actual != snapshot:
            raise ForbiddenError("snapshot manifest does not match the published snapshot")
        return actual

    def list_prewarm_chunks(self, snapshot: Snapshot, *, allow_candidate: bool = False) -> tuple[ChildChunk, ...]:
        """Trusted operator enumeration, never query authorization or a readiness assertion."""
        if not isinstance(snapshot, Snapshot):
            raise ValidationError("snapshot required")
        identifier(snapshot.tenant_id, "tenant_id")
        identifier(snapshot.generation, "generation")
        with self._snapshot_read() as connection:
            actual = self._verify_manifest(connection, snapshot, allow_candidate=allow_candidate)
            source = self._revision_source(allow_candidate)
            rows = connection.execute(self._sql("SELECT c.value_json FROM children c "
                f"JOIN {source} s USING(tenant_id,document_id,revision_id) JOIN active_documents d USING(tenant_id,document_id) "
                "WHERE s.tenant_id=? AND s.generation=? AND d.revoked=0 ORDER BY c.document_id,c.chunk_id"),
                (actual.tenant_id, actual.generation))
            chunks = []
            for row in rows:
                value = json.loads(row["value_json"])
                value["sku_ids"] = tuple(value["sku_ids"])
                chunks.append(ChildChunk(**value))
            return tuple(chunks)

    def check_prewarm_access(self, tenant_id: str, document_ids: Sequence[str], *, access: AccessContext | None) -> bool:
        """Recheck live policies for a preselected batch; None is the trusted all-scopes operator."""
        identifier(tenant_id, "tenant_id")
        if access is not None:
            if not isinstance(access, AccessContext):
                raise ValidationError("access context required")
            if access.tenant_id != tenant_id:
                raise ForbiddenError("prewarm batch belongs to a different tenant")
        if isinstance(document_ids, (str, bytes)) or not isinstance(document_ids, Sequence):
            raise ValidationError("document_ids must be a sequence of identifiers")
        ids = tuple(dict.fromkeys(identifier(doc, "document_id") for doc in document_ids))
        with self._snapshot_read() as connection:
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                markers = ",".join("?" for _ in batch)
                rows = connection.execute(self._sql("SELECT allowed_subjects,data_domains,revoked FROM active_documents "
                    f"WHERE tenant_id=? AND document_id IN ({markers})"), (tenant_id, *batch)).fetchall()
                if len(rows) != len(batch) or any(row["revoked"] or (access is not None and not self._allowed(row, access))
                                                  for row in rows):
                    return False
        return True

    def is_candidate_snapshot(self, tenant_id: str, generation: str) -> bool:
        """True only for an unactivated generation belonging to this tenant."""
        identifier(tenant_id, "tenant_id")
        identifier(generation, "generation")
        with self._connection() as connection:
            return connection.execute(self._sql("SELECT 1 FROM candidate_snapshots WHERE tenant_id=? "
                "AND generation=? AND activated=0"), (tenant_id, generation)).fetchone() is not None

    def create_candidate_snapshot(self, tenant_id: str, job_ids: Sequence[str], expected_generation: str | None) -> Snapshot:
        """Overlay a set of staged jobs on the expected base; order does not affect retry identity."""
        identifier(tenant_id, "tenant_id")
        if isinstance(job_ids, (str, bytes)) or not isinstance(job_ids, Sequence) or not job_ids:
            raise ValidationError("job_ids must be a nonempty sequence of identifiers")
        ids = sorted(identifier(job_id, "job_id") for job_id in job_ids)
        if len(set(ids)) != len(ids):
            raise ValidationError("duplicate job in candidate")
        with self._snapshot_write(tenant_id) as connection:
            base = self._compare_generation(connection, tenant_id, expected_generation)
            request_key = stable_id("candidate", tenant_id, base, ids)
            existing = connection.execute(self._sql("SELECT generation FROM candidate_snapshots WHERE tenant_id=? AND request_key=?"),
                                          (tenant_id, request_key)).fetchone()
            if existing:
                return self._snapshot(connection, tenant_id, existing["generation"], allow_candidate=True)
            generation = uuid4().hex
            changes = {}
            # Bound the number of bind parameters even for large SQLite batches.
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                markers = ",".join("?" for _ in batch)
                rows = connection.execute(self._sql("SELECT j.document_id,j.revision_id,j.state,t.trash_id FROM jobs j "
                    "LEFT JOIN document_trash t USING(tenant_id,document_id) "
                    f"WHERE j.tenant_id=? AND j.job_id IN ({markers})"), (tenant_id, *batch)).fetchall()
                if len(rows) != len(batch):
                    raise NotFoundError("ingestion job not found")
                for row in rows:
                    if row["trash_id"]:
                        raise ConflictError("restore document before creating a candidate")
                    if row["state"] != "staged":
                        raise ConflictError("candidate requires staged jobs")
                    if row["document_id"] in changes:
                        raise ValidationError("candidate contains multiple jobs for one document")
                    changes[row["document_id"]] = row["revision_id"]
            revisions = dict(self._snapshot(connection, tenant_id, base).revisions) if base else {}
            revisions.update(changes)
            created_at = datetime.now(timezone.utc).isoformat()
            connection.execute(self._sql("INSERT INTO candidate_snapshots (tenant_id,generation,base_generation,request_key,created_at) "
                                         "VALUES (?,?,?,?,?)"), (tenant_id, generation, base, request_key, created_at))
            entries = tuple(sorted(revisions.items()))
            with closing(connection.cursor()) as cursor:
                cursor.executemany(self._sql("INSERT INTO candidate_revisions VALUES (?,?,?,?)"),
                                   [(tenant_id, generation, doc, rev) for doc, rev in entries])
                cursor.executemany(self._sql("INSERT INTO candidate_jobs VALUES (?,?,?)"),
                                   [(tenant_id, generation, job_id) for job_id in ids])
            return Snapshot(tenant_id, generation, entries, created_at)

    def activate_candidate_snapshot(self, tenant_id: str, generation: str, expected_generation: str | None, *,
                                    principal: Principal | None = None) -> Snapshot:
        """Activate only on the recorded base; a lost response can be retried while still current."""
        actor = self._admin_actor(principal, tenant_id)
        identifier(tenant_id, "tenant_id")
        identifier(generation, "generation")
        if expected_generation is not None:
            identifier(expected_generation, "expected_generation")
        with self._snapshot_write(tenant_id) as connection:
            row = connection.execute(self._sql("SELECT base_generation,activated FROM candidate_snapshots "
                "WHERE tenant_id=? AND generation=?"), (tenant_id, generation)).fetchone()
            if row is None:
                raise NotFoundError("candidate snapshot not found")
            if row["activated"]:
                if expected_generation not in (row["base_generation"], generation):
                    raise ConflictError("candidate base generation does not match")
                self._compare_generation(connection, tenant_id, generation)
                return self._snapshot(connection, tenant_id, generation)
            if expected_generation != row["base_generation"]:
                raise ConflictError("candidate base generation does not match")
            self._compare_generation(connection, tenant_id, expected_generation)
            key = (tenant_id, generation)
            connection.execute(self._sql("INSERT INTO snapshots SELECT tenant_id,generation,created_at FROM candidate_snapshots "
                                         "WHERE tenant_id=? AND generation=?"), key)
            connection.execute(self._sql("INSERT INTO snapshot_revisions SELECT * FROM candidate_revisions "
                                         "WHERE tenant_id=? AND generation=?"), key)
            connection.execute(self._sql("DELETE FROM candidate_revisions WHERE tenant_id=? AND generation=?"), key)
            publications = connection.execute(self._sql("SELECT j.document_id,j.revision_id FROM candidate_jobs c "
                "JOIN jobs j USING(tenant_id,job_id) WHERE c.tenant_id=? AND c.generation=?"), key).fetchall()
            for job in publications:
                self._event(connection, tenant_id, job["document_id"], "publish", actor, job["revision_id"])
            connection.execute(self._sql("UPDATE jobs SET state='published' WHERE tenant_id=? AND job_id IN "
                "(SELECT job_id FROM candidate_jobs WHERE tenant_id=? AND generation=?)"),
                (tenant_id, tenant_id, generation))
            connection.execute(self._sql("UPDATE candidate_snapshots SET activated=1 WHERE tenant_id=? AND generation=?"),
                               (tenant_id, generation))
            self._set_current(connection, tenant_id, generation)
            return self._snapshot(connection, tenant_id, generation)

    def abandon_candidate_snapshot(self, tenant_id: str, generation: str) -> bool:
        """Drop a pending manifest and its job reservations. A repeated abandon returns False."""
        identifier(tenant_id, "tenant_id")
        identifier(generation, "generation")
        with self._snapshot_write(tenant_id) as connection:
            row = connection.execute(self._sql("SELECT activated FROM candidate_snapshots WHERE tenant_id=? AND generation=?"),
                                     (tenant_id, generation)).fetchone()
            if row is None:
                return False
            if row["activated"]:
                raise ConflictError("cannot abandon a published candidate")
            connection.execute(self._sql("DELETE FROM candidate_snapshots WHERE tenant_id=? AND generation=?"),
                               (tenant_id, generation))
            return True

    def _rollback_manifest(self, connection, target):
        trashed = connection.execute(self._sql("SELECT document_id FROM document_trash WHERE tenant_id=?"),
                                     (target.tenant_id,))
        revisions = dict(target.revisions)
        for row in trashed:
            revisions.pop(row["document_id"], None)
        return self._create_snapshot(connection, target.tenant_id, revisions)

    def _reject_candidate_job(self, connection, tenant, job):
        if connection.execute(self._sql("SELECT 1 FROM document_trash WHERE tenant_id=? AND document_id=?"),
                              (tenant, job["document_id"])).fetchone():
            raise ConflictError("restore document before publication")
        if job["state"] == "staged" and connection.execute(self._sql(
            "SELECT 1 FROM candidate_jobs j JOIN candidate_snapshots c USING(tenant_id,generation) "
            "WHERE j.tenant_id=? AND j.job_id=? AND c.activated=0"), (tenant, job["job_id"])).fetchone():
            raise ConflictError("job belongs to a candidate; activate the candidate snapshot")


class SQLiteKnowledgeStore(_SnapshotStore, _Database):
    """Revision manifests and current access policy in one transactional store."""

    @staticmethod
    def _validate(document: PreparedDocument) -> None:
        if not isinstance(document, PreparedDocument):
            raise ValidationError("document must be a PreparedDocument")
        request = document.request
        request.__post_init__()
        identifier(document.revision_id, "revision_id")
        nonempty(document.processing_fingerprint, "processing_fingerprint", 1000)
        _hash(document.content_hash, "content_hash")
        if document.content_hash != _digest(request.content.encode("utf-8")):
            raise ValidationError("source content hash mismatch")
        if document.raw_key != document.content_hash:
            raise ValidationError("raw object key must match the source content hash")
        if not document.parents or not document.children:
            raise ValidationError("prepared document requires parent and child chunks")
        identity = (request.tenant_id, request.document_id, request.version, document.revision_id)
        parents = {}
        children = set()
        referenced = set()
        for parent in document.parents:
            if (parent.tenant_id, parent.document_id, parent.version, parent.revision_id) != identity:
                raise ValidationError("parent identity does not match prepared document")
            identifier(parent.parent_id, "parent_id")
            nonempty(parent.text, "parent text", 2_000_000)
            nonempty(parent.locator, "parent locator", 10000)
            if not isinstance(parent.heading_path, tuple):
                raise ValidationError("heading_path must be a tuple")
            for heading in parent.heading_path:
                nonempty(heading, "heading", 10000)
            if parent.parent_id in parents:
                raise ValidationError("duplicate parent_id")
            if parent.content_hash != _digest(parent.text.encode("utf-8")):
                raise ValidationError("parent content hash mismatch")
            parents[parent.parent_id] = parent
        for child in document.children:
            if (child.tenant_id, child.document_id, child.version, child.revision_id) != identity:
                raise ValidationError("child identity does not match prepared document")
            identifier(child.chunk_id, "chunk_id")
            if child.chunk_id in children:
                raise ValidationError("duplicate chunk_id")
            children.add(child.chunk_id)
            if child.parent_id not in parents:
                raise ValidationError("child references an unknown parent")
            referenced.add(child.parent_id)
            nonempty(child.text, "child text", 2_000_000)
            nonempty(child.retrieval_text, "retrieval_text", 2_100_000)
            if child.text not in child.retrieval_text:
                raise ValidationError("retrieval_text must contain the citable child text")
            nonempty(child.locator, "child locator", 10000)
            if child.text not in parents[child.parent_id].text:
                raise ValidationError("child text is not present in its parent")
            if (child.scope, child.sku_ids, child.language) != (request.scope, request.sku_ids, request.language):
                raise ValidationError("child metadata does not match source metadata")
        if referenced != set(parents):
            raise ValidationError("every parent must have a child chunk")

    def stage(self, document: PreparedDocument, *, principal: Principal | None = None) -> IngestResult:
        self._validate(document)
        request = document.request
        tenant, doc, revision = request.tenant_id, request.document_id, document.revision_id
        job_id = stable_id("ingest", tenant, doc, revision)
        canonical = asdict(document)
        canonical["request"]["sku_ids"] = sorted(set(request.sku_ids))
        canonical["request"]["allowed_subjects"] = sorted(set(request.allowed_subjects))
        if not request.data_domains:
            canonical["request"].pop("data_domains")
        canonical["parents"] = sorted(canonical["parents"], key=lambda parent: parent["parent_id"])
        canonical["children"] = sorted(canonical["children"], key=lambda child: child["chunk_id"])
        for child in canonical["children"]:
            child["sku_ids"] = sorted(set(child["sku_ids"]))
        payload_hash = _digest(_json(canonical).encode("utf-8"))
        metadata = asdict(request)
        metadata.pop("content")
        try:
            with self._connection(write=True) as connection:
                owner = self._stage_owner(connection, tenant, doc, principal)
                existing = connection.execute(
                    "SELECT payload_hash FROM revisions WHERE tenant_id=? AND document_id=? AND revision_id=?",
                    (tenant, doc, revision),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != payload_hash:
                        raise ConflictError("revision_id already belongs to different prepared content")
                    row = self._job(connection, tenant, job_id)
                    return self._ingest_result(row, request.version, duplicate=True)
                connection.execute(
                    "INSERT INTO documents(tenant_id,document_id,allowed_subjects,revoked,data_domains,owner_id) "
                    "VALUES (?,?,?,0,?,?) ON CONFLICT(tenant_id,document_id) DO NOTHING",
                    (tenant, doc, _json(request.allowed_subjects), _json(request.data_domains), owner),
                )
                connection.execute(
                    "INSERT INTO revisions(tenant_id,document_id,revision_id,version,processing_fingerprint,content_hash,raw_key,metadata_json,payload_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (tenant, doc, revision, request.version, document.processing_fingerprint,
                     document.content_hash, document.raw_key, _json(metadata), payload_hash, datetime.now(timezone.utc).isoformat()),
                )
                connection.executemany(
                    "INSERT INTO parents VALUES (?,?,?,?,?)",
                    [(tenant, doc, revision, parent.parent_id, _json(asdict(parent))) for parent in document.parents],
                )
                connection.executemany(
                    "INSERT INTO children VALUES (?,?,?,?,?,?)",
                    [(tenant, doc, revision, child.chunk_id, child.parent_id, _json(asdict(child))) for child in document.children],
                )
                connection.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,'staged',?,?)",
                    (tenant, job_id, doc, revision, len(document.parents), len(document.children)),
                )
                self._event(connection, tenant, doc, "stage", owner, revision)
                return IngestResult(job_id, revision, doc, request.version, "staged",
                                    len(document.parents), len(document.children))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("prepared chunk identity conflicts with an existing revision") from exc

    @staticmethod
    def _job(connection: sqlite3.Connection, tenant: str, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT j.*, r.version FROM jobs j JOIN revisions r USING(tenant_id,document_id,revision_id) "
            "WHERE j.tenant_id=? AND j.job_id=?", (tenant, job_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("ingestion job not found")
        return row

    @staticmethod
    def _ingest_result(row: sqlite3.Row, version: str, duplicate: bool = False) -> IngestResult:
        return IngestResult(row["job_id"], row["revision_id"], row["document_id"], version,
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
                "JOIN jobs j USING(tenant_id,document_id,revision_id) WHERE d.tenant_id=? "
                "ORDER BY d.document_id,r.created_at DESC,r.revision_id DESC", (tenant_id,),
            ).fetchall()
            published = dict(self._snapshot(connection, tenant_id, generation).revisions) if generation else {}
            return _document_listing(rows, generation, published)

    @staticmethod
    def _current(connection: sqlite3.Connection, tenant: str) -> str | None:
        row = connection.execute("SELECT generation FROM current_snapshots WHERE tenant_id=?", (tenant,)).fetchone()
        return row[0] if row else None

    def _snapshot_read(self):
        return self._connection()

    def _snapshot_write(self, tenant_id):
        return self._connection(write=True)

    @staticmethod
    def _compare_generation(connection: sqlite3.Connection, tenant: str, expected: str | None) -> str | None:
        if expected is not None:
            identifier(expected, "expected_generation")
        current = SQLiteKnowledgeStore._current(connection, tenant)
        if current != expected:
            raise ConflictError("current generation changed; reopen snapshot before retrying")
        return current

    def publish(self, tenant_id: str, job_id: str, expected_generation: str | None, *,
                principal: Principal | None = None) -> Snapshot:
        actor = self._admin_actor(principal, tenant_id)
        identifier(tenant_id, "tenant_id")
        identifier(job_id, "job_id")
        with self._connection(write=True) as connection:
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
            connection.execute(
                "UPDATE jobs SET state='published' WHERE tenant_id=? AND job_id=?", (tenant_id, job_id),
            )
            return snapshot

    @staticmethod
    def _allowed(row: sqlite3.Row, access: AccessContext) -> bool:
        subjects = json.loads(row["allowed_subjects"])
        return (not row["revoked"] and domains_allowed(json.loads(row["data_domains"]), access.data_domains)
                and (not subjects or bool(access.subjects.intersection(subjects))))

    def list_chunks(self, snapshot: Snapshot, access: AccessContext, sku: str | None = None, *,
                    allow_candidate: bool = False) -> tuple[ChildChunk, ...]:
        if sku is not None:
            identifier(sku, "sku")
        with self._snapshot_read() as connection:
            actual = self._trusted_snapshot(connection, snapshot, access, allow_candidate=allow_candidate)
            rows = connection.execute(
                "SELECT c.value_json,d.allowed_subjects,d.revoked,d.data_domains FROM children c "
                f"JOIN {self._revision_source(allow_candidate)} s USING(tenant_id,document_id,revision_id) "
                "JOIN active_documents d USING(tenant_id,document_id) "
                "WHERE s.tenant_id=? AND s.generation=? ORDER BY c.document_id,c.chunk_id",
                (actual.tenant_id, actual.generation),
            ).fetchall()
            chunks = []
            for row in rows:
                if not self._allowed(row, access):
                    continue
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
        wanted = tuple(dict.fromkeys(identifier(value, "parent_id") for value in parent_ids))
        with self._snapshot_read() as connection:
            actual = self._trusted_snapshot(connection, snapshot, access, allow_candidate=allow_candidate)
            # No dynamic SQL/variable-count limit: read only the true snapshot.
            rows = connection.execute(
                "SELECT p.value_json,p.parent_id,d.allowed_subjects,d.revoked,d.data_domains FROM parents p "
                f"JOIN {self._revision_source(allow_candidate)} s USING(tenant_id,document_id,revision_id) "
                "JOIN active_documents d USING(tenant_id,document_id) "
                "WHERE s.tenant_id=? AND s.generation=?", (actual.tenant_id, actual.generation),
            ).fetchall()
            wanted_set = set(wanted)
            found = {}
            for row in rows:
                if row["parent_id"] in wanted_set and self._allowed(row, access):
                    value = json.loads(row["value_json"])
                    value["heading_path"] = tuple(value["heading_path"])
                    found[row["parent_id"]] = ParentChunk(**value)
            return tuple(found[parent_id] for parent_id in wanted if parent_id in found)


    def rollback(self, tenant_id: str, generation: str, expected_generation: str | None) -> Snapshot:
        identifier(tenant_id, "tenant_id")
        identifier(generation, "generation")
        with self._connection(write=True) as connection:
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
        with self._connection(write=True) as connection:
            result = connection.execute(
                "UPDATE documents SET allowed_subjects=COALESCE(?,allowed_subjects),"
                "revoked=COALESCE(?,revoked),data_domains=COALESCE(?,data_domains) "
                "WHERE tenant_id=? AND document_id=?",
                (_json(allowed_subjects) if allowed_subjects is not None else None,
                 int(revoked) if revoked is not None else None,
                 _json(data_domains) if data_domains is not None else None, tenant_id, document_id),
            )
            if result.rowcount != 1:
                raise NotFoundError("document not found")
            self._event(connection, tenant_id, document_id, "set_access", actor)


class SQLiteTraceSink(TraceHistory, _Database):
    """Tenant-isolated immutable JSON traces; credentials are never accepted."""

    _SECRET_KEYS = frozenset({
        "apikey", "credential", "credentials", "authorization", "token",
        "accesstoken", "refreshtoken", "password", "secret", "clientsecret",
    })

    @classmethod
    def _reject_secrets(cls, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValidationError("trace keys must be strings")
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if (normalized in cls._SECRET_KEYS
                        or normalized.endswith(("apikey", "accesstoken", "refreshtoken",
                                                "password", "clientsecret"))):
                    raise ValidationError("trace contains a credential-bearing field")
                cls._reject_secrets(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                cls._reject_secrets(item)

    def save(self, trace: dict[str, Any], *, conversation_id: str | None = None) -> None:
        if not isinstance(trace, dict):
            raise ValidationError("trace must be a dictionary")
        trace = with_conversation(trace, conversation_id)
        tenant = identifier(trace.get("tenant_id"), "tenant_id")
        trace_id = identifier(trace.get("trace_id"), "trace_id")
        self._reject_secrets(trace)
        value = _json(trace)
        with self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT envelope_json FROM traces WHERE tenant_id=? AND trace_id=?", (tenant, trace_id),
            ).fetchone()
            if existing is not None:
                if existing[0] != value:
                    raise ConflictError("trace_id already belongs to a different trace")
                return
            connection.execute("INSERT INTO traces(tenant_id,trace_id,envelope_json) VALUES (?,?,?)", (tenant, trace_id, value))
            index_trace(connection, trace, self._placeholder)

    def get(self, tenant_id: str, trace_id: str) -> dict[str, Any]:
        identifier(tenant_id, "tenant_id")
        identifier(trace_id, "trace_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT envelope_json FROM traces WHERE tenant_id=? AND trace_id=?", (tenant_id, trace_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("trace not found")
            return json.loads(row[0])
