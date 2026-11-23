import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import time
from unittest.mock import patch

from adaptive_rag.app import RagApplication
from adaptive_rag.config import Settings, load_settings
from adaptive_rag.domain import (AccessContext, CapabilityError, Claim, Coverage, Draft, Hit,
                                 IngestRequest, NotFoundError, QueryRequest, SubQuery, ValidationError)
from adaptive_rag.pipeline import CitationVerifier
from adaptive_rag.plugins import default_registry
from adaptive_rag.retrieval import BM25Retriever, ReciprocalRankFusion, tokenize


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.app = RagApplication(self.settings)
        self.access = AccessContext("shop", "reader")

    def publish(self, app=None, document="policy", content="退货期限为七天，不支持拆封后退货。", version="v1"):
        app = app or self.app
        staged = app.ingest(IngestRequest("shop", document, version, content, title=document))
        current = app.store.open_snapshot("shop")
        return app.publish("shop", staged.job_id, current.generation if current else None)

    async def test_default_end_to_end_zero_network_model_and_restart(self):
        self.publish()
        with patch("socket.socket.connect", side_effect=AssertionError("unexpected network")):
            result = await self.app.query(QueryRequest(self.access, "退货期限"))
        self.assertEqual(result.status, "evidence_preview")
        self.assertIsNone(result.answer)
        self.assertFalse(result.claims)
        self.assertTrue(result.evidence)
        app = RagApplication(self.settings)
        trace = app.get_trace("shop", result.trace_id)
        self.assertEqual((trace["model_calls"], trace["network_calls"]), (0, 0))
        self.assertTrue(trace["retrieved_chunk_ids"])
        self.assertFalse(trace["verification"]["support_checked"])
        self.assertEqual(trace["coverage"]["basis"], "lexical_presence_only")
        evidence = result.evidence[0]
        read = app.get_evidence(self.access, result.generation, evidence.evidence_id)
        self.assertEqual(read.text, evidence.text)
        self.assertEqual(read.content_hash, evidence.content_hash)
        self.assertIn("disabled", {item["status"] for item in trace["stages"]})

    async def test_optional_preferences_are_request_local_and_keep_verification(self):
        calls = []
        registry = default_registry()

        class Rewrite:
            async def transform(self, queries, request):
                calls.append(request.access.subject_id)
                return queries

        registry.register("rewrite", "fixture", lambda _: Rewrite())
        app = RagApplication(replace(self.settings, plugins={"rewrite": "fixture"}), registry)
        self.publish(app)
        first, second = await asyncio.gather(
            app.query(QueryRequest(self.access, "退货期限", skip_query_steps=("rewrite",))),
            app.query(QueryRequest(AccessContext("shop", "other"), "退货期限")),
        )
        self.assertEqual(calls, ["other"])
        self.assertTrue(app.capabilities()["plugins"]["rewrite"]["enabled"])
        for result in (first, second):
            self.assertEqual(result.status, "evidence_preview")
            trace = app.get_trace("shop", result.trace_id)
            names = {stage["name"] for stage in trace["stages"]}
            self.assertIn("authorized_chunks", names)
            self.assertIn("verifier", names)
        trace = app.get_trace("shop", first.trace_id)
        rewrite = next(item for item in trace["stages"] if item["name"] == "rewrite")
        self.assertEqual((rewrite["status"], rewrite["reason"]), ("skipped", "user_preference"))

    async def test_evidence_preserves_source_heading_context_after_restart(self):
        self.publish(content="# 部署验收用虚构售后政策\n\n## 质保与材料\n申请质保必须提供购买凭证。")
        app = RagApplication(self.settings)
        result = await app.query(QueryRequest(self.access, "申请质保材料"))
        item = result.evidence[0]
        self.assertEqual(getattr(item, "heading_path", ()), ("部署验收用虚构售后政策", "质保与材料"))
        self.assertEqual(item.text, "申请质保必须提供购买凭证。")
        read = app.get_evidence(self.access, result.generation, item.evidence_id)
        self.assertEqual(read, replace(item, query_ids=()))

    async def test_no_index_empty_clarify_and_realtime_are_distinct(self):
        self.assertEqual((await self.app.query(QueryRequest(self.access, "退货"))).status, "no_index")
        self.publish()
        self.assertEqual((await self.app.query(QueryRequest(self.access, "zzzxxyy"))).status, "empty")
        self.assertEqual((await self.app.query(QueryRequest(self.access, "这款的保修"))).status, "clarify")
        self.assertEqual((await self.app.query(QueryRequest(self.access, "实时库存"))).status, "capability_unavailable")

    async def test_evidence_budget_integrity_and_live_revoke(self):
        self.publish(content="退货政策。" * 100)
        app = RagApplication(replace(self.settings, evidence_chars=64))
        result = await app.query(QueryRequest(self.access, "退货"))
        self.assertLessEqual(sum(len(e.text) for e in result.evidence), 64)
        item = result.evidence[0]
        self.assertTrue(item.truncated)
        self.assertNotEqual(item.content_hash, item.source_hash)
        with self.assertRaises(NotFoundError):
            app.get_evidence(self.access, result.generation, item.evidence_id[:-1] + ("0" if item.evidence_id[-1] != "0" else "1"))
        app.set_access("shop", "policy", ("admin",))
        with self.assertRaises(NotFoundError):
            app.get_evidence(self.access, result.generation, item.evidence_id)
        self.assertFalse((await app.query(QueryRequest(self.access, "退货"))).evidence)

    async def test_multiquestion_fairness_and_source_generation(self):
        first = self.publish(document="returns", content="退货期限为七天。")
        self.publish(document="warranty", content="保修期为一年。")
        result = await self.app.query(QueryRequest(self.access, "退货期限？保修期？", top_k=2))
        self.assertEqual({e.document_id for e in result.evidence}, {"returns", "warranty"})
        self.assertFalse(result.missing_query_ids)
        old = await self.app.query(QueryRequest(self.access, "保修期", generation=first.generation))
        self.assertNotIn("warranty", {item.document_id for item in old.evidence})
        self.assertEqual(old.generation, first.generation)
        with self.assertRaises(ValidationError):
            await self.app.query(QueryRequest(self.access, "a?b?c?d?"))

    async def test_all_query_slots_are_replaceable_and_gap_keeps_original_qid(self):
        calls = []
        registry = default_registry()

        class Transform:
            def __init__(self, role): self.role = role
            async def transform(self, queries, request):
                calls.append(self.role)
                return tuple(SubQuery(q.query_id, "退货" if self.role == "gap" else q.text) for q in queries)

        class Dense:
            async def search(self, query, chunks, limit):
                calls.append("dense")
                return ()

        class Generator:
            async def generate(self, plan, evidence):
                calls.append("generator")
                return Draft("本地测试替身输出", (Claim("退货测试", (evidence[0].evidence_id,)),), "test_fixture")

        for role in ("rewrite", "multi_query", "hyde", "gap"):
            registry.register(role, "fixture", lambda s, r=role: Transform(r))
        registry.register("dense", "fixture", lambda s: Dense())
        registry.register("generator", "fixture", lambda s: Generator())
        selected = {role: "fixture" for role in ("rewrite", "multi_query", "hyde", "gap", "dense", "generator")}
        app = RagApplication(replace(self.settings, plugins=selected), registry)
        self.publish(app)
        result = await app.query(QueryRequest(self.access, "zzzxxyy"))
        self.assertEqual(result.status, "answered")
        self.assertEqual(set(calls), set(selected))
        self.assertEqual(result.evidence[0].query_ids, ("q1",))
        trace = app.get_trace("shop", result.trace_id)
        self.assertEqual(trace["retry_count"], 1)
        # Gap queries use original IDs; judge still assesses the original question.
        self.assertEqual(result.missing_query_ids, ("q1",))

    async def test_plugin_failure_is_not_empty_and_is_traced(self):
        class Broken:
            async def search(self, *args): raise RuntimeError("private key must not be surfaced")
        registry = default_registry()
        registry.register("lexical", "broken", lambda s: Broken())
        app = RagApplication(replace(self.settings, plugins={"lexical": "broken"}), registry)
        self.publish(app)
        with self.assertRaises(CapabilityError) as caught:
            await app.query(QueryRequest(self.access, "退货"))
        self.assertNotIn("private", str(caught.exception))
        trace = app.get_trace("shop", str(caught.exception).split("trace_id=")[1])
        self.assertEqual(trace["status"], "error")
        self.assertIn("error", {s["status"] for s in trace["stages"]})

    async def test_timeout_persisted_and_limited(self):
        class Slow:
            async def search(self, *args):
                await asyncio.sleep(5)
                return ()
        registry = default_registry()
        registry.register("lexical", "slow", lambda s: Slow())
        app = RagApplication(replace(self.settings, plugins={"lexical": "slow"}, stage_timeout_seconds=0.3), registry)
        self.publish(app)
        with self.assertRaises(CapabilityError) as caught:
            await app.query(QueryRequest(self.access, "退货"))
        trace = app.get_trace("shop", str(caught.exception).split("trace_id=")[1])
        self.assertEqual(trace["status"], "timeout")
        self.assertLess(trace["duration_ms"], 3000)

    async def test_mutated_unauthorized_chunk_rejected(self):
        class Bad:
            async def search(self, query, chunks, limit):
                return (Hit(query.query_id, replace(chunks[0], text="伪造正文"), "bad", 1, 1),)
        registry = default_registry()
        registry.register("lexical", "bad", lambda s: Bad())
        app = RagApplication(replace(self.settings, plugins={"lexical": "bad"}), registry)
        self.publish(app)
        with self.assertRaises(ValidationError):
            await app.query(QueryRequest(self.access, "退货"))

    async def test_transform_cannot_change_ids_or_exceed_budget(self):
        class Bad:
            async def transform(self, queries, request): return (SubQuery("other", "退货"),)
        registry = default_registry()
        registry.register("rewrite", "bad", lambda s: Bad())
        app = RagApplication(replace(self.settings, plugins={"rewrite": "bad"}), registry)
        with self.assertRaises(ValidationError):
            await app.query(QueryRequest(self.access, "退货"))

    async def test_invalid_citations_fail_and_no_semantic_support_claim(self):
        verifier = CitationVerifier()
        result = await verifier.verify(Draft("x", (Claim("x", ("missing",)),)), ())
        self.assertFalse(result.valid)
        self.assertFalse(result.support_checked)

    async def test_fusion_cannot_invent_candidates(self):
        self.publish()
        snapshot = self.app.store.open_snapshot("shop")
        chunk = self.app.store.list_chunks(snapshot, self.access)[0]
        class BadFusion:
            def fuse(self, query, lanes, limit):
                return (Hit(query.query_id, chunk, "bad", 1, 1),)
        registry = default_registry()
        registry.register("fusion", "bad", lambda s: BadFusion())
        app = RagApplication(replace(self.settings, plugins={"fusion": "bad"}), registry)
        with self.assertRaises(ValidationError):
            await app.query(QueryRequest(self.access, "zzzxxyy"))

    async def test_policy_stop_never_returns_revoked_evidence(self):
        self.publish()
        store = self.app.store
        class RevokingPolicy:
            async def decide(self, state):
                if state["checkpoint"] == "gap":
                    store.set_access("shop", "policy", (), True)
                    return {"action": "stop"}
                return {"action": "continue"}
        registry = default_registry()
        registry.register("decision", "revoke", lambda s: RevokingPolicy())
        app = RagApplication(replace(self.settings, plugins={"decision": "revoke"}), registry)
        result = await app.query(QueryRequest(self.access, "退货"))
        self.assertEqual(result.status, "policy_stopped")
        self.assertFalse(result.evidence)

    async def test_custom_chunk_identifiers_roundtrip(self):
        from adaptive_rag.ingestion import ParentChildChunker
        class CustomChunker:
            def chunk(self, request, revision_id, sections):
                parents, children = ParentChildChunker().chunk(request, revision_id, sections)
                return ((replace(parents[0], parent_id="custom.parent"),),
                        (replace(children[0], chunk_id="custom.child", parent_id="custom.parent"),))
        registry = default_registry()
        registry.register("chunker", "custom", lambda s: CustomChunker())
        app = RagApplication(replace(self.settings, plugins={"chunker": "custom"}), registry)
        self.publish(app)
        result = await app.query(QueryRequest(self.access, "退货"))
        item = app.get_evidence(self.access, result.generation, result.evidence[0].evidence_id)
        self.assertEqual(item.parent_id, "custom.parent")

    async def test_disabled_generator_is_not_called(self):
        registry = default_registry()
        registry.register("generator", "off", lambda s: None, enabled=False)
        app = RagApplication(replace(self.settings, plugins={"generator": "off"}), registry)
        self.publish(app)
        self.assertIsNone((await app.query(QueryRequest(self.access, "退货"))).answer)

    async def test_trace_failure_is_wrapped_and_save_does_not_block_loop(self):
        class BadTrace:
            def save(self, trace): raise RuntimeError("sensitive detail")
            def get(self, tenant_id, trace_id): raise NotFoundError()
            def list_conversations(self, tenant_id, subject_id, limit, offset): return []
            def conversation_turns(self, tenant_id, subject_id, conversation_id, *, limit=65): return []
        registry = default_registry()
        registry.register("trace", "bad", lambda s: BadTrace())
        app = RagApplication(replace(self.settings, plugins={"trace": "bad"}), registry)
        with self.assertRaises(CapabilityError) as caught:
            await app.query(QueryRequest(self.access, "hello"))
        self.assertIn("trace persistence failed", str(caught.exception))
        self.assertNotIn("sensitive", str(caught.exception))

        class SlowTrace(BadTrace):
            def save(self, trace): time.sleep(0.2)
        registry.register("trace", "slow", lambda s: SlowTrace())
        app = RagApplication(replace(self.settings, plugins={"trace": "slow"}, timeout_seconds=0.1), registry)
        ticked = []
        async def tick():
            await asyncio.sleep(0.01)
            ticked.append(True)
        ticker = asyncio.create_task(tick())
        with self.assertRaises(CapabilityError):
            await app.query(QueryRequest(self.access, "hello"))
        await ticker
        self.assertTrue(ticked)

    def test_model_and_network_plugins_rejected_before_factory(self):
        for flags in ({"requires_model": True}, {"uses_network": True}):
            registry = default_registry()
            registry.register("dense", "forbidden-fixture", lambda s: self.fail("factory invoked"), **flags)
            with self.assertRaises(CapabilityError):
                RagApplication(replace(self.settings, plugins={"dense": "forbidden-fixture"}), registry)

    def test_config_and_domain_reject_ambiguous_types(self):
        for options in ({"top_k": True}, {"history": "x"}):
            with self.assertRaises(ValidationError): QueryRequest(self.access, "x", **options)
        for options in ({"max_retries": 2}, {"host": "0.0.0.0"}, {"timeout_seconds": float("nan")}, {"plugins": {"nope": "x"}}):
            with self.assertRaises(ValidationError): Settings(**options)
        path = Path(self.temp.name) / "bad.toml"
        for content in ('bad = 1', '[api_tokens]\naaaaaaaaaaaaaaaaaaaaaaaa = 1'):
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(ValidationError): load_settings(path)

    async def test_bm25_is_lexical_and_rrf_fuses_rank_not_score(self):
        self.publish()
        snapshot = self.app.store.open_snapshot("shop")
        chunks = self.app.store.list_chunks(snapshot, self.access)
        q = SubQuery("q1", "退货")
        hits = await BM25Retriever().search(q, chunks, 5)
        self.assertGreater(hits[0].score, 0)
        fused = ReciprocalRankFusion().fuse(q, (hits, hits), 5)
        self.assertAlmostEqual(fused[0].score, 2 / 61)
        self.assertIn("a01-500", tokenize("A01-500 中文"))


if __name__ == "__main__":
    unittest.main()
