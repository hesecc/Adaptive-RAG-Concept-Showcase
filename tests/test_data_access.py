"""Classification isolation contracts using real persistence and ingestion."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from uuid import uuid4

from adaptive_rag.config import Principal, load_settings
from adaptive_rag.data_access import DATA_DOMAIN_LABELS, domains_allowed, normalize_data_domains
from adaptive_rag.domain import AccessContext, IngestRequest, ValidationError, stable_id
from adaptive_rag.ingestion import IngestionService, ParentChildChunker, StructuredParser, TextCleaner
from adaptive_rag.postgres import PostgresKnowledgeStore, _driver, migrate_sqlite_to_postgres
from adaptive_rag.storage import FileObjectStore, SQLiteKnowledgeStore, _SCHEMA, _json
from test_storage import prepared


class ClassificationValidationTests(unittest.TestCase):
    def test_known_domains_are_canonical_and_propagate_to_access(self):
        self.assertEqual(set(DATA_DOMAIN_LABELS), {"public", "operations", "finance", "product", "sales", "hr"})
        self.assertEqual(normalize_data_domains(("sales", "operations", "sales")), ("operations", "sales"))
        principal = Principal("tenant-a", "alice", data_domains=("sales", "operations", "sales"))
        self.assertEqual(principal.access.data_domains, ("operations", "sales"))
        request = IngestRequest("tenant-a", "document", "v1", "body", data_domains=("sales", "sales"))
        self.assertEqual(request.data_domains, ("sales",))
        self.assertIsNone(Principal("tenant-a", admin=True).access.data_domains)

    def test_invalid_classification_fails_at_the_trusted_boundary(self):
        for value in (None, [], "finance", ("unknown",), (None,), ([],), ("public",) * 1001):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValidationError):
                normalize_data_domains(value)
        with self.assertRaises(ValidationError):
            AccessContext("tenant-a", data_domains=["finance"])
        with self.assertRaises(ValidationError):
            IngestRequest("tenant-a", "document", "v1", "body", data_domains=("unknown",))

    def test_unclassified_and_corrupt_resources_fail_closed_for_scoped_accounts(self):
        self.assertTrue(domains_allowed([], None))
        self.assertTrue(domains_allowed(["public"], ()))
        self.assertTrue(domains_allowed(["operations", "finance"], ("operations",)))
        self.assertFalse(domains_allowed(["finance"], ("operations",)))
        for value in ([], None, "public", {"public": True}, ["public", "unknown"], [None]):
            with self.subTest(resource=value):
                self.assertFalse(domains_allowed(value, ("operations",)))

    def test_explicit_api_principal_configuration_keeps_classification_scope(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.toml"
            token = "test-token-for-classifications-001"
            path.write_text(f'[api_tokens.{token}]\ntenant_id = "tenant-a"\n'
                            'data_domains = ["operations", "public"]\n', encoding="utf-8")
            settings = load_settings(path)
            self.assertEqual(settings.api_tokens[token].access.data_domains, ("operations", "public"))
            path.write_text(f'[api_tokens.{token}]\ntenant_id = "tenant-a"\n'
                            'data_domains = "operations"\n', encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_settings(path)


class ClassificationStoreContract:
    def configure_ingestion(self):
        self.ingestion = IngestionService(self.store, FileObjectStore(self.root),
            StructuredParser(), TextCleaner(), ParentChildChunker(), "classifications-v1")
        self.operations = AccessContext("tenant-a", "alice", data_domains=("operations",))
        self.finance = AccessContext("tenant-a", "bob", data_domains=("finance",))
        self.admin = AccessContext("tenant-a", "admin")

    def ingest(self, doc, domains, *, version="v1", allowed=(), tenant="tenant-a"):
        return self.ingestion.ingest(IngestRequest(tenant, doc, version,
            f"# {doc}\nConfidential content for {doc} {version}.", title=f"Title {doc} {version}",
            allowed_subjects=allowed, data_domains=domains))

    def publish(self, doc, domains, **kwargs):
        job = self.ingest(doc, domains, **kwargs)
        tenant = kwargs.get("tenant", "tenant-a")
        previous = self.store.open_snapshot(tenant)
        return self.store.publish(tenant, job.job_id, previous.generation if previous else None)

    def visible_ids(self, snapshot, access):
        return {chunk.document_id for chunk in self.store.list_chunks(snapshot, access)}

    def test_public_department_and_legacy_data_are_isolated_in_children_and_parents(self):
        for doc, domains in (("operations", ("operations",)), ("finance", ("finance",)),
                             ("public", ("public",)), ("legacy", ())):
            snapshot = self.publish(doc, domains)
        self.assertEqual(self.visible_ids(snapshot, self.operations), {"operations", "public"})
        self.assertEqual(self.visible_ids(snapshot, self.finance), {"finance", "public"})
        self.assertEqual(self.visible_ids(snapshot, AccessContext("tenant-a", "new", data_domains=())), {"public"})
        all_children = self.store.list_chunks(snapshot, self.admin)
        self.assertEqual({c.document_id for c in all_children}, {"operations", "finance", "public", "legacy"})
        parent_ids = [c.parent_id for c in all_children]
        parents = self.store.get_parents(snapshot, parent_ids, self.operations)
        self.assertEqual({p.document_id for p in parents}, {"operations", "public"})

    def test_classification_and_subject_acl_are_both_required(self):
        self.publish("private-operations", ("operations",), allowed=("managers",))
        snapshot = self.publish("public-acl", ("public",), allowed=("managers",))
        self.assertEqual(self.visible_ids(snapshot, self.operations), set())
        member = AccessContext("tenant-a", "alice", ("managers",), ("operations",))
        self.assertEqual(self.visible_ids(snapshot, member), {"private-operations", "public-acl"})
        finance_manager = AccessContext("tenant-a", "bob", ("managers",), ("finance",))
        self.assertEqual(self.visible_ids(snapshot, finance_manager), {"public-acl"})

    def test_reclassification_revokes_old_snapshots_and_preserves_acl_updates(self):
        old = self.publish("report", ("operations",))
        children = self.store.list_chunks(old, self.operations)
        self.store.set_access("tenant-a", "report", (), data_domains=("finance",))
        self.assertEqual(self.visible_ids(old, self.operations), set())
        self.assertEqual(self.store.get_parents(old, [children[0].parent_id], self.operations), ())
        self.assertEqual(self.visible_ids(old, self.finance), {"report"})
        self.store.set_access("tenant-a", "report", ("bob",))
        self.assertEqual(self.store.list_documents("tenant-a")[0]["data_domains"], ["finance"])
        self.assertEqual(self.visible_ids(old, self.finance), {"report"})
        self.store.set_access("tenant-a", "report", (), data_domains=())
        self.assertEqual(self.visible_ids(old, self.finance), set())
        self.assertEqual(self.visible_ids(old, self.admin), {"report"})

    def test_new_revision_and_rollback_keep_live_classification_and_revocation(self):
        old = self.publish("report", ("operations",))
        self.store.set_access("tenant-a", "report", (), data_domains=("finance",))
        new = self.publish("report", ("operations",), version="v2")
        self.assertEqual(self.visible_ids(new, self.operations), set())
        self.assertEqual(self.visible_ids(new, self.finance), {"report"})
        deleted = self.store.delete_document("tenant-a", "report", new.generation)
        rolled = self.store.rollback("tenant-a", old.generation, deleted.generation)
        self.assertEqual(self.visible_ids(rolled, self.finance), set())
        self.store.set_access("tenant-a", "report", (), revoked=False)
        # Changing access or rolling back must not stand in for restoring a file.
        self.assertEqual(self.visible_ids(rolled, self.finance), set())
        manager = Principal("tenant-a", "manager", admin=True)
        tomb, = self.store.managed_documents(manager, "trash")
        self.store.restore_document(manager, "report", tomb["trash_id"], rolled.generation)
        restored = self.store.open_snapshot("tenant-a")
        self.assertEqual(self.visible_ids(restored, self.finance), {"report"})
        self.assertEqual(self.visible_ids(restored, self.operations), set())

    def test_partial_policy_patch_preserves_unmentioned_acl_and_revocation(self):
        old = self.publish("private", ("operations",), allowed=("alice",))
        self.store.set_access("tenant-a", "private", None, True)
        self.store.set_access("tenant-a", "private", data_domains=("finance",))
        listing = self.store.list_documents("tenant-a")[0]
        self.assertTrue(listing["revoked"])
        self.assertEqual(listing["allowed_subjects"], ["alice"])
        self.assertEqual(listing["data_domains"], ["finance"])
        self.assertEqual(self.visible_ids(old, self.admin), set())
        self.store.set_access("tenant-a", "private", (), False)
        self.assertEqual(self.visible_ids(old, self.finance), {"private"})

    def test_document_listing_contains_staged_and_current_published_metadata_without_body(self):
        staged = self.ingest("new", ("operations",))
        first_listing = self.store.list_documents("tenant-a")
        self.assertEqual(len(first_listing), 1)
        record = first_listing[0]
        self.assertFalse(record["published"])
        self.assertIsNone(record["generation"])
        self.assertEqual(record["staged_jobs"][0]["job_id"], staged.job_id)
        snapshot = self.store.publish("tenant-a", staged.job_id, None)
        next_job = self.ingest("new", ("finance",), version="v2")
        self.publish("foreign", ("finance",), tenant="tenant-b")
        listing = self.store.list_documents("tenant-a")
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["revision_id"], staged.revision_id)
        self.assertEqual(listing[0]["version"], "v1")
        self.assertEqual(listing[0]["title"], "Title new v1")
        self.assertTrue(listing[0]["published"])
        self.assertEqual(listing[0]["generation"], snapshot.generation)
        self.assertEqual(listing[0]["data_domains"], ["operations"])
        self.assertEqual([j["job_id"] for j in listing[0]["staged_jobs"]], [next_job.job_id])
        self.assertNotIn("Confidential content", json.dumps(listing))
        self.assertNotIn("metadata_json", listing[0])
        self.assertEqual(self.store.list_documents("unknown-tenant"), [])

    def test_latest_unpublished_revision_is_chosen_by_creation_time(self):
        self.ingest("draft", ("operations",))
        latest = self.ingest("draft", ("finance",), version="v2")
        listing = self.store.list_documents("tenant-a")
        self.assertEqual(listing[0]["revision_id"], latest.revision_id)
        self.assertEqual(listing[0]["title"], "Title draft v2")
        self.assertEqual(len(listing[0]["staged_jobs"]), 2)
        self.assertTrue(all(j["created_at"] for j in listing[0]["staged_jobs"]))

    def test_reordered_classifications_deduplicate_and_changed_classification_has_new_revision(self):
        first = self.ingest("both", ("operations", "finance"))
        same = self.ingest("both", ("finance", "operations", "operations"))
        changed = self.ingest("both", ("operations",))
        self.assertEqual(first.revision_id, same.revision_id)
        self.assertTrue(same.duplicate)
        self.assertNotEqual(first.revision_id, changed.revision_id)
        self.assertEqual(self.store.list_documents("tenant-a")[0]["data_domains"], ["finance", "operations"])


class SQLiteClassificationTests(ClassificationStoreContract, unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = SQLiteKnowledgeStore(self.root)
        self.configure_ingestion()

    def test_unclassified_ingestion_keeps_legacy_revision_identity_and_payload_hash(self):
        request = IngestRequest("tenant-a", "legacy", "v1", "Legacy body")
        job = self.ingestion.ingest(request)
        metadata = asdict(request)
        metadata.pop("content")
        metadata.pop("data_domains")
        metadata["sku_ids"] = []
        metadata["allowed_subjects"] = []
        expected = stable_id("revision-v1", metadata, sha256(request.content.encode()).hexdigest(),
                             self.ingestion.processing_fingerprint)
        self.assertEqual(job.revision_id, expected)
        document = prepared()
        original = asdict(document)
        original["request"].pop("data_domains")
        original["request"]["sku_ids"] = []
        original["request"]["allowed_subjects"] = []
        original["parents"] = sorted(original["parents"], key=lambda p: p["parent_id"])
        original["children"] = sorted(original["children"], key=lambda c: c["chunk_id"])
        expected_payload = sha256(_json(original).encode()).hexdigest()
        self.store.stage(document)
        with self.store._connection() as connection:
            actual = connection.execute("SELECT payload_hash FROM revisions WHERE document_id='shipping'").fetchone()[0]
        self.assertEqual(actual, expected_payload)


def legacy_database(root):
    """Create a real pre-classification database, including one immutable job."""
    schema = _SCHEMA.replace("    data_domains TEXT NOT NULL DEFAULT '[]',\n", "")
    schema = schema.replace("    created_at TEXT NOT NULL DEFAULT '',\n", "")
    document = prepared()
    request = document.request
    canonical = asdict(document)
    canonical["request"].pop("data_domains")
    canonical["request"]["sku_ids"] = []
    canonical["request"]["allowed_subjects"] = []
    canonical["parents"] = sorted(canonical["parents"], key=lambda p: p["parent_id"])
    canonical["children"] = sorted(canonical["children"], key=lambda c: c["chunk_id"])
    metadata = asdict(request)
    metadata.pop("content")
    metadata.pop("data_domains")
    with closing(sqlite3.connect(root / "knowledge.sqlite3")) as connection:
        connection.executescript(schema)
        connection.execute("INSERT INTO documents VALUES (?,?,?,0)",
                           (request.tenant_id, request.document_id, "[]"))
        connection.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?)",
            (request.tenant_id, request.document_id, document.revision_id, request.version,
             document.processing_fingerprint, document.content_hash, document.raw_key,
             _json(metadata), sha256(_json(canonical).encode()).hexdigest()))
        for parent in document.parents:
            connection.execute("INSERT INTO parents VALUES (?,?,?,?,?)", (request.tenant_id,
                request.document_id, document.revision_id, parent.parent_id, _json(asdict(parent))))
        for child in document.children:
            connection.execute("INSERT INTO children VALUES (?,?,?,?,?,?)", (request.tenant_id,
                request.document_id, document.revision_id, child.chunk_id, child.parent_id, _json(asdict(child))))
        connection.execute("INSERT INTO jobs VALUES (?,?,?,?,'staged',?,?)", (request.tenant_id,
            stable_id("ingest", request.tenant_id, request.document_id, document.revision_id),
            request.document_id, document.revision_id, len(document.parents), len(document.children)))
        connection.commit()
    return document


class SQLiteClassificationUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_legacy_database_upgrade_is_idempotent_preserves_duplicate_jobs_and_restricts_data(self):
        document = legacy_database(self.root)
        for _ in range(2):
            store = SQLiteKnowledgeStore(self.root)
            job = store.stage(document)
            self.assertTrue(job.duplicate)
            listing = store.list_documents("tenant-a")
            self.assertEqual(listing[0]["data_domains"], [])
            self.assertEqual(listing[0]["staged_jobs"][0]["created_at"], "")
        snapshot = store.publish("tenant-a", job.job_id, None)
        self.assertEqual(store.list_chunks(snapshot, AccessContext("tenant-a")), document.children)
        self.assertEqual(store.list_chunks(snapshot, AccessContext("tenant-a", data_domains=("operations",))), ())
        store.set_access("tenant-a", "shipping", (), data_domains=("operations",))
        self.assertEqual(store.list_chunks(snapshot, AccessContext("tenant-a", data_domains=("operations",))), document.children)

    def test_concurrent_legacy_database_upgrade_is_serialized(self):
        legacy_database(self.root)
        barrier = Barrier(2)
        def initialize(_):
            barrier.wait(timeout=10)
            return SQLiteKnowledgeStore(self.root).list_documents("tenant-a")[0]["data_domains"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(list(executor.map(initialize, range(2))), [[], []])

    def test_cli_ingestion_persists_repeated_classifications(self):
        from adaptive_rag.cli import dispatch, parser
        source = self.root / "manual.md"
        source.write_text("# Operations\nOperational instructions.", encoding="utf-8")
        dispatch(parser().parse_args(["--data-dir", str(self.root), "ingest", "--tenant", "tenant-a",
            "--document", "manual", "--version", "v1", "--file", str(source),
            "--data-domain", "operations", "--data-domain", "product"]))
        listing = SQLiteKnowledgeStore(self.root).list_documents("tenant-a")
        self.assertEqual(listing[0]["data_domains"], ["operations", "product"])


@unittest.skipUnless(os.environ.get("RAG_TEST_POSTGRES_DSN"), "set RAG_TEST_POSTGRES_DSN for isolated real PostgreSQL tests")
class PostgresClassificationTests(ClassificationStoreContract, unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.dsn = os.environ["RAG_TEST_POSTGRES_DSN"]
        self.schema = "rag_test_" + uuid4().hex
        self.store = PostgresKnowledgeStore(self.dsn, schema=self.schema)
        self.addCleanup(self.drop_schema)
        self.configure_ingestion()

    def drop_schema(self):
        psycopg, sql, _ = _driver()
        with psycopg.connect(self.dsn) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def test_migration_from_unupgraded_sqlite_uses_defaults_without_mutating_source(self):
        document = legacy_database(self.root)
        original_bytes = (self.root / "knowledge.sqlite3").read_bytes()
        counts = migrate_sqlite_to_postgres(self.root / "knowledge.sqlite3", self.dsn, schema=self.schema)
        self.assertEqual(counts["documents"], 1)
        self.assertEqual((self.root / "knowledge.sqlite3").read_bytes(), original_bytes)
        self.assertTrue(self.store.stage(document).duplicate)
        self.assertEqual(self.store.list_documents("tenant-a")[0]["data_domains"], [])

    def test_existing_postgres_schema_upgrade_is_idempotent(self):
        document = prepared()
        self.store.stage(document)
        with self.store._connection() as connection:
            # A pre-classification installation also predates the lifecycle view.
            connection.execute("DROP VIEW active_documents")
            connection.execute("ALTER TABLE documents DROP COLUMN owner_id")
            connection.execute("ALTER TABLE documents DROP COLUMN data_domains")
            connection.execute("ALTER TABLE revisions DROP COLUMN created_at")
        for _ in range(2):
            self.store = PostgresKnowledgeStore(self.dsn, schema=self.schema)
            self.assertTrue(self.store.stage(document).duplicate)
            self.assertEqual(self.store.list_documents("tenant-a")[0]["data_domains"], [])


if __name__ == "__main__":
    unittest.main()
