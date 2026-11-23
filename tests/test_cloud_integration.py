"""Exercise the public application with real adapters and HTTP mock responses."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

from adaptive_rag.app import RagApplication
from adaptive_rag.config import Settings
from adaptive_rag.domain import AccessContext, CapabilityError, IngestRequest, QueryRequest
from adaptive_rag.environment import apply_environment


class CloudIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.base = Settings(data_dir=self.root / "data")
        self.settings = apply_environment(self.base, {
            "RAG_ENABLE_CLOUD": "true", "RAG_CLOUD_BASE_URL": "https://models.example.test/v1",
            "RAG_CLOUD_API_KEY": "synthetic-test-private-key", "RAG_LLM_MODEL": "llm-test",
            "RAG_EMBEDDING_MODEL": "embed-test", "RAG_RERANK_MODEL": "rank-test"})
        self.access = AccessContext("tenant", "reader")
        self.calls = []
        original = httpx.AsyncClient
        self.http_patch = patch("adaptive_rag.cloud.httpx.AsyncClient",
            side_effect=lambda **kwargs: original(transport=httpx.MockTransport(self.handle), **kwargs))
        self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.network_patch = patch("socket.socket.connect", side_effect=AssertionError("unexpected real network"))
        self.network_patch.start()
        self.addCleanup(self.network_patch.stop)

    def handle(self, request):
        payload = json.loads(request.content)
        self.calls.append((request.url.path, payload))
        usage = {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [1, .5]} for i, _ in enumerate(payload["input"])], "usage": usage})
        if request.url.path.endswith("/rerank"):
            return httpx.Response(200, json={"results": [{"index": i, "relevance_score": 1 / (i + 1)} for i in range(payload["top_n"])], "usage": usage})
        if request.url.path.endswith("/chat/completions"):
            source = json.loads(payload["messages"][1]["content"])["evidence"][0]
            content = {"claims": [{"text": "The amber warranty lasts twelve months.", "evidence_ids": [source["evidence_id"]]}]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}], "usage": usage})
        raise AssertionError("unexpected mock endpoint")

    def publish(self, app, document="policy", content="The amber warranty lasts twelve months.", allowed=(), tenant="tenant"):
        staged = app.ingest(IngestRequest(tenant, document, "v1", content, title=document, allowed_subjects=allowed))
        current = app.store.open_snapshot(tenant)
        return app.publish(tenant, staged.job_id, current.generation if current else None)

    async def test_cloud_full_flow_counts_cache_restart_and_acl(self):
        app = RagApplication(self.settings)
        self.publish(app)
        self.publish(app, "hidden", "TOP_SECRET_RESTRICTED warranty covers fifty years.", ("owner",))
        self.publish(app, "other", "TOP_SECRET_OTHER_TENANT warranty lasts ninety years.", tenant="other-tenant")
        first = await app.query(QueryRequest(self.access, "amber warranty"))
        self.assertEqual(first.status, "answered")
        self.assertEqual(first.capabilities["mode"], "cloud")
        self.assertEqual({e.document_id for e in first.evidence}, {"policy"})
        self.assertIn(first.evidence[0].evidence_id, first.answer)
        self.assertEqual(first.capabilities["usage"]["model_calls"], 4)
        trace = app.get_trace("tenant", first.trace_id)
        self.assertEqual((trace["model_calls"], trace["network_calls"]), (4, 4))
        self.assertEqual(trace["usage"]["total_tokens"], 36)
        self.assertFalse(trace["verification"]["support_checked"])
        self.assertNotIn("TOP_SECRET", json.dumps(self.calls))
        self.assertNotIn("synthetic-test-private-key", json.dumps(trace))
        restarted = RagApplication(self.settings)
        second = await restarted.query(QueryRequest(self.access, "amber warranty"))
        self.assertEqual(second.capabilities["usage"]["model_calls"], 3)
        restarted.set_access("tenant", "policy", ("owner",))
        self.calls.clear()
        denied = await restarted.query(QueryRequest(self.access, "amber warranty"))
        self.assertEqual(denied.status, "empty")
        self.assertFalse(denied.evidence)
        self.assertEqual(denied.capabilities["usage"]["model_calls"], 0)
        self.assertEqual(self.calls, [])

    async def test_model_free_query_after_cloud_has_no_usage_leak(self):
        cloud = RagApplication(self.settings)
        self.publish(cloud)
        self.assertGreater((await cloud.query(QueryRequest(self.access, "amber warranty"))).capabilities["usage"]["model_calls"], 0)
        self.calls.clear()
        plain = RagApplication(self.base)
        result = await plain.query(QueryRequest(self.access, "amber warranty"))
        self.assertEqual(result.status, "evidence_preview")
        self.assertEqual(result.capabilities["mode"], "model_free")
        self.assertEqual(result.capabilities["usage"]["model_calls"], 0)
        trace = plain.get_trace("tenant", result.trace_id)
        self.assertEqual((trace["model_calls"], trace["network_calls"]), (0, 0))
        self.assertEqual(self.calls, [])

    async def test_cloud_selections_without_opt_in_fail_before_factories(self):
        with patch("adaptive_rag.cloud.CloudTransport", side_effect=AssertionError("factory ran before authorization")):
            with self.assertRaises(CapabilityError):
                RagApplication(Settings(data_dir=self.root / "denied", plugins={"dense": "cloud"}))
        self.assertEqual(self.calls, [])

    async def test_upstream_auth_failure_is_metered_and_next_query_recovers(self):
        app = RagApplication(self.settings)
        self.publish(app)
        original = self.handle
        self.handle = lambda request: httpx.Response(401, json={"error": "synthetic-test-private-key provider detail"})
        with patch.object(app.trace_sink, "save", wraps=app.trace_sink.save) as save:
            with self.assertRaises(CapabilityError) as caught:
                await app.query(QueryRequest(self.access, "amber warranty"))
        self.assertNotIn("synthetic-test-private-key", str(caught.exception))
        self.assertNotIn("provider detail", str(caught.exception))
        save.assert_called_once()
        trace_id = save.call_args.args[0]["trace_id"]
        trace = app.get_trace("tenant", trace_id)
        self.assertEqual(trace["status"], "error")
        self.assertEqual((trace["model_calls"], trace["network_calls"]), (1, 1))
        self.assertEqual(trace["usage"]["calls"][0]["http_status"], 401)
        self.assertNotIn("synthetic-test-private-key", json.dumps(trace))
        self.handle = original
        recovered = await app.query(QueryRequest(self.access, "amber warranty"))
        self.assertEqual(recovered.status, "answered")
        self.assertEqual(recovered.capabilities["usage"]["model_calls"], 4)


if __name__ == "__main__":
    unittest.main()
