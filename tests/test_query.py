"""Query preparation protocols with synthetic cloud responses; no paid calls."""

import json
import unittest

import httpx

from adaptive_rag.cloud import CloudSettings, CloudTransport
from adaptive_rag.domain import AccessContext, CapabilityError, QueryRequest, SubQuery, ValidationError
from adaptive_rag.query import CloudPlanner, CloudQueryTransform


def request(question="那它怎么清洗？", **kwargs):
    return QueryRequest(AccessContext("demo"), question, **kwargs)


def transport(value, seen, *, raw=None, finish="stop"):
    def handler(call):
        seen.append(json.loads(call.content))
        content = raw if raw is not None else json.dumps(value, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": {"content": content}}]})
    settings = CloudSettings("https://models.example.test/v1", "synthetic-private", "llm", "embed", "rank",
                             llm_enable_thinking=False)
    return CloudTransport(settings, lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))


class CloudQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rewrite_receives_history_and_preserves_original_query(self):
        seen = []
        q = SubQuery("q1", "它怎么清洗？")
        req = request(history=("我们正在看A500保温杯。",), sku="A500")
        adapter = CloudQueryTransform(transport({"queries": [{"query_id": "q1", "text": "A500保温杯清洗方式"}]}, seen))
        result = await adapter.transform((q,), req)
        self.assertEqual(result, (q, SubQuery("q1", "A500保温杯清洗方式")))
        payload = seen[0]
        self.assertEqual(payload["model"], "llm")
        self.assertIs(payload["enable_thinking"], False)
        data = json.loads(payload["messages"][1]["content"])
        self.assertEqual(data["history"], list(req.history))
        self.assertEqual(data["trusted_sku"], "A500")
        self.assertEqual(req.question, "那它怎么清洗？")

    async def test_multiquery_and_hyde_only_append_existing_ids(self):
        queries = (SubQuery("q1", "质保多久"), SubQuery("q2", "不能机洗吗"))
        for mode in ("multi_query", "hyde"):
            seen = []
            value = {"queries": [{"query_id": "q1", "text": "保修有效期限"},
                                 {"query_id": "q2", "text": "清洗限制和注意事项"}]}
            result = await CloudQueryTransform(transport(value, seen), mode).transform(queries, request())
            self.assertEqual(result[:2], queries)
            self.assertEqual([q.query_id for q in result], ["q1", "q2", "q1", "q2"])
            if mode == "hyde":
                self.assertIn("not an answer or evidence", seen[0]["messages"][0]["content"])
            self.assertTrue(all(isinstance(q, SubQuery) for q in result))

    async def test_transform_deduplicates_and_skips_call_when_full(self):
        seen = []
        q = SubQuery("q1", "原问题")
        adapter = CloudQueryTransform(transport({"queries": [{"query_id": "q1", "text": "原问题"}]}, seen), max_variants=2)
        self.assertEqual(await adapter.transform((q, q), request()), (q,))
        full = (q, SubQuery("q1", "另一个问法"))
        self.assertEqual(await adapter.transform(full, request()), full)
        self.assertEqual(len(seen), 1)

    async def test_chained_transforms_keep_first_query_as_anchor(self):
        seen = []
        original = SubQuery("q1", "真实问题")
        variants = (original, SubQuery("q1", "假设文档"))
        result = await CloudQueryTransform(transport({"queries": []}, seen), "multi_query").transform(variants, request())
        self.assertEqual(result, variants)
        data = json.loads(seen[0]["messages"][1]["content"])
        self.assertEqual(data["queries"], [{"query_id": "q1", "text": original.text}])

    async def test_transform_rejects_mutated_contract_and_over_budget(self):
        q = SubQuery("q1", "原问题")
        bad_values = [
            {"queries": [{"query_id": "q9", "text": "x"}]},
            {"queries": [{"query_id": "q1", "text": "x", "sku": "other"}]},
            {"queries": [{"query_id": "q1", "text": " "}]},
            {"queries": [{"query_id": "q1", "text": "a\x00b"}]},
            {"queries": [{"query_id": "q1", "text": "x"}, {"query_id": "q1", "text": "y"}]},
            {"queries": [], "answer": "not permitted"},
        ]
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(CapabilityError):
                await CloudQueryTransform(transport(value, []), max_variants=2).transform((q,), request())
        with self.assertRaises(CapabilityError):
            await CloudQueryTransform(transport({"queries": [{"query_id": "q1", "text": "x" * 1001}]}, []), "hyde").transform((q,), request())
        with self.assertRaises(ValidationError):
            await CloudQueryTransform(transport({}, []), max_variants=1).transform((q, q), request())

    async def test_truncated_duplicate_or_nonjson_output_is_rejected_safely(self):
        for raw, finish in (("{\"queries\":[],\"queries\":[]}", "stop"),
                            ("```json\n{}\n```", "stop"), ("{}", "length"), ("[]", "stop")):
            with self.subTest(raw=raw), self.assertRaises(CapabilityError) as caught:
                await CloudQueryTransform(transport({}, [], raw=raw, finish=finish)).transform((SubQuery("q1", "hi"),), request())
            self.assertNotIn("synthetic-private", str(caught.exception))

    async def test_planner_decomposes_without_changing_trusted_sku(self):
        seen = []
        req = request("A款能机洗吗？质保多久？凭证是什么？", sku="A500", history=("A款",))
        result = await CloudPlanner(transport({"questions": ["A款清洗限制", "A款质保期限", "A款申请凭证"]}, seen)).plan(req)
        self.assertEqual(result.sku, "A500")
        self.assertEqual([q.query_id for q in result.queries], ["q1", "q2", "q3"])
        self.assertEqual(json.loads(seen[0]["messages"][1]["content"])["question"], req.question)

    async def test_planner_preserves_rule_guards_without_cloud_calls(self):
        seen = []
        adapter = CloudPlanner(transport({"questions": ["unreachable"]}, seen))
        self.assertEqual((await adapter.plan(request("当前库存还有多少"))).action, "capability_unavailable")
        self.assertEqual((await adapter.plan(request("这款能清洗吗"))).action, "clarify")
        self.assertFalse(seen)

    async def test_planner_rejects_sku_injection_empty_and_oversized_plan(self):
        for value in ({"questions": ["x"], "sku": "other"}, {"questions": []},
                      {"questions": ["x"] * 4}, {"questions": ["x", "x"]},
                      {"questions": ["x\x00"]}, {"questions": [1]}):
            with self.subTest(value=value), self.assertRaises(CapabilityError):
                await CloudPlanner(transport(value, [])).plan(request("需要查询质保", sku="A500"))

    def test_configuration_validation(self):
        fake = transport({}, [])
        for limit in (0, 4, True):
            with self.assertRaises(ValidationError):
                CloudPlanner(fake, limit)
        for limit in (0, 7, True):
            with self.assertRaises(ValidationError):
                CloudQueryTransform(fake, max_variants=limit)
        with self.assertRaises(ValidationError):
            CloudQueryTransform(fake, "other")


if __name__ == "__main__":
    unittest.main()
