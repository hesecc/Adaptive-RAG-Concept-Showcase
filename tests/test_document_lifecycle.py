"""Shared document management and retention contract; PG requires an isolated test DSN."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from adaptive_rag.app import RagApplication
from adaptive_rag.config import Principal, Settings
from adaptive_rag.domain import ConflictError, ForbiddenError, NotFoundError
from adaptive_rag.postgres import PostgresKnowledgeStore, _driver, migrate_sqlite_to_postgres
from adaptive_rag.storage import SQLiteKnowledgeStore
from test_storage import prepared


class LifecycleContract:
    tenant = "tenant-a"
    owner = Principal(tenant, "owner", data_domains=("public",))
    admin = Principal(tenant, "operator", admin=True)
    other = Principal(tenant, "stranger")

    def stage(self, doc="shipping", version="v1", actor=None):
        value = prepared(doc=doc, version=version, text=f"{doc} {version}")
        value = replace(value, request=replace(value.request, data_domains=("public",)))
        return self.store.stage(value, principal=actor)

    def test_owner_from_first_stage_management_scope_and_audit(self):
        self.stage(actor=self.owner)
        self.stage(version="v2", actor=self.owner)
        job = self.stage(version="v3", actor=self.admin)
        self.stage(version="v3", actor=self.admin)  # No duplicate stage event.
        for actor in (self.other, Principal("tenant-b", "owner", admin=True)):
            with self.assertRaises((ConflictError, ForbiddenError)):
                self.stage(actor=actor)
            with self.assertRaises(NotFoundError):
                self.store.managed_document(actor, "shipping")
        self.stage(doc="legacy")
        with self.assertRaises(ConflictError):
            self.stage(doc="legacy", version="v2", actor=self.owner)
        self.stage(doc="legacy", version="v2", actor=self.admin)
        self.assertIsNone(self.store.managed_document(self.admin, "legacy")["owner_id"])
        with self.assertRaises(ForbiddenError):
            self.store.managed_documents(self.owner, "all")
        self.assertEqual(self.store.managed_documents(self.admin, "mine"), [])
        self.assertEqual(len(self.store.managed_documents(self.admin, "all")), 2)
        mine, = self.store.managed_documents(self.owner, "mine")
        self.assertEqual((mine["kind"], mine["id"], mine["owner_id"], mine["updated_by"]),
                         ("document", "shipping", "owner", "operator"))
        self.assertEqual(len(mine["staged_jobs"]), 3)
        self.assertEqual(mine["data_domains"], ["public"])
        generation = self.store.publish(self.tenant, job.job_id, None, principal=self.admin).generation
        self.store.publish(self.tenant, job.job_id, generation, principal=self.admin)
        self.store.set_access(self.tenant, "shipping", revoked=True, principal=self.admin)
        row = self.store.trash_document(self.owner, "shipping", generation)
        self.store.restore_document(self.owner, "shipping", row["trash_id"], row["generation"])
        detail = self.store.managed_document(self.owner, "shipping")
        self.assertTrue(detail["revoked"])
        self.assertEqual(set(v["version"] for v in detail["versions"]), {"v1", "v2", "v3"})
        self.assertEqual([(e["action"], e["actor_id"]) for e in reversed(detail["events"])],
                         [("stage", "owner"), ("stage", "owner"), ("stage", "operator"), ("publish", "operator"),
                          ("set_access", "operator"), ("trash", "owner"), ("restore", "owner")])

    def test_source_scope_current_acl_and_raw_keys_never_listed(self):
        doc = prepared()
        pdf_key = "f" * 64
        source = replace(doc, request=replace(doc.request, data_domains=("public",), source_object_key=pdf_key))
        self.store.stage(source, principal=self.owner)
        other = self.stage(doc="other", actor=self.other)
        lookup = self.store.document_source
        self.assertEqual(lookup(self.owner, "shipping"), (doc.raw_key, doc.request.media_type, doc.revision_id))
        self.assertEqual(lookup(self.owner, "shipping", doc.revision_id, original=True),
                         (pdf_key, "application/pdf", doc.revision_id))
        detail = json.dumps(self.store.managed_document(self.owner, "shipping"))
        self.assertNotIn(pdf_key, detail)
        self.assertNotIn(doc.raw_key, detail)
        with self.assertRaises(NotFoundError):
            lookup(self.owner, "shipping", other.revision_id)
        for actor in (self.other, Principal("tenant-b", "owner", admin=True)):
            with self.assertRaises(NotFoundError):
                lookup(actor, "shipping")
        for subjects, revoked, domains in (((), False, ("hr",)), (("private",), False, ("public",)),
                                           ((), True, ("public",))):
            self.store.set_access(self.tenant, "shipping", subjects, revoked, domains, principal=self.admin)
            with self.assertRaises(NotFoundError):
                lookup(self.owner, "shipping", doc.revision_id, original=True)
            self.assertEqual(lookup(self.admin, "shipping", original=True)[0], pdf_key)
        self.store.trash_document(self.admin, "shipping", None)
        with self.assertRaises(NotFoundError):
            lookup(self.admin, "shipping")

    def test_unclassified_draft_preview_only_for_original_uploader_and_before_policy_or_publication(self):
        doc = prepared()
        pdf_key = "a" * 64
        doc = replace(doc, request=replace(doc.request, source_object_key=pdf_key))
        job = self.store.stage(doc, principal=self.owner)
        lookup = self.store.document_source
        self.assertEqual(lookup(self.owner, "shipping", original=True)[0], pdf_key)
        self.assertEqual(lookup(self.owner, "shipping")[0], doc.raw_key)
        private = self.store.stage(prepared(version="v2", text="private edit"), principal=self.admin)
        for actor, rev in ((self.other, job.revision_id), (self.owner, private.revision_id), (self.owner, None)):
            with self.assertRaises(NotFoundError):
                lookup(actor, "shipping", rev, original=True)
        # A duplicate upload is not evidence of authorship.
        self.store.stage(prepared(version="v2", text="private edit"), principal=self.owner)
        with self.assertRaises(NotFoundError):
            lookup(self.owner, "shipping", private.revision_id)
        self.store.publish(self.tenant, private.job_id, None)
        with self.assertRaises(NotFoundError):
            lookup(self.owner, "shipping", job.revision_id, original=True)
        another = prepared(doc="another")
        self.store.stage(another, principal=self.owner)
        self.store.set_access(self.tenant, "another", revoked=True, principal=self.admin)
        with self.assertRaises(NotFoundError):
            lookup(self.owner, "another")
        self.store.set_access(self.tenant, "another", revoked=False, data_domains=("finance",), principal=self.admin)
        with self.assertRaises(NotFoundError):
            lookup(self.owner, "another")
        self.store.set_access(self.tenant, "another", data_domains=(), principal=self.admin)
        with self.assertRaises(NotFoundError):
            lookup(self.owner, "another")

    def test_trash_blocks_reads_prewarm_publication_candidate_and_rollback(self):
        first = self.stage(actor=self.owner)
        old = self.store.publish(self.tenant, first.job_id, None)
        second = self.stage(version="v2", actor=self.owner)
        candidate = self.store.create_candidate_snapshot(self.tenant, [second.job_id], old.generation)
        row = self.store.trash_document(self.owner, "shipping", old.generation)
        for snapshot, preview in ((old, False), (candidate, True)):
            self.assertEqual(self.store.list_chunks(snapshot, self.owner.access, allow_candidate=preview), ())
            self.assertEqual(self.store.list_prewarm_chunks(snapshot, allow_candidate=preview), ())
        for access in (None, self.owner.access):
            self.assertFalse(self.store.check_prewarm_access(self.tenant, ["shipping"], access=access))
        for operation in (lambda: self.stage(actor=self.owner), lambda: self.stage(actor=self.admin),
                          lambda: self.stage(), lambda: self.store.publish(self.tenant, first.job_id, row["generation"]),
                          lambda: self.store.create_candidate_snapshot(self.tenant, [second.job_id], row["generation"]),
                          lambda: self.store.activate_candidate_snapshot(self.tenant, candidate.generation, old.generation)):
            with self.assertRaises(ConflictError):
                operation()
        rolled = self.store.rollback(self.tenant, old.generation, row["generation"])
        self.assertEqual(rolled.revisions, ())
        with self.assertRaises(ConflictError):
            self.store.restore_document(self.owner, "shipping", row["trash_id"], row["generation"])
        self.store.restore_document(self.owner, "shipping", row["trash_id"], rolled.generation)
        self.assertEqual(self.store.open_snapshot(self.tenant).revisions, old.revisions)

    def test_admin_hold_and_cli_delete_share_the_same_168_hour_clock(self):
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        job = self.stage(actor=self.owner)
        old = self.store.publish(self.tenant, job.job_id, None)
        with patch("adaptive_rag.document_lifecycle._now", return_value=start):
            self.store.delete_document(self.tenant, "shipping", old.generation, principal=self.admin)
        trash, = self.store.managed_documents(self.owner, "trash")
        self.assertEqual(trash["deleted_by"], "operator")
        self.assertTrue(trash["admin_hold"])
        self.assertFalse(trash["can_restore"])
        self.assertEqual(datetime.fromisoformat(trash["purge_after"]), start + timedelta(hours=168))
        with self.assertRaises(ForbiddenError):
            self.store.restore_document(self.owner, "shipping", trash["trash_id"], trash["generation"])
        with patch("adaptive_rag.document_lifecycle._now", return_value=start + timedelta(hours=168)):
            with self.assertRaises(ConflictError):
                self.store.restore_document(self.admin, "shipping", trash["trash_id"], trash["generation"])
        with patch("adaptive_rag.document_lifecycle._now", return_value=start + timedelta(hours=168, microseconds=-1)):
            active = self.store.restore_document(self.admin, "shipping", trash["trash_id"], trash["generation"])
        self.store.delete_document(self.tenant, "shipping", active["generation"])
        current, = self.store.managed_documents(self.admin, "trash")
        self.assertIsNone(current["deleted_by"])
        self.assertNotEqual(current["trash_id"], trash["trash_id"])
        with self.assertRaises(ConflictError):
            self.store.restore_document(self.admin, "shipping", trash["trash_id"], current["generation"])

    def test_candidate_activation_attributes_publication_once(self):
        job = self.stage(actor=self.owner)
        candidate = self.store.create_candidate_snapshot(self.tenant, [job.job_id], None)
        for _ in range(2):
            self.store.activate_candidate_snapshot(self.tenant, candidate.generation, None, principal=self.admin)
        detail = self.store.managed_document(self.owner, "shipping")
        self.assertEqual([(e["action"], e["actor_id"]) for e in detail["events"]],
                         [("publish", "operator"), ("stage", "owner")])
        self.assertEqual(detail["versions"][0]["created_by"], "owner")

    def test_concurrent_first_stage_cannot_claim_another_owner(self):
        barrier = Barrier(2)
        def attempt(actor):
            barrier.wait(timeout=10)
            try:
                self.stage(actor=actor)
                return actor.subject_id
            except ConflictError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [x for x in pool.map(attempt, (self.owner, self.other)) if x]
        self.assertEqual(winners, [self.store.managed_document(self.admin, "shipping")["owner_id"]])


class SQLiteLifecycleTests(LifecycleContract, unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = SQLiteKnowledgeStore(Path(tmp.name))

    def test_trace_access_rechecks_historical_chunk_and_parent_after_cli_delete(self):
        app = RagApplication(Settings(data_dir=self.store.path.parent))
        doc = prepared()
        job = app.ingest(doc.request, principal=self.owner)
        snap = app.publish(self.tenant, job.job_id, None)
        access = self.admin.access
        chunk, = app.store.list_chunks(snap, access)
        for key, ref in (("retrieved_chunk_ids", chunk.chunk_id), ("source_parent_ids", chunk.parent_id)):
            app.trace_sink.save({"tenant_id": self.tenant, "trace_id": key, "generation": snap.generation, key: [ref]})
            self.assertEqual(app.get_accessible_trace(access, key)[key], [ref])
        app.delete_document(self.tenant, "shipping", snap.generation)
        for key in ("retrieved_chunk_ids", "source_parent_ids"):
            with self.assertRaises(NotFoundError):
                app.get_accessible_trace(access, key)

    def test_purging_staged_file_does_not_recycle_a_cached_url(self):
        job = self.stage(actor=self.owner)
        trash = self.store.trash_document(self.owner, "shipping", None)
        self.assertFalse(trash["published"])
        with self.store._snapshot_write(self.tenant) as conn:
            for table in ("jobs", "children", "parents", "revisions", "documents"):
                conn.execute(f"DELETE FROM {table} WHERE tenant_id=? AND document_id=?", (self.tenant, "shipping"))
        for actor in (self.owner, self.admin, None):
            with self.assertRaises(ConflictError):
                self.stage(actor=actor)
        with self.assertRaises(NotFoundError):
            self.store.document_source(self.admin, "shipping", job.revision_id)


@unittest.skipUnless(os.environ.get("RAG_TEST_POSTGRES_DSN"), "requires isolated PostgreSQL test DSN")
class PostgresLifecycleTests(LifecycleContract, unittest.TestCase):
    def setUp(self):
        dsn, schema = os.environ["RAG_TEST_POSTGRES_DSN"], "rag_test_" + uuid4().hex
        self.store = PostgresKnowledgeStore(dsn, schema=schema)
        def cleanup():
            psycopg, sql, _ = _driver()
            with psycopg.connect(dsn) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        self.addCleanup(cleanup)

    def test_migration_preserves_ownership_tombstone_and_audit(self):
        with TemporaryDirectory() as directory:
            source = SQLiteKnowledgeStore(Path(directory))
            job = source.stage(prepared(), principal=self.owner)
            snap = source.publish(self.tenant, job.job_id, None, principal=self.admin)
            trash = source.trash_document(self.owner, "shipping", snap.generation)
            counts = migrate_sqlite_to_postgres(source.path, os.environ["RAG_TEST_POSTGRES_DSN"], schema=self.store.schema)
        self.assertEqual(counts["document_trash"], 1)
        self.assertEqual(counts["retired_document_ids"], 1)
        self.assertEqual(self.store.managed_documents(self.owner, "trash"), [trash])
        detail = self.store.managed_document(self.owner, "shipping")
        self.assertEqual([e["action"] for e in detail["events"]], ["trash", "publish", "stage"])
        self.assertEqual(self.store.list_chunks(snap, self.admin.access), ())
        self.store.restore_document(self.owner, "shipping", trash["trash_id"], trash["generation"])
        self.assertTrue(self.store.list_chunks(snap, self.admin.access))
