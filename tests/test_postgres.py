"""PostgreSQL contract tests; live cases require an explicit isolated test DSN."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from uuid import uuid4

from adaptive_rag.domain import AccessContext, CapabilityError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from adaptive_rag.postgres import (
    PostgresKnowledgeStore, PostgresTraceSink, _driver, _lock_id,
    migrate_sqlite_to_postgres, validate_prepared_document,
)
from adaptive_rag.storage import SQLiteKnowledgeStore, SQLiteTraceSink
from test_storage import prepared


class PostgresConfigurationTests(unittest.TestCase):
    def test_invalid_configuration_fails_before_connecting(self):
        for dsn in (None, "", " ", "db\x00password"):
            with self.subTest(dsn=dsn), self.assertRaises(ValidationError):
                PostgresKnowledgeStore(dsn)
        for schema in ("public; DROP TABLE", "with-dash", "UPPER", "a" * 64, None):
            with self.subTest(schema=schema), self.assertRaises(ValidationError):
                PostgresKnowledgeStore("postgresql://unused", schema=schema)
        for timeout in (True, 0, 121, "10"):
            with self.subTest(timeout=timeout), self.assertRaises(ValidationError):
                PostgresKnowledgeStore("postgresql://unused", connect_timeout=timeout)

    def test_shared_prepared_validation_rejects_corrupt_content(self):
        document = prepared()
        validate_prepared_document(document)
        with self.assertRaises(ValidationError):
            validate_prepared_document(replace(document, content_hash="0" * 64))

    def test_lock_identity_is_stable_and_scoped(self):
        self.assertEqual(_lock_id("schema", "tenant"), _lock_id("schema", "tenant"))
        self.assertNotEqual(_lock_id("schema", "tenant"), _lock_id("other", "tenant"))
        self.assertTrue(-(2**63) <= _lock_id("schema", "tenant") < 2**63)

    def test_failed_connection_does_not_expose_dsn(self):
        from unittest.mock import patch
        try:
            psycopg, _, _ = _driver()
        except CapabilityError:
            self.skipTest("optional PostgreSQL driver is not installed")
        secret = "postgresql://user:never-print-this@host/database"
        with patch.object(psycopg, "connect", side_effect=psycopg.OperationalError(secret)):
            with self.assertRaises(CapabilityError) as caught:
                PostgresKnowledgeStore(secret)
        self.assertNotIn("never-print-this", str(caught.exception))


@unittest.skipUnless(os.environ.get("RAG_TEST_POSTGRES_DSN"), "set RAG_TEST_POSTGRES_DSN for isolated real PostgreSQL tests")
class PostgresLiveTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["RAG_TEST_POSTGRES_DSN"]
        self.schema = "rag_test_" + uuid4().hex
        self.store = PostgresKnowledgeStore(self.dsn, schema=self.schema)
        self.addCleanup(self.drop_schema, self.schema)
        self.access = AccessContext("tenant-a", "alice")

    def drop_schema(self, schema):
        psycopg, sql, _ = _driver()
        with psycopg.connect(self.dsn) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    def publish(self, document=None, expected=None):
        document = document or prepared()
        job = self.store.stage(document)
        return self.store.publish(document.request.tenant_id, job.job_id, expected)

    def test_stage_publish_reopen_idempotency_and_ordered_parent_reads(self):
        document = prepared(allowed=("alice", "staff"), scope="sku", sku_ids=("SKU-1", "SKU-2"))
        first = self.store.stage(document)
        self.assertIsNone(self.store.open_snapshot("tenant-a"))
        self.assertEqual(self.store.get_job("tenant-a", first.job_id)["state"], "staged")
        reordered = replace(document, request=replace(document.request, allowed_subjects=("staff", "alice", "alice"),
            sku_ids=("SKU-2", "SKU-1")), children=(replace(document.children[0], sku_ids=("SKU-2", "SKU-1")),))
        self.assertTrue(self.store.stage(reordered).duplicate)
        snapshot = self.store.publish("tenant-a", first.job_id, None)
        reopened = PostgresKnowledgeStore(self.dsn, schema=self.schema)
        self.assertEqual(reopened.open_snapshot("tenant-a"), snapshot)
        self.assertEqual(reopened.list_chunks(snapshot, self.access), ())
        self.assertEqual(reopened.list_chunks(snapshot, self.access, "SKU-1"), document.children)
        self.assertEqual(reopened.get_parents(snapshot, [document.parents[0].parent_id] * 2, self.access), document.parents)
        self.assertEqual(self.store.publish("tenant-a", first.job_id, snapshot.generation), snapshot)
        self.assertEqual(self.store.stage(document).state, "published")
        self.assertNotIn(self.dsn, repr(reopened))

    def test_concurrent_first_publish_has_exactly_one_cas_winner(self):
        jobs = [self.store.stage(prepared(doc=doc)) for doc in ("first", "second")]
        gate = Barrier(2)
        def run(job):
            gate.wait(timeout=10)
            try:
                return self.store.publish("tenant-a", job.job_id, None)
            except ConflictError as error:
                return error
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(run, jobs))
        self.assertEqual(sum(isinstance(v, ConflictError) for v in outcomes), 1)
        loser = jobs[next(i for i, v in enumerate(outcomes) if isinstance(v, ConflictError))]
        current = self.store.open_snapshot("tenant-a")
        final = self.store.publish("tenant-a", loser.job_id, current.generation)
        self.assertEqual(len(final.revisions), 2)

    def test_concurrent_schema_initialization_is_serialized(self):
        fresh = "rag_test_" + uuid4().hex
        self.addCleanup(self.drop_schema, fresh)
        gate = Barrier(2)
        def initialize(_):
            gate.wait(timeout=10)
            return PostgresKnowledgeStore(self.dsn, schema=fresh).open_snapshot("tenant-a")
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(list(executor.map(initialize, range(2))), [None, None])

    def test_access_revoke_delete_rollback_and_tenant_boundary(self):
        document = prepared(allowed=("staff",))
        old = self.publish(document)
        member = AccessContext("tenant-a", "bob", ("staff",))
        self.assertEqual(self.store.list_chunks(old, self.access), ())
        self.assertEqual(self.store.list_chunks(old, member), document.children)
        self.store.set_access("tenant-a", "shipping", ("alice",))
        self.assertEqual(self.store.list_chunks(old, self.access), document.children)
        self.assertEqual(self.store.list_chunks(old, member), ())
        self.store.stage(document)
        self.assertEqual(self.store.list_chunks(old, member), ())
        with self.assertRaises(ForbiddenError):
            self.store.list_chunks(old, AccessContext("tenant-b"))
        with self.assertRaises(NotFoundError):
            self.store.open_snapshot("tenant-b", old.generation)
        deleted = self.store.delete_document("tenant-a", "shipping", old.generation)
        self.assertEqual(deleted.revisions, ())
        self.assertEqual(self.store.list_chunks(old, self.access), ())
        rolled = self.store.rollback("tenant-a", old.generation, deleted.generation)
        self.assertEqual(rolled.revisions, ())
        self.assertEqual(self.store.list_chunks(rolled, self.access), ())
        self.store.set_access("tenant-a", "shipping", (), revoked=False)
        self.assertEqual(self.store.list_chunks(rolled, self.access), ())
        self.assertEqual(self.store.list_chunks(old, self.access), ())

    def test_reprocessing_and_forged_snapshot_cannot_read_unpublished_text(self):
        first = prepared()
        old = self.publish(first)
        second = prepared(version="v2", text="Unpublished private text.")
        job = self.store.stage(second)
        for forged in (replace(old, revisions=(("shipping", second.revision_id),)), replace(old, created_at="forged")):
            with self.assertRaises(ForbiddenError):
                self.store.list_chunks(forged, self.access)
            with self.assertRaises(ForbiddenError):
                self.store.get_parents(forged, [second.parents[0].parent_id], self.access)
        published = self.store.publish("tenant-a", job.job_id, old.generation)
        self.assertEqual(self.store.list_chunks(old, self.access), first.children)
        self.assertEqual(self.store.list_chunks(published, self.access), second.children)

    def test_invalid_prepared_content_collision_and_failed_publish_leave_state_unchanged(self):
        original = prepared()
        with self.assertRaises(ValidationError):
            self.store.stage(replace(original, content_hash="0" * 64))
        self.assertIsNone(self.store.open_snapshot("tenant-a"))
        old = self.publish(original)
        with self.assertRaises(ConflictError):
            self.store.stage(replace(original, processing_fingerprint="changed"))
        changed = prepared(doc="different")
        changed = replace(changed, parents=(replace(changed.parents[0], parent_id=original.parents[0].parent_id),),
            children=(replace(changed.children[0], parent_id=original.parents[0].parent_id),))
        with self.assertRaises(ConflictError):
            self.store.stage(changed)
        with self.assertRaises(NotFoundError):
            self.store.set_access("tenant-a", "different", ())
        with self.assertRaises(ConflictError):
            self.store.publish("tenant-a", self.store.stage(original).job_id, None)
        with self.assertRaises(NotFoundError):
            self.store.publish("tenant-a", "missing", old.generation)
        self.assertEqual(self.store.open_snapshot("tenant-a"), old)

    def test_immutable_trace_and_secret_field_policy(self):
        sink = PostgresTraceSink(self.dsn, schema=self.schema)
        trace = {"tenant_id": "tenant-a", "trace_id": "t1", "stages": [{"name": "检索", "duration_ms": 3}]}
        sink.save(trace)
        sink.save(trace)
        self.assertEqual(PostgresTraceSink(self.dsn, schema=self.schema).get("tenant-a", "t1"), trace)
        trace["stages"][0]["duration_ms"] = 9
        with self.assertRaises(ConflictError):
            sink.save(trace)
        with self.assertRaises(NotFoundError):
            sink.get("tenant-b", "t1")
        with self.assertRaises(ValidationError):
            sink.save({"tenant_id": "tenant-a", "trace_id": "bad", "nested": {"API_Key": "private"}})

    def test_explicit_migration_preserves_revisions_snapshots_acl_and_traces(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = SQLiteKnowledgeStore(root)
            first = prepared()
            first_job = original.stage(first)
            old = original.publish("tenant-a", first_job.job_id, None)
            second = prepared(doc="second", text="退款期限为三十天。")
            job = original.stage(second)
            current = original.publish("tenant-a", job.job_id, old.generation)
            original.set_access("tenant-a", "second", ("bob",))
            trace = {"tenant_id": "tenant-a", "trace_id": "saved", "stages": []}
            SQLiteTraceSink(root).save(trace)
            counts = migrate_sqlite_to_postgres(root / "knowledge.sqlite3", self.dsn, schema=self.schema)
            self.assertEqual(counts["revisions"], 2)
            self.assertEqual(self.store.open_snapshot("tenant-a"), current)
            self.assertEqual(self.store.open_snapshot("tenant-a", old.generation), old)
            self.assertEqual(self.store.list_chunks(current, self.access), first.children)
            self.assertEqual(PostgresTraceSink(self.dsn, schema=self.schema).get("tenant-a", "saved"), trace)
            with self.assertRaises(ConflictError):
                migrate_sqlite_to_postgres(root / "knowledge.sqlite3", self.dsn, schema=self.schema)
            self.assertEqual(original.open_snapshot("tenant-a"), current)

    def test_migration_failure_rolls_back_all_target_rows(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = SQLiteKnowledgeStore(root)
            source.stage(prepared())
            # Force an error after documents/revisions/chunks have been copied.
            with self.store._connection() as connection:
                connection.execute("ALTER TABLE jobs ADD CONSTRAINT test_fail_staged CHECK (state='published')")
            with self.assertRaises(ConflictError):
                migrate_sqlite_to_postgres(root / "knowledge.sqlite3", self.dsn, schema=self.schema)
            self.assertIsNone(self.store.open_snapshot("tenant-a"))
            with self.store._connection() as connection:
                for table in ("documents", "revisions", "parents", "children", "jobs"):
                    self.assertEqual(connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
