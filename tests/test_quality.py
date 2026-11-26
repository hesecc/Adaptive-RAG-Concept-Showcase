"""Quality wire validation and bounded recovery, with no sockets or credentials."""

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from adaptive_rag.app import RagApplication
from adaptive_rag.config import Settings
from adaptive_rag.domain import (AccessContext, CapabilityError, Claim, Coverage, Draft,
    Evidence, IngestRequest, Plan, QueryRequest, SubQuery, Verification)
from adaptive_rag.plugins import default_registry
from adaptive_rag.quality import CloudEvidenceJudge, CloudGapTransform, CloudVerifier
from adaptive_rag.pipeline import QueryPipeline


class Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.settings = SimpleNamespace(llm_model="fixture", llm_max_tokens=2000,
                                       llm_enable_thinking=False, chat_path="chat/completions")

    async def post(self, path, payload):
        self.calls.append((path, payload))
        content = self.response if isinstance(self.response, str) else json.dumps(self.response)
        return {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}


def evidence(text="整机质保一年，人为损坏除外。"):
    digest = sha256(text.encode()).hexdigest()
    return Evidence("e1", ("q1",), "t", "g", "doc", "v1", "r", "p", text, "line:1", digest, digest)


class QualityWireTests(unittest.IsolatedAsyncioTestCase):
    def test_query_budget_retains_originals_and_round_robins_expansions(self):
        originals = (SubQuery("q1", "原问1"), SubQuery("q2", "原问2"))
        variants = (SubQuery("q1", "扩展11"), SubQuery("q1", "扩展12"), SubQuery("q2", "扩展21"))
        selected = QueryPipeline._budget_variants(variants, originals, 4)
        self.assertEqual(selected, (*originals, variants[0], variants[2]))

    async def test_sufficiency_requires_all_aspects_and_keeps_targeted_gap(self):
        transport = Transport({"queries": [{"query_id": "q1", "sufficient": False,
            "evidence_ids": ["e1"], "missing_query": "质保申请所需材料"}]})
        result = await CloudEvidenceJudge(transport).assess((SubQuery("q1", "如何保修，需要哪些材料？"),), (evidence(),))
        self.assertEqual(result.covered_query_ids, ())
        self.assertEqual(result.missing_queries, (SubQuery("q1", "质保申请所需材料"),))
        self.assertEqual(result.basis, "cloud_semantic_sufficiency")
        self.assertIn("untrusted", transport.calls[0][1]["messages"][0]["content"])

    async def test_empty_evidence_has_no_cloud_call(self):
        transport = Transport({})
        result = await CloudEvidenceJudge(transport).assess((SubQuery("q1", "质保"),), ())
        self.assertEqual(result.missing_query_ids, ("q1",))
        self.assertFalse(transport.calls)

    async def test_semantic_judge_receives_separate_source_heading_context(self):
        transport = Transport({"queries": [{"query_id": "q1", "sufficient": True,
            "evidence_ids": ["e1"], "missing_query": ""}]})
        item = replace(evidence(), heading_path=("部署验收用虚构售后政策", "质保与材料"))
        await CloudEvidenceJudge(transport).assess((SubQuery("q1", "部署验收政策申请材料"),), (item,))
        row = json.loads(transport.calls[0][1]["messages"][1]["content"])["evidence"][0]
        self.assertEqual(row.get("heading_path"), ["部署验收用虚构售后政策", "质保与材料"])
        self.assertEqual(row["text"], item.text)

    async def test_source_heading_context_has_a_shared_budget_and_marks_truncation(self):
        transport = Transport({"queries": [{"query_id": "q1", "sufficient": True,
            "evidence_ids": ["e1"], "missing_query": ""}]})
        first = replace(evidence(), heading_path=("A" * 9000,))
        second = replace(evidence(), evidence_id="e2", heading_path=("B" * 9000,))
        await CloudEvidenceJudge(transport).assess((SubQuery("q1", "质保"),), (first, second))
        rows = json.loads(transport.calls[0][1]["messages"][1]["content"])["evidence"]
        self.assertEqual(rows[0]["heading_path"], ["A" * 4000])
        self.assertEqual(rows[1]["heading_path"], ["B" * 4000])
        self.assertTrue(all(row.get("heading_path_truncated") for row in rows))
        self.assertEqual([row["text"] for row in rows], [first.text, second.text])

    async def test_sufficiency_rejects_unknown_or_wrong_question_citation(self):
        for key in ("unknown", "e1"):
            with self.subTest(key=key):
                transport = Transport({"queries": [{"query_id": "q1", "sufficient": True,
                    "evidence_ids": [key], "missing_query": ""}]})
                item = replace(evidence(), query_ids=("q2",))
                with self.assertRaises(CapabilityError):
                    await CloudEvidenceJudge(transport).assess((SubQuery("q1", "质保"),), (item,))

    async def test_duplicate_json_keys_and_truncated_reviews_fail_closed(self):
        for response in ('{"queries":[],"queries":[]}', '{"queries":', {"queries": []}):
            with self.subTest(response=response), self.assertRaises(CapabilityError):
                await CloudEvidenceJudge(Transport(response)).assess((SubQuery("q1", "质保"),), (evidence(),))

    async def test_verifier_rejects_two_year_claim_and_records_omission(self):
        response = {"claims": [{"claim_index": 0, "status": "contradicted", "evidence_ids": ["e1"]},
                               {"claim_index": 1, "status": "supported", "evidence_ids": ["e1"]}],
                    "queries": [{"query_id": "q1", "complete": False, "missing_query": "申请质保所需材料"}]}
        draft = Draft("fixture", (Claim("质保两年", ("e1",)), Claim("人为损坏除外", ("e1",))), "fixture")
        value = await CloudVerifier(Transport(response)).evaluate(Plan((SubQuery("q1", "如何保修"),)), draft, (evidence(),))
        self.assertFalse(value.valid)
        self.assertTrue(value.support_checked)
        self.assertTrue(value.completeness_checked)
        self.assertEqual(value.supported_claim_indexes, (1,))
        self.assertEqual(value.missing_queries[0].text, "申请质保所需材料")

    async def test_verifier_never_sends_unknown_citation_to_model(self):
        transport = Transport({})
        draft = Draft("fixture", (Claim("质保一年", ("unknown",)),), "fixture")
        value = await CloudVerifier(transport).verify(draft, (evidence(),))
        self.assertFalse(value.valid)
        self.assertFalse(transport.calls)

    async def test_verifier_requires_one_verdict_per_claim_and_exact_citations(self):
        draft = Draft("fixture", (Claim("质保一年", ("e1",)),), "fixture")
        for claims in ([], [{"claim_index": True, "status": "supported", "evidence_ids": ["e1"]}],
                       [{"claim_index": 0, "status": "supported", "evidence_ids": ["unknown"]}]):
            response = {"claims": claims, "queries": [{"query_id": "q1", "complete": True, "missing_query": ""}]}
            with self.subTest(claims=claims), self.assertRaises(CapabilityError):
                await CloudVerifier(Transport(response)).evaluate(Plan((SubQuery("q1", "质保"),)), draft, (evidence(),))

    async def test_gap_cannot_reassign_or_drop_question(self):
        request = QueryRequest(AccessContext("t"), "如何保修")
        for row in ({"query_id": "q2", "text": "质保"}, {"query_id": "q1", "text": ""}):
            with self.subTest(row=row), self.assertRaises(CapabilityError):
                await CloudGapTransform(Transport({"queries": [row]})).transform((SubQuery("q1", "申请材料"),), request)


class QualityPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.access = AccessContext("t")

    def app(self, judge, generator, verifier, gap=None):
        registry = default_registry()
        selected = {}
        for role, component in (("judge", judge), ("generator", generator), ("verifier", verifier), ("gap", gap)):
            if component is not None:
                registry.register(role, "fixture_quality", lambda s, c=component: c)
                selected[role] = "fixture_quality"
        app = RagApplication(replace(self.settings, plugins=selected), registry)
        current = None
        for name, content in (("warranty", "质保政策：整机质保一年。"), ("materials", "申请材料：订单号和故障照片。")):
            job = app.ingest(IngestRequest("t", name, "v1", content, title=name))
            current = app.publish("t", job.job_id, current.generation if current else None)
        return app

    async def test_insufficient_evidence_never_calls_generator(self):
        class Judge:
            async def assess(self, queries, evidence):
                return Coverage((), ("q1",), "cloud_semantic_sufficiency", (SubQuery("q1", "未知事实"),))
        class Generator:
            async def generate(self, *args): raise AssertionError("must not generate")
        app = self.app(Judge(), Generator(), None)
        result = await app.query(QueryRequest(self.access, "质保"))
        self.assertEqual(result.status, "insufficient")
        self.assertIsNone(result.answer)
        self.assertTrue(any(item.document_id == "warranty" for item in result.evidence))

    async def test_unsupported_claim_removed_partial_result_has_only_verified_claim(self):
        class Judge:
            async def assess(self, queries, evidence): return Coverage(("q1",), (), "cloud_semantic_sufficiency")
        class Generator:
            async def generate(self, plan, evidence):
                if plan.original_question != "质保":
                    raise AssertionError("generator needs the original question")
                return Draft("unsafe free text", (Claim("整机质保两年", (evidence[0].evidence_id,)),
                    Claim("政策说明存在", (evidence[0].evidence_id,))), "fixture")
        class Verifier:
            async def verify(self, *args): raise AssertionError("evaluate expected")
            async def evaluate(self, plan, draft, evidence): return Verification(False, ("claim_contradicted:0",), True, (1,), (), True)
        app = self.app(Judge(), Generator(), Verifier())
        result = await app.query(QueryRequest(self.access, "质保"))
        self.assertEqual(result.status, "partial")
        self.assertEqual(len(result.claims), 1)
        self.assertNotIn("两年", result.answer)
        self.assertNotIn("unsafe", result.answer)

    async def test_post_generation_repair_requires_new_evidence_and_reverifies(self):
        events = []
        class Judge:
            async def assess(self, queries, evidence): return Coverage(("q1",), (), "cloud_semantic_sufficiency")
        class Generator:
            async def generate(self, plan, evidence):
                events.append("generate")
                return Draft("fixture", (Claim("已有证据", (evidence[0].evidence_id,)),), "fixture")
        class Verifier:
            async def verify(self, *args): raise AssertionError("evaluate expected")
            async def evaluate(self, plan, draft, evidence):
                events.append("verify")
                if len(events) == 2:
                    return Verification(True, (), True, (0,), (SubQuery("q1", "申请材料"),), True)
                return Verification(True, (), True, (0,), (), True)
        class Gap:
            async def transform(self, queries, request): return queries
        app = self.app(Judge(), Generator(), Verifier(), Gap())
        result = await app.query(QueryRequest(self.access, "质保", top_k=2))
        self.assertEqual(result.status, "answered")
        self.assertEqual(events, ["generate", "verify", "generate", "verify"])
        trace = app.get_trace("t", result.trace_id)
        self.assertEqual(trace["retry_count"], 1)
        self.assertTrue(trace["repair_new_evidence"])

    async def test_same_evidence_repair_does_not_generate_again(self):
        calls = []
        class Judge:
            async def assess(self, queries, evidence): return Coverage(("q1",), (), "cloud_semantic_sufficiency")
        class Generator:
            async def generate(self, plan, evidence):
                calls.append("generate")
                return Draft("fixture", (Claim("质保一年", (evidence[0].evidence_id,)),), "fixture")
        class Verifier:
            async def verify(self, *args): raise AssertionError("evaluate expected")
            async def evaluate(self, plan, draft, evidence):
                return Verification(True, (), True, (0,), (SubQuery("q1", "质保"),), True)
        class Gap:
            async def transform(self, queries, request): return queries
        app = self.app(Judge(), Generator(), Verifier(), Gap())
        result = await app.query(QueryRequest(self.access, "质保"))
        self.assertEqual(result.status, "partial")
        self.assertEqual(calls, ["generate"])
        self.assertEqual(app.get_trace("t", result.trace_id)["retry_count"], 1)

    async def test_pre_and_post_generation_recovery_share_one_budget(self):
        calls = []
        class Judge:
            async def assess(self, queries, evidence):
                calls.append("judge")
                return Coverage(("q1",), (), "cloud_semantic_sufficiency") if len(calls) > 1 else Coverage((), ("q1",), "cloud_semantic_sufficiency", (SubQuery("q1", "质保"),))
        class Generator:
            async def generate(self, plan, evidence): return Draft("fixture", (Claim("质保一年", (evidence[0].evidence_id,)),), "fixture")
        class Verifier:
            async def verify(self, *args): raise AssertionError("evaluate expected")
            async def evaluate(self, plan, draft, evidence): return Verification(True, (), True, (0,), (SubQuery("q1", "申请材料"),), True)
        class Gap:
            async def transform(self, queries, request):
                calls.append("gap")
                return queries
        app = self.app(Judge(), Generator(), Verifier(), Gap())
        result = await app.query(QueryRequest(self.access, "质保"))
        self.assertEqual(result.status, "partial")
        self.assertEqual(calls.count("gap"), 1)
        self.assertEqual(app.get_trace("t", result.trace_id)["retry_count"], 1)

    async def test_semantic_partial_exit_rechecks_acl(self):
        app_holder = []
        class Judge:
            async def assess(self, queries, evidence): return Coverage(("q1",), (), "cloud_semantic_sufficiency")
        class Generator:
            async def generate(self, plan, evidence): return Draft("fixture", (Claim("质保一年", (evidence[0].evidence_id,)),), "fixture")
        class Verifier:
            async def verify(self, *args): raise AssertionError("evaluate expected")
            async def evaluate(self, plan, draft, evidence):
                app_holder[0].set_access("t", "warranty", (), True)
                return Verification(False, ("claim_contradicted:0",), True, (), (), True)
        app = self.app(Judge(), Generator(), Verifier())
        app_holder.append(app)
        from adaptive_rag.domain import NotFoundError
        with self.assertRaises(NotFoundError):
            await app.query(QueryRequest(self.access, "质保"))

    async def test_malformed_semantic_review_never_releases_draft(self):
        class Judge:
            async def assess(self, queries, evidence): return Coverage(("q1",), (), "cloud_semantic_sufficiency")
        class Generator:
            async def generate(self, plan, evidence): return Draft("private draft", (Claim("质保两年", (evidence[0].evidence_id,)),), "fixture")
        app = self.app(Judge(), Generator(), CloudVerifier(Transport("not JSON")))
        with self.assertRaises(CapabilityError) as caught:
            await app.query(QueryRequest(self.access, "质保"))
        self.assertNotIn("private draft", str(caught.exception))
        self.assertNotIn("not JSON", str(caught.exception))

    async def test_empty_authorized_set_never_invokes_gap_or_cloud_judge(self):
        class Generator:
            async def generate(self, *args): raise AssertionError("no generation for empty ACL")
        class Gap:
            async def transform(self, *args): raise AssertionError("no gap for empty ACL")
        transport = Transport({})
        app = self.app(CloudEvidenceJudge(transport), Generator(), CloudVerifier(transport), Gap())
        app.set_access("t", "warranty", (), True)
        app.set_access("t", "materials", (), True)
        result = await app.query(QueryRequest(self.access, "质保"))
        self.assertEqual(result.status, "empty")
        self.assertFalse(transport.calls)

    async def test_repair_respects_lane_budget_without_losing_safe_partial(self):
        class Judge:
            async def assess(self, queries, evidence): return Coverage(("q1",), (), "cloud_semantic_sufficiency")
        class Generator:
            async def generate(self, plan, evidence): return Draft("fixture", (Claim("质保一年", (evidence[0].evidence_id,)),), "fixture")
        class Verifier:
            async def verify(self, *args): raise AssertionError("evaluate expected")
            async def evaluate(self, plan, draft, evidence): return Verification(True, (), True, (0,), (SubQuery("q1", "申请材料"),), True)
        class Gap:
            async def transform(self, *args): raise AssertionError("lane budget exhausted")
        self.settings = replace(self.settings, max_lanes=1)
        app = self.app(Judge(), Generator(), Verifier(), Gap())
        result = await app.query(QueryRequest(self.access, "质保"))
        self.assertEqual(result.status, "partial")
        trace = app.get_trace("t", result.trace_id)
        self.assertEqual(trace["retry_count"], 0)
        self.assertEqual(trace["repair_skipped"], "retrieval_lane_budget")

    async def test_initial_variants_reserve_repair_budget_and_do_not_starve_questions(self):
        registry = default_registry()
        class Transform:
            async def transform(self, queries, request):
                return tuple(SubQuery(query.query_id, f"扩展{index} " + query.text)
                             for query in queries for index in range(2))
        class Dense:
            async def search(self, query, chunks, limit): return ()
        class Gap:
            async def transform(self, queries, request): return queries
        for role, component in (("rewrite", Transform()), ("dense", Dense()), ("gap", Gap())):
            registry.register(role, "fixture_budget", lambda s, c=component: c)
        app = RagApplication(replace(self.settings, plugins={role: "fixture_budget" for role in ("rewrite", "dense", "gap")}), registry)
        job = app.ingest(IngestRequest("t", "doc", "v1", "质保一年，申请材料需要订单号。"))
        app.publish("t", job.job_id, None)
        result = await app.query(QueryRequest(self.access, "质保？申请材料？"))
        trace = app.get_trace("t", result.trace_id)
        self.assertEqual(trace["reserved_repair_lanes"], 4)
        self.assertEqual(trace["lane_count"], 8)
        variants = trace["query_variants"]
        self.assertEqual([q["text"] for q in variants[:2]], ["质保", "申请材料"])
        self.assertEqual([q["query_id"] for q in variants[2:]], ["q1", "q2"])

    async def test_history_trace_records_only_actual_cloud_consumption(self):
        from adaptive_rag.cloud import CloudSettings, CloudTransport
        from adaptive_rag.query import CloudPlanner
        import httpx
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                "message": {"content": json.dumps({"questions": ["质保"]})}}]})
        settings = CloudSettings("https://models.example.test/v1", "fixture", "llm", "embedding", "rerank")
        transport = CloudTransport(settings, lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))
        registry = default_registry()
        registry.register("planner", "fixture_history", lambda s: CloudPlanner(transport))
        app = RagApplication(replace(self.settings, plugins={"planner": "fixture_history"}), registry)
        job = app.ingest(IngestRequest("t", "doc", "v1", "质保一年。"))
        app.publish("t", job.job_id, None)
        request = QueryRequest(self.access, "质保多久", history=("正在问保修",))
        result = await app.query(request)
        self.assertTrue(app.get_trace("t", result.trace_id)["history_used"])
        result = await app.query(replace(request, question="实时库存"))
        self.assertFalse(app.get_trace("t", result.trace_id)["history_used"])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
