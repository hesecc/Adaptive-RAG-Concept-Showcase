"""Document prewarm exercises the real knowledge/vector stores with a recording provider."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from adaptive_rag.app import RagApplication
from adaptive_rag.cloud import CloudDenseRetriever, CloudEmbedder, CloudSettings
from adaptive_rag.config import Settings
from adaptive_rag.domain import (AccessContext, CapabilityError, ForbiddenError, IngestRequest,
                                 NotFoundError, SubQuery, ValidationError)
from adaptive_rag.figures import marker, readable_text
from adaptive_rag.prewarm import prewarm_generation


class RecordingProvider:
    def __init__(self):
        self.settings = CloudSettings("https://fixture.invalid/api", "test", "unused", "doc-model", "unused",
                                      embeddings_path="doc-embeddings", embedding_batch_size=2)
        self.calls = []
        self.on_call = None
        self.dimension = 2

    async def post(self, path, payload):
        assert path == self.settings.embeddings_path and payload["model"] == self.settings.embedding_model
        assert 0 < len(payload["input"]) <= self.settings.embedding_batch_size
        self.calls.append(payload["input"])
        if self.on_call:
            self.on_call()
        return {"data": [{"index": i, "embedding": [1] * self.dimension} for i in range(len(payload["input"]))]}


class PrewarmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.app = RagApplication(Settings(data_dir=self.root))
        self.store = self.app.store
        self.provider = RecordingProvider()
        self.access = AccessContext("tenant", "reader", ("staff",), ("sales",))
        self.dense = self.retriever()

    def retriever(self, budget=20):
        return CloudDenseRetriever(CloudEmbedder(self.provider), self.root, max_missing_chunks=budget)

    def publish(self, doc, text=None, *, tenant="tenant", domains=("public",), **metadata):
        job = self.app.ingest(IngestRequest(tenant, doc, "v1", text or doc, data_domains=domains, **metadata))
        current = self.store.open_snapshot(tenant)
        return self.app.publish(tenant, job.job_id, current.generation if current else None)

    async def warm(self, snapshot, **options):
        return await prewarm_generation(self.store, self.dense, self.access, snapshot.generation, **options)

    async def test_dry_run_deduplication_readable_text_and_query_cache(self):
        source = marker("a" * 64, "Diagram caption", 2)
        self.publish("figure", source)
        self.publish("copy", source)
        snap = self.publish("different-heading", source, title="Another heading")
        before = await self.warm(snap)
        self.assertEqual(before, {"generation": snap.generation, "scope": "principal", "all_scopes": False,
                                  "dry_run": True, "authorized_chunks": 3,
                                  "unique_chunks": 2, "cached": 0, "missing": 2, "planned": 2,
                                  "embedded": 0, "remaining": 2})
        self.assertEqual(self.provider.calls, [])
        chunks = self.store.list_chunks(snap, self.access)
        expected = set(readable_text(chunk.retrieval_text) for chunk in chunks)
        self.assertEqual((await self.warm(snap, dry_run=False))["embedded"], 2)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(set(self.provider.calls[0]), expected)
        self.assertNotIn("ragfig:", str(self.provider.calls))
        self.assertIn("Diagram caption", str(self.provider.calls))
        self.dense = self.retriever()
        await self.dense.search(SubQuery("q1", "question only"), chunks, 3)
        self.assertEqual(self.provider.calls[1:], [["question only"]])
        self.assertEqual((await self.warm(snap, dry_run=False))["missing"], 0)
        self.assertEqual(len(self.provider.calls), 2)
        self.provider.settings = replace(self.provider.settings, embedding_model="different-model")
        self.dense = self.retriever()
        self.assertEqual((await self.warm(snap))["missing"], 2)

    async def test_query_seeded_cache_pinned_generation_and_current_acl(self):
        self.publish("allowed", allowed_subjects=("staff",), domains=("sales",))
        self.publish("private", allowed_subjects=("owner",))
        self.publish("finance", domains=("finance",))
        self.publish("legacy", domains=())
        self.publish("revoked")
        self.publish("sku-b", scope="sku", sku_ids=("B",))
        snap = self.publish("sku-a", scope="sku", sku_ids=("A",))
        self.publish("new-document")
        self.publish("other-tenant", tenant="other")
        self.app.set_access("tenant", "revoked", revoked=True)
        store_only = self.store.list_chunks(snap, self.access)
        self.assertEqual([c.document_id for c in store_only], ["allowed"])
        await self.dense.search(SubQuery("q1", "question"), store_only, 1)
        self.provider.calls.clear()
        result = await self.warm(snap, sku="A", dry_run=False)
        self.assertEqual((result["authorized_chunks"], result["cached"], result["embedded"]), (2, 1, 1))
        self.assertEqual(self.provider.calls, [["Untitled\nsku-a"]])
        with self.assertRaises(NotFoundError):
            await prewarm_generation(self.store, self.dense, AccessContext("other"), snap.generation, dry_run=False)

    async def test_candidate_opt_in_warms_the_cache_without_publishing(self):
        live = self.publish("live")
        job = self.app.ingest(IngestRequest("tenant", "staged", "v1", "new text", data_domains=("public",)))
        candidate = self.store.create_candidate_snapshot("tenant", (job.job_id,), live.generation)
        with self.assertRaises(NotFoundError):
            await self.warm(candidate, dry_run=False)
        self.assertEqual(self.provider.calls, [])
        result = await self.warm(candidate, allow_candidate=True, dry_run=False)
        self.assertEqual((result["embedded"], result["remaining"]), (2, 0))
        self.assertEqual(self.store.open_snapshot("tenant"), live)
        active = self.store.activate_candidate_snapshot("tenant", candidate.generation, live.generation)
        await self.retriever().search(SubQuery("q1", "question"), self.store.list_chunks(active, self.access), 2)
        self.assertEqual(self.provider.calls[1:], [["question"]])

    async def test_all_scopes_reads_generation_once_but_keeps_policies_live(self):
        self.publish("private", allowed_subjects=("owner",), domains=("finance",))
        self.publish("sku", scope="sku", sku_ids=("B",))
        self.publish("revoked", allowed_subjects=("owner",))
        snap = self.publish("public")
        self.publish("later")
        self.publish("other-sku", tenant="other", scope="sku", sku_ids=("B",))
        self.app.set_access("tenant", "revoked", revoked=True)
        operator = AccessContext("tenant")
        self.provider.settings = replace(self.provider.settings, embedding_batch_size=1)
        with patch.object(self.store, "list_prewarm_chunks", wraps=self.store.list_prewarm_chunks) as listing:
            result = await prewarm_generation(self.store, self.retriever(), operator, snap.generation,
                                              all_scopes=True, dry_run=False)
            listing.assert_called_once()
        self.assertEqual((result["scope"], result["authorized_chunks"], result["embedded"]), ("all_scopes", 3, 3))
        self.assertEqual(sorted(item for batch in self.provider.calls for item in batch),
                         ["Untitled\nprivate", "Untitled\npublic", "Untitled\nsku"])
        report = await self.warm(snap)
        self.assertEqual((report["scope"], report["authorized_chunks"], report["cached"]), ("principal", 1, 1))
        for access, sku in ((self.access, None), (AccessContext("tenant", "owner"), None),
                            (AccessContext("tenant", groups=("group",)), None),
                            (AccessContext("tenant", data_domains=()), None), (operator, "B")):
            with self.subTest(access=access, sku=sku), self.assertRaises(ValidationError):
                await prewarm_generation(self.store, self.dense, access, snap.generation,
                                         all_scopes=True, sku=sku, dry_run=False)

    async def test_operator_honors_revocation_between_batches(self):
        self.publish("a")
        self.publish("b")
        snap = self.publish("z-restricted", allowed_subjects=("owner",))
        self.provider.on_call = lambda: self.app.set_access("tenant", "z-restricted", revoked=True)
        with self.assertRaises(ForbiddenError):
            await prewarm_generation(self.store, self.dense, AccessContext("tenant"), snap.generation,
                                     all_scopes=True, dry_run=False)
        self.assertEqual(self.provider.calls, [["Untitled\na", "Untitled\nb"]])

    async def test_restart_after_provider_failure_and_per_run_budget(self):
        for doc in ("a", "b", "c", "d", "e", "f"):
            snap = self.publish(doc)
        self.dense = self.retriever(budget=3)

        def interrupt():
            if len(self.provider.calls) == 2:
                raise CapabilityError("interrupted batch")

        self.provider.on_call = interrupt
        with self.assertRaises(CapabilityError):
            await self.warm(snap, dry_run=False)
        self.assertEqual([len(c) for c in self.provider.calls], [2, 1])
        self.dense = self.retriever(budget=3)
        self.provider.on_call = None
        self.provider.calls.clear()
        result = await self.warm(snap, dry_run=False)
        self.assertEqual((result["cached"], result["embedded"], result["remaining"]), (2, 3, 1))
        self.assertEqual(self.provider.calls, [["Untitled\nc", "Untitled\nd"], ["Untitled\ne"]])
        self.assertEqual((await self.warm(snap, max_chunks=1, dry_run=False))["remaining"], 0)
        self.assertEqual(self.provider.calls[-1], ["Untitled\nf"])

    async def test_revocation_between_provider_batches(self):
        self.publish("a")
        self.publish("b")
        snap = self.publish("secret")
        self.provider.on_call = lambda: self.app.set_access("tenant", "secret", revoked=True)
        with self.assertRaises(ForbiddenError):
            await self.warm(snap, dry_run=False)
        self.assertEqual(self.provider.calls, [["Untitled\na", "Untitled\nb"]])
        self.assertEqual((await self.warm(snap))["cached"], 2)

    async def test_changed_implicit_dimension_does_not_poison_cache_on_resume(self):
        self.publish("a")
        snap = self.publish("b")
        await self.warm(snap, dry_run=False, max_chunks=1)
        self.dense = self.retriever()
        self.provider.dimension = 3
        with self.assertRaises(CapabilityError):
            await self.warm(snap, dry_run=False)
        self.assertEqual((await self.warm(snap))["missing"], 1)
        self.provider.dimension = 2
        self.assertEqual((await self.warm(snap, dry_run=False))["remaining"], 0)

    async def test_rejects_implicit_generation_invalid_controls_and_disabled_dense(self):
        snap = self.publish("a")
        for generation in (None, "", " "):
            with self.subTest(generation=generation), self.assertRaises(ValidationError):
                await prewarm_generation(self.store, self.dense, self.access, generation, dry_run=False)
        with self.assertRaises(NotFoundError):
            await prewarm_generation(self.store, self.dense, self.access, "not-published", dry_run=False)
        for options in ({"max_chunks": False}, {"max_chunks": 0}, {"max_chunks": 21}, {"dry_run": 0}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                await self.warm(snap, **options)
        with self.assertRaises(CapabilityError):
            await prewarm_generation(self.store, None, self.access, snap.generation, dry_run=False)
        self.assertEqual(self.provider.calls, [])
