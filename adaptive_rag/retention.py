"""Offline collection for the two stock deployments. Never sweep an object namespace.

Caller must fence *all* writers for the entire operation (retention_cli). Each catalog
transaction records the exact blob candidates before removing their ownership records.
After commit, count references again in both catalogs; retry interrupted external I/O.
"""

from collections import defaultdict
from contextlib import ExitStack, contextmanager, closing
from datetime import datetime
from hashlib import sha256
import json
import math
import sqlite3
from time import time

from .domain import ChildChunk
from .figures import figure_binding, find_figures
from .storage import FileObjectStore, _hash
from .trace_history import STORAGE_VERSION

WEEK = 7 * 24 * 3600
QUEUE = "retention_object_candidates_v1"


class UnsafeInventory(ValueError):
    """Unsupported topology, corrupt metadata or ambiguous references."""


def require(condition):
    if not condition:
        raise UnsafeInventory("incomplete retention inventory")


class Database:
    def __init__(self, connection, postgres=False):
        self.connection, self.postgres = connection, postgres

    def sql(self, text, args=()):
        return self.connection.execute(text.replace("?", "%s") if self.postgres else text, args)

    def queue(self, keys):
        for key in keys:
            self.sql(f"INSERT INTO {QUEUE}(digest) VALUES (?) ON CONFLICT DO NOTHING", (key,))


@contextmanager
def local_db(path):
    # mode=rw: a missing authoritative catalog must never become an empty catalog.
    for suffix in ("", "-shm", "-wal", "-journal"):
        require(not path.with_name(path.name + suffix).is_symlink())
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA secure_delete=ON")
        with con:
            yield Database(con)
        require(con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0)


@contextmanager
def pg_db(dsn, *, docs):
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, connect_timeout=10, row_factory=dict_row) as con:
        if docs:
            con.execute("SET search_path TO adaptive_rag")
        # Assets use the ordinary application role's search_path, like AssetDatabase.
        yield Database(con, True)


def expired(deleted, deadline, now):
    require(all(math.isfinite(x) for x in (deleted, deadline, now)))
    require(deadline >= deleted + WEEK)
    return now >= deadline


def timestamp(text):
    date = datetime.fromisoformat(text)
    require(date.utcoffset() is not None)
    return date.timestamp()


def doc_keys(row):
    meta = json.loads(row["metadata_json"])
    require(all(meta[k] == row[k] for k in ("tenant_id", "document_id", "version")))
    keys = {_hash(row["raw_key"], "raw key")}
    if meta.get("source_object_key") is not None:
        keys.add(_hash(meta["source_object_key"], "source key"))
    figures = meta.get("parser_figure_keys", [])
    require(isinstance(figures, list))
    for key in figures:
        keys.add(_hash(key, "figure key"))
        entry = figure_binding(row["tenant_id"], row["document_id"], row["version"], row["revision_id"], key)
        keys.add(sha256(entry).hexdigest())
    return keys


def asset(row):
    body = json.loads(row["body"])
    require(all(body[k] == row[k] for k in ("tenant_id", "asset_id")))
    if "revision_id" in row.keys():
        require(row["revision_id"] == body["revision_id"])
    return body


def image_keys(body):
    return {_hash(body[k], k) for k in ("object_key", "thumbnail_key")}


def inventory(docs, images, *, get_legacy=None):
    keys = set()
    for row in docs.sql("SELECT * FROM revisions"):
        keys.update(doc_keys(row))
        if get_legacy is not None:
            meta = json.loads(row["metadata_json"])
            if meta.get("source_object_key") and "parser_figure_keys" not in meta:
                # Before figure provenance was recorded, PDFs could contain real
                # and forged markers. These may veto deletion, never authorize it.
                text = get_legacy(row["raw_key"]).decode("utf-8")
                keys.update(image.key for image in find_figures(text))
    for table in ("image_assets", "image_asset_revisions", "image_trash"):
        for row in images.sql(f"SELECT * FROM {table}"):
            keys.update(image_keys(asset(row)))
    return keys


