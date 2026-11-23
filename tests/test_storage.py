"""Persistence contract tests: these exercise real SQLite and filesystem I/O."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from adaptive_rag.domain import (
    AccessContext, ChildChunk, ConflictError, ForbiddenError, IngestRequest,
    NotFoundError, ParentChunk, PreparedDocument, ValidationError, stable_id,
)
from adaptive_rag.storage import FileObjectStore, SQLiteKnowledgeStore, SQLiteTraceSink


def prepared(*, tenant="tenant-a", doc="shipping", version="v1",
             text="Orders ship within 3 business days.", fingerprint="parser-v1",
             allowed=(), scope="store", sku_ids=()):
    request = IngestRequest(tenant, doc, version, text, title=doc,
                            allowed_subjects=allowed, scope=scope, sku_ids=sku_ids)
    digest = sha256(text.encode()).hexdigest()
    revision = stable_id(tenant, doc, version, digest, fingerprint, allowed, scope, sku_ids)
    parent_id = stable_id("parent", tenant, doc, revision)
    parent = ParentChunk(tenant, doc, version, revision, parent_id,
                         ("Policies",), text, "section:1", digest)
    child = ChildChunk(tenant, doc, version, revision,
                       stable_id("child", tenant, doc, revision), parent_id,
                       text, "Policies\n" + text, "section:1#child:1", scope, sku_ids, "zh")
    return PreparedDocument(request, revision, fingerprint, digest, digest, (parent,), (child,))


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = SQLiteKnowledgeStore(self.root)
        self.access = AccessContext("tenant-a", "alice")

    def publish(self, document=None, expected=None):
        document = document or prepared()
        job = self.store.stage(document)
        return self.store.publish(document.request.tenant_id, job.job_id, expected)

    def test_stage_is_not_searchable_until_publish_and_survives_restart(self):
        document = prepared()
        result = self.store.stage(document)
        self.assertEqual(result.state, "staged")
        self.assertFalse(result.duplicate)
        self.assertIsNone(self.store.open_snapshot("tenant-a"))
        self.assertEqual(self.store.get_job("tenant-a", result.job_id)["state"], "staged")
        snapshot = self.store.publish("tenant-a", result.job_id, None)
        restarted = SQLiteKnowledgeStore(self.root)
        self.assertEqual(restarted.open_snapshot("tenant-a"), snapshot)
        self.assertEqual(restarted.list_chunks(snapshot, self.access), document.children)
        self.assertEqual(restarted.get_parents(snapshot, [document.parents[0].parent_id], self.access), document.parents)
        self.assertEqual(restarted.get_job("tenant-a", result.job_id)["state"], "published")

    def test_duplicate_stage_and_publish_are_idempotent_at_current_generation(self):
        document = prepared()
        first = self.store.stage(document)
        repeated = self.store.stage(document)
        self.assertTrue(repeated.duplicate)
        self.assertEqual(repeated.job_id, first.job_id)
        snapshot = self.store.publish("tenant-a", first.job_id, None)
        self.assertEqual(self.store.publish("tenant-a", first.job_id, snapshot.generation), snapshot)
        self.assertEqual(self.store.stage(document).state, "published")

    def test_duplicate_metadata_sets_ignore_order(self):
        document = prepared(allowed=("alice", "staff"), scope="sku", sku_ids=("SKU-1", "SKU-2"))
        first = self.store.stage(document)
        reordered = replace(
            document,
            request=replace(document.request, allowed_subjects=("staff", "alice", "alice"),
                            sku_ids=("SKU-2", "SKU-1")),
            children=(replace(document.children[0], sku_ids=("SKU-2", "SKU-1")),),
        )
        result = self.store.stage(reordered)
        self.assertEqual(first.job_id, result.job_id)
        self.assertTrue(result.duplicate)

    def test_reprocessing_same_source_version_preserves_old_snapshot(self):
        first = prepared()
        old = self.publish(first)
        rebuilt = prepared(fingerprint="parser-v2")
        new = self.publish(rebuilt, old.generation)
        self.assertNotEqual(first.revision_id, rebuilt.revision_id)
        self.assertEqual(self.store.list_chunks(old, self.access), first.children)
        self.assertEqual(self.store.list_chunks(new, self.access), rebuilt.children)
        self.assertEqual(self.store.open_snapshot("tenant-a", old.generation), old)

    def test_publish_merges_other_documents(self):
        first, second = prepared(), prepared(doc="refund", text="Refunds require the receipt.")
        old = self.publish(first)
        new = self.publish(second, old.generation)
        self.assertEqual(dict(new.revisions), {first.request.document_id: first.revision_id,
                                               second.request.document_id: second.revision_id})
        self.assertEqual(len(self.store.list_chunks(new, self.access)), 2)

    def test_cas_conflict_and_missing_job_do_not_change_pointer(self):
        old = self.publish()
        second = self.store.stage(prepared(doc="second"))
        with self.assertRaises(ConflictError):
            self.store.publish("tenant-a", second.job_id, None)
        with self.assertRaises(NotFoundError):
            self.store.publish("tenant-a", "missing", old.generation)
        self.assertEqual(self.store.open_snapshot("tenant-a"), old)
        self.assertEqual(self.store.get_job("tenant-a", second.job_id)["state"], "staged")

    def test_concurrent_publish_cas_then_retry_keeps_both_updates(self):
        jobs = [self.store.stage(prepared(doc=doc)) for doc in ("one", "two")]
        gate = Barrier(2)

        def publish(job):
            gate.wait(timeout=5)
            try:
                return self.store.publish("tenant-a", job.job_id, None)
            except ConflictError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(publish, jobs))
        self.assertEqual(sum(isinstance(value, ConflictError) for value in outcomes), 1)
        current = self.store.open_snapshot("tenant-a")
        failed = jobs[next(index for index, outcome in enumerate(outcomes) if isinstance(outcome, ConflictError))]
        final = self.store.publish("tenant-a", failed.job_id, current.generation)
        self.assertEqual(len(final.revisions), 2)

    def test_live_acl_subject_group_revoke_and_duplicate_stage(self):
        document = prepared(allowed=("staff",))
        snapshot = self.publish(document)
        self.assertEqual(self.store.list_chunks(snapshot, self.access), ())
        member = AccessContext("tenant-a", "bob", ("staff",))
        self.assertEqual(self.store.list_chunks(snapshot, member), document.children)
        self.store.set_access("tenant-a", "shipping", ("alice",))
        self.assertEqual(self.store.list_chunks(snapshot, self.access), document.children)
        self.assertEqual(self.store.list_chunks(snapshot, member), ())
        self.store.stage(document)
        self.assertEqual(self.store.list_chunks(snapshot, self.access), document.children)
        self.store.set_access("tenant-a", "shipping", (), revoked=True)
        self.store.stage(document)
        self.assertEqual(self.store.list_chunks(snapshot, self.access), ())
        self.assertEqual(self.store.get_parents(snapshot, [document.parents[0].parent_id], member), ())

    def test_new_revision_does_not_reset_live_access(self):
        old = self.publish()
        self.store.set_access("tenant-a", "shipping", (), revoked=True)
        new = self.publish(prepared(version="v2", text="Updated shipping terms."), old.generation)
        self.assertEqual(self.store.list_chunks(new, self.access), ())

    def test_store_scope_and_sku_scope(self):
        store_doc = prepared()
        first = self.publish(store_doc)
        sku_doc = prepared(doc="sizing", scope="sku", sku_ids=("SKU-1", "SKU-2"))
        snapshot = self.publish(sku_doc, first.generation)
        self.assertEqual(self.store.list_chunks(snapshot, self.access), store_doc.children)
        self.assertEqual(self.store.list_chunks(snapshot, self.access, "OTHER"), store_doc.children)
        self.assertEqual(len(self.store.list_chunks(snapshot, self.access, "SKU-1")), 2)

    def test_delete_revoke_old_snapshots_and_rollback_does_not_restore_access(self):
        document = prepared()
        old = self.publish(document)
        deleted = self.store.delete_document("tenant-a", "shipping", old.generation)
        self.assertEqual(deleted.revisions, ())
        self.assertEqual(self.store.list_chunks(old, self.access), ())
        rolled = self.store.rollback("tenant-a", old.generation, deleted.generation)
        self.assertNotEqual(rolled.generation, old.generation)
        self.assertEqual(rolled.revisions, ())
        self.assertEqual(self.store.list_chunks(rolled, self.access), ())
        self.store.set_access("tenant-a", "shipping", (), revoked=False)
        self.assertEqual(self.store.list_chunks(rolled, self.access), ())
        self.assertEqual(self.store.list_chunks(old, self.access), ())

    def test_delete_and_rollback_validate_cas_and_target(self):
        current = self.publish()
        for action in (
            lambda: self.store.delete_document("tenant-a", "shipping", None),
            lambda: self.store.rollback("tenant-a", current.generation, None),
        ):
            with self.assertRaises(ConflictError):
                action()
        with self.assertRaises(NotFoundError):
            self.store.delete_document("tenant-a", "missing", current.generation)
        with self.assertRaises(NotFoundError):
            self.store.rollback("tenant-a", "missing", current.generation)
        self.assertEqual(self.store.open_snapshot("tenant-a"), current)

    def test_tenant_isolation_and_parent_ids_are_scoped_to_published_revision(self):
        first_doc = prepared()
        first = self.publish(first_doc)
        other_doc = prepared(tenant="tenant-b")
        other = self.publish(other_doc)
        staged = prepared(doc="staged")
        self.store.stage(staged)
        wrong_parents = [other_doc.parents[0].parent_id, staged.parents[0].parent_id]
        self.assertEqual(self.store.get_parents(first, wrong_parents, self.access), ())
        with self.assertRaises(ForbiddenError):
            self.store.list_chunks(other, self.access)
        with self.assertRaises(ForbiddenError):
            self.store.get_parents(first, [first_doc.parents[0].parent_id], AccessContext("tenant-b"))
        with self.assertRaises(NotFoundError):
            self.store.open_snapshot("tenant-b", first.generation)
        with self.assertRaises(NotFoundError):
            self.store.get_job("tenant-b", self.store.stage(first_doc).job_id)

    def test_forged_snapshot_cannot_read_unpublished_revision(self):
        old = self.publish()
        staged = prepared(version="v2", text="SECRET unpublished text")
        self.store.stage(staged)
        for forged in (replace(old, revisions=(("shipping", staged.revision_id),)),
                       replace(old, created_at="forged")):
            with self.assertRaises(ForbiddenError):
                self.store.list_chunks(forged, self.access)
            with self.assertRaises(ForbiddenError):
                self.store.get_parents(forged, [staged.parents[0].parent_id], self.access)

    def test_get_parents_keeps_requested_order_and_deduplicates(self):
        first_doc, second_doc = prepared(), prepared(doc="second")
        first = self.publish(first_doc)
        snapshot = self.publish(second_doc, first.generation)
        ids = [second_doc.parents[0].parent_id, first_doc.parents[0].parent_id]
        self.assertEqual(self.store.get_parents(snapshot, ids + ids, self.access),
                         (second_doc.parents[0], first_doc.parents[0]))

    def test_invalid_prepared_documents_leave_no_jobs_revisions_or_pointer(self):
        valid = prepared()
        parent, child = valid.parents[0], valid.children[0]
        invalid = [
            replace(valid, content_hash="0" * 64),
            replace(valid, raw_key="0" * 64),
            replace(valid, parents=()),
            replace(valid, children=()),
            replace(valid, parents=(parent, parent)),
            replace(valid, children=(child, child)),
            replace(valid, parents=(replace(parent, tenant_id="tenant-b"),)),
            replace(valid, parents=(replace(parent, content_hash="0" * 64),)),
            replace(valid, children=(replace(child, parent_id="missing"),)),
            replace(valid, children=(replace(child, document_id="wrong"),)),
            replace(valid, children=(replace(child, text="not part of the parent"),)),
            replace(valid, children=(replace(child, retrieval_text="unrelated text"),)),
            replace(valid, children=(replace(child, scope="sku", sku_ids=("SKU-1",)),)),
        ]
        for document in invalid:
            with self.subTest(document=document), self.assertRaises(ValidationError):
                self.store.stage(document)
        with closing(sqlite3.connect(self.root / "knowledge.sqlite3")) as connection:
            for table in ("jobs", "revisions", "parents", "children", "documents"):
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertIsNone(self.store.open_snapshot("tenant-a"))

    def test_revision_collision_and_chunk_identity_collision_roll_back(self):
        original = prepared()
        snapshot = self.publish(original)
        with self.assertRaises(ConflictError):
            self.store.stage(replace(original, processing_fingerprint="changed"))
        changed = prepared(doc="different")
        changed = replace(changed, parents=(replace(changed.parents[0], parent_id=original.parents[0].parent_id),),
                          children=(replace(changed.children[0], parent_id=original.parents[0].parent_id),))
        with self.assertRaises(ConflictError):
            self.store.stage(changed)
        with closing(sqlite3.connect(self.root / "knowledge.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM documents").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        self.assertEqual(self.store.open_snapshot("tenant-a"), snapshot)

    def test_access_update_validates_inputs_and_missing_document(self):
        with self.assertRaises(NotFoundError):
            self.store.set_access("tenant-a", "missing", ())
        with self.assertRaises(ValidationError):
            self.store.set_access("tenant-a", "shipping", ["alice"])
        with self.assertRaises(ValidationError):
            self.store.set_access("tenant-a", "shipping", (), revoked=1)


class FileObjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = FileObjectStore(self.root)

    def test_content_addressed_idempotent_and_persistent(self):
        content = "业务知识\nContent".encode()
        key = self.store.put(content)
        self.assertEqual(key, sha256(content).hexdigest())
        self.assertEqual(self.store.put(content), key)
        self.assertEqual(FileObjectStore(self.root).get(key), content)
        self.assertEqual(self.store.get(self.store.put(b"")), b"")
        self.assertFalse(list(self.root.rglob(".pending-*")))

    def test_rejects_invalid_keys_and_non_bytes(self):
        for value in ("../secret", "a" * 63, "A" * 64, "/etc/passwd", None):
            with self.subTest(key=value), self.assertRaises(ValidationError):
                self.store.get(value)
        with self.assertRaises(ValidationError):
            self.store.put("text")
        with self.assertRaises(NotFoundError):
            self.store.get("0" * 64)

    def test_corruption_is_detected_on_read_and_duplicate_write(self):
        key = self.store.put(b"correct")
        path = self.root / "objects" / key[:2] / key
        path.write_bytes(b"damaged")
        with self.assertRaises(ConflictError):
            self.store.get(key)
        with self.assertRaises(ConflictError):
            self.store.put(b"correct")

    def test_symbolic_link_object_or_bucket_cannot_escape_root(self):
        with TemporaryDirectory() as outside:
            key = sha256(b"outside").hexdigest()
            target = Path(outside) / "source"
            target.write_bytes(b"outside")
            bucket = self.root / "objects" / key[:2]
            bucket.mkdir()
            try:
                (bucket / key).symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable for this Windows account")
            with self.assertRaises(ValidationError):
                self.store.get(key)
            with self.assertRaises(ValidationError):
                self.store.put(b"outside")
            (bucket / key).unlink()
            bucket.rmdir()
            bucket.symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaises(ValidationError):
                self.store.get(key)
            bucket.unlink()


class TraceSinkTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sink = SQLiteTraceSink(self.root)

    def test_trace_persistence_idempotency_isolation_and_json_snapshot(self):
        trace = {"tenant_id": "tenant-a", "trace_id": "trace-1",
                 "model_calls": 0, "stages": [{"name": "retrieve", "chunk_ids": ["one"]}]}
        self.sink.save(trace)
        self.sink.save(trace)
        self.assertEqual(SQLiteTraceSink(self.root).get("tenant-a", "trace-1"), trace)
        trace["stages"][0]["chunk_ids"].append("two")
        self.assertEqual(self.sink.get("tenant-a", "trace-1")["stages"][0]["chunk_ids"], ["one"])
        with self.assertRaises(ConflictError):
            self.sink.save(trace)
        with self.assertRaises(NotFoundError):
            self.sink.get("tenant-b", "trace-1")
        other = dict(trace, tenant_id="tenant-b")
        self.sink.save(other)
        self.assertEqual(self.sink.get("tenant-b", "trace-1"), other)

    def test_trace_secret_keys_invalid_json_and_missing_identity_rejected(self):
        for value in (
            {},
            {"tenant_id": "tenant-a"},
            {"tenant_id": "tenant-a", "trace_id": "t", "data": {"API_Key": "private"}},
            {"tenant_id": "tenant-a", "trace_id": "t", "data": {"openai_api_key": "private"}},
            {"tenant_id": "tenant-a", "trace_id": "t", "data": [{"Authorization": "private"}]},
            {"tenant_id": "tenant-a", "trace_id": "t", "duration": float("nan")},
        ):
            with self.subTest(trace=value), self.assertRaises(ValidationError):
                self.sink.save(value)
        with self.assertRaises(NotFoundError):
            self.sink.get("tenant-a", "t")


if __name__ == "__main__":
    unittest.main()
