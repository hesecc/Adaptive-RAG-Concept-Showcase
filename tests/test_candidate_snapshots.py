"""One candidate lifecycle contract for SQLite and isolated, opt-in PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from adaptive_rag.app import RagApplication
from adaptive_rag.config import Settings
from adaptive_rag.domain import AccessContext, ConflictError, ForbiddenError, NotFoundError, ValidationError
from adaptive_rag.pipeline import make_evidence
from adaptive_rag.postgres import PostgresKnowledgeStore, _driver, migrate_sqlite_to_postgres
from adaptive_rag.storage import SQLiteKnowledgeStore
from test_storage import prepared


class CandidateContract:
    tenant = "tenant-a"
    access = AccessContext(tenant, "alice")

    def stage(self, **kwargs):
        doc = prepared(**kwargs)
        return doc, self.store.stage(doc).job_id

    def test_hidden_until_activation_and_historical_afterwards(self):
        t, store = self.tenant, self.store
        original, job = self.stage()
        base = store.publish(t, job, None)
        replacement, first = self.stage(version="v2")
        restricted, second = self.stage(doc="private", allowed=("bob",))
        candidate = store.create_candidate_snapshot(t, [first, second], base.generation)
        g = candidate.generation
        # Reopen through an independent adapter and retry with a reordered batch.
        store = self.reopen()
        self.assertEqual(store.create_candidate_snapshot(t, [second, first], base.generation), candidate)
        self.assertTrue(store.is_candidate_snapshot(t, g))
        self.assertEqual(store.open_snapshot(t), base)
        self.assertEqual(store.get_job(t, first)["state"], "staged")
        self.assertEqual(store.get_job(t, second)["state"], "staged")
        self.assertEqual(store.list_chunks(base, self.access), original.children)
        with self.assertRaises(NotFoundError):
            store.open_snapshot(t, g)
        with self.assertRaises(NotFoundError):
            store.list_chunks(candidate, self.access)
        parents = [replacement.parents[0].parent_id, restricted.parents[0].parent_id]
        with self.assertRaises(NotFoundError):
            store.get_parents(candidate, parents, self.access)
        self.assertEqual(store.open_snapshot(t, g, allow_candidate=True), candidate)
        self.assertEqual(store.list_chunks(candidate, self.access, allow_candidate=True), replacement.children)
        self.assertEqual(store.get_parents(candidate, parents, self.access, allow_candidate=True), replacement.parents)
        with self.assertRaises(NotFoundError):
            store.rollback(t, g, base.generation)
        for job_id in (first, second):
            with self.assertRaises(ConflictError):
                store.publish(t, job_id, base.generation)
        self.assertEqual(store.activate_candidate_snapshot(t, g, base.generation), candidate)
        self.assertEqual(store.activate_candidate_snapshot(t, g, base.generation), candidate)
        self.assertFalse(store.is_candidate_snapshot(t, g))
        self.assertEqual(store.open_snapshot(t), candidate)
        self.assertEqual(store.get_job(t, first)["state"], "published")
        self.assertEqual(store.get_job(t, second)["state"], "published")
        self.assertEqual(store.get_parents(candidate, parents, self.access), replacement.parents)
        self.assertEqual(store.open_snapshot(t, base.generation), base)
        reverted = store.rollback(t, base.generation, g)
        self.assertEqual(store.open_snapshot(t, g), candidate)
        with self.assertRaises(ConflictError):
            store.activate_candidate_snapshot(t, g, base.generation)
        self.assertEqual(store.open_snapshot(t), reverted)

    def test_validation_rejects_entire_batch_and_is_tenant_scoped(self):
        t, store = self.tenant, self.store
        _, first = self.stage()
        _, second = self.stage(version="v2")
        _, other = self.stage(tenant="tenant-b")
        for batch, exc in (([], ValidationError), (first, ValidationError), ([first, first], ValidationError),
                           ([first, second], ValidationError), ([first, other], NotFoundError)):
            with self.subTest(batch=batch), self.assertRaises(exc):
                store.create_candidate_snapshot(t, batch, None)
        current = store.publish(t, first, None)
        with self.assertRaises(ConflictError):
            store.create_candidate_snapshot(t, [first], current.generation)
        with self.assertRaises(ConflictError):
            store.create_candidate_snapshot(t, [second], None)
        candidate = store.create_candidate_snapshot(t, [second], current.generation)
        g = candidate.generation
        self.assertIsNone(store.open_snapshot("tenant-b"))
        self.assertFalse(store.is_candidate_snapshot("tenant-b", g))
        with self.assertRaises(NotFoundError):
            store.open_snapshot("tenant-b", g, allow_candidate=True)
        with self.assertRaises(NotFoundError):
            store.activate_candidate_snapshot("tenant-b", g, None)
        with self.assertRaises(ForbiddenError):
            store.list_chunks(candidate, AccessContext("tenant-b"), allow_candidate=True)
        with self.assertRaises(NotFoundError):
            store.activate_candidate_snapshot(t, current.generation, current.generation)

    def test_competing_first_activations_and_explicit_rebase(self):
        t, store = self.tenant, self.store
        candidates = []
        for doc in ("one", "two"):
            _, job = self.stage(doc=doc)
            candidates.append((store.create_candidate_snapshot(t, [job], None), job))
        barrier = Barrier(2)

        def activate(item):
            barrier.wait(timeout=10)
            try:
                return store.activate_candidate_snapshot(t, item[0].generation, None)
            except ConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(activate, candidates))
        self.assertEqual(results.count(None), 1)
        winner = store.open_snapshot(t)
        loser, job = candidates[results.index(None)]
        with self.assertRaises(ConflictError):
            store.activate_candidate_snapshot(t, loser.generation, winner.generation)
        self.assertEqual(store.get_job(t, job)["state"], "staged")
        self.assertTrue(store.is_candidate_snapshot(t, loser.generation))
        rebased = store.create_candidate_snapshot(t, [job], winner.generation)
        self.assertEqual(set(rebased.revisions), set(winner.revisions + loser.revisions))
        self.assertEqual(store.activate_candidate_snapshot(t, rebased.generation, winner.generation), rebased)

    def test_legacy_reader_sees_no_pending_rows_and_abandon_releases_stale_job(self):
        t, store = self.tenant, self.store
        _, job = self.stage()
        old = store.create_candidate_snapshot(t, [job], None)
        with store._connection() as connection:
            for table in ("snapshots", "snapshot_revisions", "current_snapshots"):
                self.assertIsNone(connection.execute(f"SELECT 1 FROM {table}").fetchone(), table)
        self.assertFalse(store.abandon_candidate_snapshot("tenant-b", old.generation))
        self.assertTrue(store.abandon_candidate_snapshot(t, old.generation))
        replacement = store.create_candidate_snapshot(t, [job], None)
        self.assertNotEqual(old.generation, replacement.generation)
        self.assertFalse(store.abandon_candidate_snapshot(t, old.generation))
        with self.assertRaises(NotFoundError):
            store.activate_candidate_snapshot(t, old.generation, None)
        _, other = self.stage(doc="other")
        base = store.publish(t, other, None)
        self.assertTrue(store.abandon_candidate_snapshot(t, replacement.generation))
        self.assertEqual(store.open_snapshot(t), base)
        self.assertEqual(store.get_job(t, job)["state"], "staged")
        with self.assertRaises(NotFoundError):
            store.open_snapshot(t, replacement.generation, allow_candidate=True)
        self.assertEqual(len(store.publish(t, job, base.generation).revisions), 2)

    def test_trusted_prewarm_includes_all_scopes_but_never_revocations_or_forged_manifests(self):
        t, store = self.tenant, self.store
        public, p = self.stage(doc="public")
        sku, s = self.stage(doc="sku", scope="sku", sku_ids=("SKU-1",))
        group, g = self.stage(doc="restricted", allowed=("other",))
        _, r = self.stage(doc="removed")
        self.stage(tenant="tenant-b", doc="public")
        candidate = store.create_candidate_snapshot(t, [s, g, p, r], None)
        store.set_access(t, group.request.document_id, data_domains=("finance",))
        store.set_access(t, "removed", revoked=True)
        with self.assertRaises(NotFoundError):
            store.list_prewarm_chunks(candidate)
        for forged in (replace(candidate, revisions=()), replace(candidate, created_at="forged")):
            with self.assertRaises(ForbiddenError):
                store.list_prewarm_chunks(forged, allow_candidate=True)
        expected = set(c.chunk_id for doc in (sku, public, group) for c in doc.children)
        self.assertEqual({c.chunk_id for c in store.list_prewarm_chunks(candidate, allow_candidate=True)}, expected)
        self.assertEqual(store.list_chunks(candidate, self.access, allow_candidate=True), public.children)
        self.assertTrue(store.check_prewarm_access(t, ["restricted", "sku"], access=None))
        self.assertFalse(store.check_prewarm_access(t, ["restricted"], access=self.access))
        self.assertFalse(store.check_prewarm_access(t, ["removed"], access=None))
        self.assertFalse(store.check_prewarm_access(t, ["unknown"], access=None))
        with self.assertRaises(ForbiddenError):
            store.check_prewarm_access(t, ["public"], access=AccessContext("tenant-b"))
        store.activate_candidate_snapshot(t, candidate.generation, None)
        with self.assertRaises(ConflictError):
            store.abandon_candidate_snapshot(t, candidate.generation)
        self.assertEqual({c.chunk_id for c in store.list_prewarm_chunks(candidate)}, expected)

    def test_normal_evidence_lookup_cannot_read_candidate_parent(self):
        t, store = self.tenant, self.store
        doc, job = self.stage()
        candidate = store.create_candidate_snapshot(t, [job], None)
        evidence = make_evidence(doc.parents[0], candidate.generation, 12)
        with TemporaryDirectory() as directory:
            app = RagApplication(Settings(data_dir=Path(directory)))
            app.store = store
            with self.assertRaises(NotFoundError):
                app.get_evidence(self.access, candidate.generation, evidence.evidence_id)
            store.activate_candidate_snapshot(t, candidate.generation, None)
            self.assertEqual(app.get_evidence(self.access, candidate.generation, evidence.evidence_id), evidence)

    def test_interrupted_creation_leaves_no_manifest_and_can_be_retried(self):
        t, store = self.tenant, self.store
        _, job = self.stage()
        generation = None
        write = store._snapshot_write

        @contextmanager
        def interrupt(tenant):
            nonlocal generation
            with write(tenant) as connection:
                yield connection
                row = connection.execute("SELECT generation FROM candidate_snapshots").fetchone()
                generation = row["generation"]
                raise RuntimeError("simulated disconnect before commit")

        with patch.object(store, "_snapshot_write", interrupt), self.assertRaisesRegex(RuntimeError, "disconnect"):
            store.create_candidate_snapshot(t, [job], None)
        reader = self.reopen()
        with self.assertRaises(NotFoundError):
            reader.open_snapshot(t, generation, allow_candidate=True)
        self.assertIsNone(reader.open_snapshot(t))
        candidate = reader.create_candidate_snapshot(t, [job], None)
        self.assertTrue(reader.is_candidate_snapshot(t, candidate.generation))
        self.assertEqual(reader.get_job(t, job)["state"], "staged")

    def test_interruption_after_pointer_write_rolls_back_jobs_and_visibility(self):
        t, store = self.tenant, self.store
        jobs = [self.stage(doc=name)[1] for name in ("a", "b")]
        candidate = store.create_candidate_snapshot(t, jobs, None)
        reader = self.reopen()
        set_current = store._set_current

        def interrupt(connection, tenant, generation):
            set_current(connection, tenant, generation)
            self.assertIsNone(reader.open_snapshot(t))
            self.assertTrue(reader.is_candidate_snapshot(t, generation))
            self.assertEqual([reader.get_job(t, j)["state"] for j in jobs], ["staged"] * 2)
            raise RuntimeError("simulated disconnect before commit")

        with patch.object(store, "_set_current", side_effect=interrupt), self.assertRaisesRegex(RuntimeError, "disconnect"):
            store.activate_candidate_snapshot(t, candidate.generation, None)
        self.assertIsNone(reader.open_snapshot(t))
        self.assertTrue(reader.is_candidate_snapshot(t, candidate.generation))
        self.assertEqual([reader.get_job(t, j)["state"] for j in jobs], ["staged"] * 2)
        self.assertEqual(reader.activate_candidate_snapshot(t, candidate.generation, None), candidate)
        self.assertEqual([store.get_job(t, j)["state"] for j in jobs], ["published"] * 2)


class SQLiteCandidates(CandidateContract, unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.reopen = lambda: SQLiteKnowledgeStore(Path(directory.name))
        self.store = self.reopen()


@unittest.skipUnless(os.environ.get("RAG_TEST_POSTGRES_DSN"), "requires an isolated PostgreSQL test DSN")
class PostgresCandidates(CandidateContract, unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["RAG_TEST_POSTGRES_DSN"]
        self.schema = "rag_test_" + uuid4().hex
        self.reopen = lambda: PostgresKnowledgeStore(self.dsn, schema=self.schema)
        self.store = self.reopen()
        self.addCleanup(self.drop_schema)

    def drop_schema(self):
        psycopg, sql, _ = _driver()
        with psycopg.connect(self.dsn) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def test_migration_preserves_unactivated_candidate(self):
        with TemporaryDirectory() as directory:
            source = SQLiteKnowledgeStore(Path(directory))
            job = source.stage(prepared()).job_id
            candidate = source.create_candidate_snapshot(self.tenant, [job], None)
            migrate_sqlite_to_postgres(source.path, self.dsn, schema=self.schema)
        self.assertIsNone(self.store.open_snapshot(self.tenant))
        with self.assertRaises(NotFoundError):
            self.store.open_snapshot(self.tenant, candidate.generation)
        with self.assertRaises(ConflictError):
            self.store.publish(self.tenant, job, None)
        self.assertEqual(self.store.activate_candidate_snapshot(self.tenant, candidate.generation, None), candidate)