def document_trace(value, refs):
    # Match typed structural IDs, never substrings of questions/answers.
    if isinstance(value, dict):
        for name, item in value.items():
            if name in refs and isinstance(item, str) and item in refs[name]:
                return True
            for key, plural in (("chunk_id", ("retrieved_chunk_ids", "ranked_chunk_ids", "fused_chunk_ids", "related_chunk_ids", "chunk_ids")),
                                ("parent_id", ("source_parent_ids", "parent_ids"))):
                if name in plural and isinstance(item, list) and refs[key].intersection(item):
                    return True
            if document_trace(item, refs):
                return True
    if isinstance(value, list):
        return any(document_trace(item, refs) for item in value)
    return False


def purge_docs(db, cache, now):
    due = [(r["tenant_id"], r["document_id"]) for r in db.sql("SELECT * FROM document_trash")
           if expired(timestamp(r["deleted_at"]), timestamp(r["purge_after"]), now)]
    refs = defaultdict(lambda: defaultdict(set))
    doomed_cache, kept_cache = set(), set()
    if cache and due:
        from .cloud import CloudDenseRetriever
        targets = set(due)
        for row in db.sql("SELECT tenant_id,document_id,value_json FROM children"):
            chunk = ChildChunk(**json.loads(row["value_json"]))
            key = (row["tenant_id"], row["document_id"])
            require(key == (chunk.tenant_id, chunk.document_id))
            (doomed_cache if key in targets else kept_cache).add(CloudDenseRetriever._key(chunk))
        for key in doomed_cache - kept_cache:
            cache.sql("DELETE FROM vectors WHERE cache_key=?", (key,))
    for tenant, doc in due:
        pair = tenant, doc
        where = " WHERE tenant_id=? AND document_id=?"
        # Historical published manifests lose only this document. Pending multi-document
        # candidates containing it are invalid as a whole; never activate a partial plan.
        require(not db.sql("SELECT 1 FROM snapshot_revisions JOIN current_snapshots USING(tenant_id,generation)" + where, pair).fetchone())
        db.sql("DELETE FROM candidate_snapshots WHERE tenant_id=? AND activated=0 AND generation IN "
               "(SELECT generation FROM candidate_revisions" + where + ")", (tenant, *pair))
        db.sql("DELETE FROM candidate_jobs WHERE tenant_id=? AND job_id IN (SELECT job_id FROM jobs" + where + ")", (tenant, *pair))
        for row in db.sql("SELECT * FROM revisions" + where, pair):
            db.queue(doc_keys(row))
        refs[tenant]["document_id"].add(doc)
        for table, column in (("children", "chunk_id"), ("parents", "parent_id")):
            refs[tenant][column].update(row[column] for row in db.sql(f"SELECT {column} FROM {table}" + where, pair))
        for table in ("snapshot_revisions", "jobs", "children", "parents", "revisions", "document_trash", "documents"):
            db.sql(f"DELETE FROM {table}" + where, pair)
    for tenant, ids in refs.items():
        dependents = defaultdict(set)
        doomed = set()
        for row in db.sql("SELECT trace_id,envelope_json FROM traces WHERE tenant_id=?", (tenant,)):
            trace = json.loads(row["envelope_json"])
            if document_trace(trace, ids):
                doomed.add(row["trace_id"])
            for parent in trace.get("history_trace_ids", ()):
                dependents[parent].add(row["trace_id"])
        # A follow-up can quote an old answer without retrieving its source again.
        pending = list(doomed)
        while pending:
            more = dependents.pop(pending.pop(), set()) - doomed
            doomed.update(more)
            pending.extend(more)
        for key in doomed:
            db.sql("DELETE FROM traces WHERE tenant_id=? AND trace_id=?", (tenant, key))
    return len(due)


