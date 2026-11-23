"""Private transactional persistence for image assets and durable index jobs.

Objects and vectors are external effects. Only asset metadata and its outbox
are committed together. A running job has an owner token; completing an old
job can never publish a newer asset revision.
"""

from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .domain import CapabilityError, ValidationError


SCHEMA = """
CREATE TABLE IF NOT EXISTS image_assets (
    tenant_id TEXT NOT NULL, asset_id TEXT NOT NULL, revision_id TEXT NOT NULL,
    body TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, asset_id)
);
CREATE TABLE IF NOT EXISTS image_asset_revisions (
    tenant_id TEXT NOT NULL, asset_id TEXT NOT NULL, revision_id TEXT NOT NULL,
    body TEXT NOT NULL, PRIMARY KEY (tenant_id, asset_id, revision_id)
);
CREATE TABLE IF NOT EXISTS image_index_jobs (
    tenant_id TEXT NOT NULL, job_id TEXT NOT NULL, asset_id TEXT NOT NULL,
    revision_id TEXT NOT NULL, state TEXT NOT NULL, owner TEXT,
    started_at DOUBLE PRECISION, body TEXT NOT NULL,
    PRIMARY KEY (tenant_id, job_id)
);
CREATE INDEX IF NOT EXISTS image_jobs_pending
    ON image_index_jobs(tenant_id, state);
CREATE TABLE IF NOT EXISTS image_traces (
    tenant_id TEXT NOT NULL, trace_id TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY (tenant_id, trace_id)
);
CREATE TABLE IF NOT EXISTS image_trash (
    tenant_id TEXT NOT NULL, trash_id TEXT NOT NULL, asset_id TEXT NOT NULL,
    deleted_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
    deleted_by TEXT NOT NULL, admin_hold INTEGER NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY (tenant_id, trash_id)
);
CREATE INDEX IF NOT EXISTS image_trash_expiry ON image_trash(expires_at);
CREATE INDEX IF NOT EXISTS image_trash_asset ON image_trash(tenant_id, asset_id);
CREATE TABLE IF NOT EXISTS image_events (
    tenant_id TEXT NOT NULL, asset_id TEXT NOT NULL, incarnation TEXT NOT NULL,
    event_id TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, action TEXT NOT NULL,
    actor_id TEXT NOT NULL, revision_id TEXT NOT NULL, PRIMARY KEY (tenant_id,event_id),
    FOREIGN KEY (tenant_id,asset_id,revision_id) REFERENCES image_asset_revisions(tenant_id,asset_id,revision_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS image_events_asset ON image_events(tenant_id,asset_id,incarnation);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


class _Connection:
    def __init__(self, connection, postgres):
        self.connection, self.postgres = connection, postgres

    def execute(self, statement, parameters=()):
        return self.connection.execute(statement.replace("?", "%s") if self.postgres else statement,
                                       parameters)


class AssetDatabase:
    def __init__(self, directory, postgres_dsn=None):
        self._dsn = postgres_dsn
        self.path = None
        if not postgres_dsn:
            root = Path(directory).resolve()
            root.mkdir(parents=True, exist_ok=True)
            self.path = root / "assets.sqlite3"
            if self.path.is_symlink():
                raise ValidationError("asset database must not be a symlink")
        with self.transaction("adaptive-rag:image-schema-v1") as connection:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)

    @contextmanager
    def transaction(self, lock=""):
        connection = None
        try:
            if self._dsn:
                try:
                    import psycopg
                    from psycopg.rows import dict_row
                except ImportError:
                    raise CapabilityError("PostgreSQL assets require the remote optional dependency") from None
                connection = psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=10)
            else:
                if self.path.is_symlink():
                    raise ValidationError("asset database must not be a symlink")
                connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute("BEGIN IMMEDIATE")
            wrapped = _Connection(connection, bool(self._dsn))
            if self._dsn and lock:
                key = int.from_bytes(sha256(lock.encode()).digest()[:8], "big", signed=True)
                wrapped.execute("SELECT pg_advisory_xact_lock(?)", (key,))
            yield wrapped
            connection.commit()
        except (sqlite3.DatabaseError, OSError):
            if connection is not None:
                connection.rollback()
            raise CapabilityError("asset metadata storage is unavailable") from None
        except Exception as exc:
            if connection is not None:
                connection.rollback()
            if exc.__class__.__module__.startswith("psycopg"):
                raise CapabilityError("asset PostgreSQL storage is unavailable") from None
            raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def current(connection, tenant_id, asset_id):
        row = connection.execute("SELECT body,deleted FROM image_assets WHERE tenant_id=? AND asset_id=?",
                                 (tenant_id, asset_id)).fetchone()
        return (json.loads(row["body"]), bool(row["deleted"])) if row else None

    @staticmethod
    def write_asset(connection, asset):
        body = encode(asset)
        key = (asset["tenant_id"], asset["asset_id"], asset["revision_id"])
        connection.execute("INSERT INTO image_asset_revisions(tenant_id,asset_id,revision_id,body) VALUES(?,?,?,?) ON CONFLICT(tenant_id,asset_id,revision_id) DO NOTHING",
                           (*key, body))
        connection.execute("INSERT INTO image_assets(tenant_id,asset_id,revision_id,body,deleted) VALUES(?,?,?,?,0) ON CONFLICT(tenant_id,asset_id) DO UPDATE SET revision_id=excluded.revision_id,body=excluded.body,deleted=0",
                           (*key, body))

    @staticmethod
    def write_current(connection, asset):
        connection.execute("UPDATE image_assets SET body=? WHERE tenant_id=? AND asset_id=? AND revision_id=? AND deleted=0",
                           (encode(asset), asset["tenant_id"], asset["asset_id"], asset["revision_id"]))

    @staticmethod
    def copy_vectors(connection, asset, profile, previous_revision, job_id):
        from .domain import stable_id
        tenant, asset_id = asset["tenant_id"], asset["asset_id"]
        old_id = stable_id("image-index-v1", tenant, asset_id, previous_revision, profile)
        rows = connection.execute("SELECT job_id,body FROM image_index_jobs WHERE tenant_id=? AND job_id IN (?,?)",
                                  (tenant, old_id, job_id)).fetchall()
        jobs = {r["job_id"]: json.loads(r["body"]) for r in rows}
        vectors = jobs.get(old_id, {}).get("vectors")
        if vectors is not None:
            connection.execute("UPDATE image_index_jobs SET body=? WHERE tenant_id=? AND job_id=?",
                               (encode(jobs[job_id] | {"vectors": vectors}), tenant, job_id))

    @staticmethod
    def enqueue(connection, asset, profile, *, requeue=False):
        job = {"asset_id": asset["asset_id"], "revision_id": asset["revision_id"],
               "profile": profile, "vectors": None, "error": None, "stages": []}
        from .domain import stable_id
        job_id = stable_id("image-index-v1", asset["tenant_id"], asset["asset_id"], asset["revision_id"], profile)
        connection.execute("INSERT INTO image_index_jobs(tenant_id,job_id,asset_id,revision_id,state,body) VALUES(?,?,?,?,?,?) ON CONFLICT(tenant_id,job_id) DO NOTHING",
                           (asset["tenant_id"], job_id, asset["asset_id"], asset["revision_id"], "pending", encode(job)))
        if requeue:
            connection.execute("UPDATE image_index_jobs SET state='pending',owner=NULL WHERE tenant_id=? AND job_id=?",
                               (asset["tenant_id"], job_id))
        return job_id