def purge_images(db, delete_vector, now):
    removed = defaultdict(set)
    count = 0
    # asset_id can be reused while an older incarnation is in the recycle bin.
    for row in db.sql("SELECT * FROM image_trash").fetchall():
        if not expired(row["deleted_at"], row["expires_at"], now):
            continue
        body = asset(row)
        tenant, aid, tid = row["tenant_id"], row["asset_id"], row["trash_id"]
        pair = tenant, aid
        current = db.sql("SELECT * FROM image_assets WHERE tenant_id=? AND asset_id=?", pair).fetchone()
        candidates = set()
        incarnation = body.get("incarnation_id")
        def same(value):
            # Legacy images have no stable incarnation. Never infer ownership of older
            # revisions from a matching asset_id or created_at; only exact revision IDs.
            return value["revision_id"] == body["revision_id"] or (incarnation is not None and incarnation == value.get("incarnation_id"))
        other = db.sql("SELECT * FROM image_trash WHERE tenant_id=? AND asset_id=? AND trash_id<>?", (*pair, tid))
        protected = any(same(asset(r)) for r in other)
        if current:
            cur = asset(current)
            if current["deleted"] and cur.get("trash_id") == tid:
                require(same(cur))
                db.sql("DELETE FROM image_assets WHERE tenant_id=? AND asset_id=?", pair)
            elif same(cur):
                protected = True
        db.queue(image_keys(body))
        if not protected:
            candidates.add(body["revision_id"])
            for revision in db.sql("SELECT * FROM image_asset_revisions WHERE tenant_id=? AND asset_id=?", pair):
                item = asset(revision)
                if same(item):
                    candidates.add(item["revision_id"])
                    db.queue(image_keys(item))
            for rev in candidates:
                delete_vector(tenant, aid, rev)
                for table in ("image_index_jobs", "image_asset_revisions", "image_events"):
                    db.sql(f"DELETE FROM {table} WHERE tenant_id=? AND asset_id=? AND revision_id=?", (*pair, rev))
            removed[tenant].update((aid, rev) for rev in candidates)
        db.sql("DELETE FROM image_trash WHERE tenant_id=? AND trash_id=?", (tenant, tid))
        count += 1
    for tenant, pairs in removed.items():
        for row in db.sql("SELECT trace_id,body FROM image_traces WHERE tenant_id=?", (tenant,)).fetchall():
            hits = json.loads(row["body"])["hits"]
            if any((h["asset"]["asset_id"], h["asset"]["revision_id"]) in pairs for h in hits):
                db.sql("DELETE FROM image_traces WHERE tenant_id=? AND trace_id=?", (tenant, row["trace_id"]))
    return count


def _milvus_delete(index):
    # Existing public delete targets every revision of an asset, unsafe after ID reuse.
    checked = None
    def delete(tenant, aid, rev):
        nonlocal checked
        if checked is None:
            result = index._post("collections/has", {})
            require(isinstance(result, dict) and type(result.get("has")) is bool)
            checked = result["has"]
            if checked:
                desc = index._post("collections/describe", {})
                field = next(f for f in desc["fields"] if f["name"] == "vector")
                params = field["params"]
                if isinstance(params, list):
                    params = {p["key"]: p["value"] for p in params}
                index._validate_schema(desc, index.dimension or int(params["dim"]))
        if checked:
            expr = " and ".join(f"{k} == {json.dumps(v, ensure_ascii=True)}" for k, v in
                                (("tenant_id", tenant), ("asset_id", aid), ("revision_id", rev)))
            index._post("entities/delete", {"filter": expr})
    return delete


def _s3_delete(store):
    result = store.client.get_bucket_versioning(Bucket=store.bucket)
    require(result.get("ResponseMetadata", {}).get("HTTPStatusCode") == 200 and "Status" not in result)
    def delete(key):
        result = store.client.delete_object(Bucket=store.bucket, Key=store.object_key(key))
        require(result.get("ResponseMetadata", {}).get("HTTPStatusCode") in (200, 204)
                and not result.get("DeleteMarker") and not result.get("VersionId"))
    return delete


def _run_locked(root, values, now):
    from .infrastructure import load_infrastructure
    config, selectors = load_infrastructure(values)
    topology = (selectors['store'], selectors['trace'], config.asset_store, selectors['objects'], config.vector_backend)
    local = topology == ('sqlite', 'sqlite', 'sqlite', 'file', 'sqlite')
    remote = topology == ('postgres', 'postgres', 'postgres', 's3', 'milvus')
    require(local or remote)
    if local:
        docs_db = lambda: local_db(root / 'knowledge.sqlite3')
        image_db = lambda: local_db(root / 'assets.sqlite3')
        require((root / 'objects').is_dir())
        store = FileObjectStore(root)
        def delete_blob(key):
            store._path(key).unlink(missing_ok=True)
    else:
        require(values.get('RAG_ENABLE_REMOTE_STORAGE', '').lower() == 'true')
        # Mixed/migrated local catalogs in this directory are not proven independent.
        require(not any((root / f).exists() for f in ('knowledge.sqlite3', 'assets.sqlite3', 'objects', 'asset-vectors.sqlite3')))
        from .remote import MilvusVectorIndex, S3ObjectStore
        docs_db = lambda: pg_db(config.postgres_dsn, docs=True)
        image_db = lambda: pg_db(config.postgres_dsn, docs=False)
        store = S3ObjectStore(config.s3_endpoint, config.s3_access_key, config.s3_secret_key,
                              config.s3_bucket, config.s3_prefix, config.s3_region)
        delete_blob = _s3_delete(store)
        delete_vector = _milvus_delete(MilvusVectorIndex(config.milvus_endpoint, config.milvus_collection,
                                      token=config.milvus_token, dimension=config.embedding_dimensions))

    with ExitStack() as stack:
        docs = stack.enter_context(docs_db())
        images = stack.enter_context(image_db())
        require(docs.sql('SELECT version FROM schema_metadata WHERE id=1').fetchone()['version'] == STORAGE_VERSION)
        for db in (docs, images):
            db.sql(f'CREATE TABLE IF NOT EXISTS {QUEUE} (digest TEXT PRIMARY KEY)')
        if local:
            vector = stack.enter_context(local_db(root / 'asset-vectors.sqlite3'))
            def delete_vector(t, a, r):
                vector.sql('DELETE FROM asset_vectors WHERE tenant_id=? AND asset_id=? AND revision_id=?', (t, a, r))
        cache_path = root / 'cloud-vectors.sqlite3'
        require(not cache_path.is_symlink())
        cache = stack.enter_context(local_db(cache_path)) if cache_path.exists() else None
        # Missing, corrupt or inaccessible authoritative inventories abort before external effects.
        inventory(docs, images)
        nd = purge_docs(docs, cache, now)
        ni = purge_images(images, delete_vector, now)
    # Metadata and its retry queue have committed together. Recompute *all* retained
    # owners on retry, including uploads/restores since a previous failed maintenance run.
    with docs_db() as docs, image_db() as images:
        candidates = {_hash(r['digest'], 'queued key') for db in (docs, images)
                      for r in db.sql(f'SELECT digest FROM {QUEUE}')}
        retained = inventory(docs, images, get_legacy=store.get) if candidates else set()
        for key in sorted(candidates - retained):
            delete_blob(key)
        for db in (docs, images):
            db.sql(f'DELETE FROM {QUEUE}')
    return dict(documents=nd, images=ni, unshared_objects=len(candidates - retained))


def run_offline(root, values):
    from .retention_cli import directory_lock
    with directory_lock(root, writer=False):
        return _run_locked(root, values, time())
