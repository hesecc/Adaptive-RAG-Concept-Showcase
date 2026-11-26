"""Provider wire contracts tested without credentials, sockets or paid calls."""

import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import httpx

from adaptive_rag.cloud import (CloudDenseRetriever, CloudEmbedder, CloudGenerator, CloudReranker,
                                CloudSettings, CloudTransport, load_cloud_settings, reset_usage,
                                start_usage, usage_snapshot)
from adaptive_rag.domain import (CapabilityError, ChildChunk, Evidence, Hit, Plan, SubQuery, ValidationError)
from adaptive_rag.pipeline import CitationVerifier


def settings(**changes):
    return replace(CloudSettings("https://models.example.test/v1", "fixture-key-private", "llm-v1", "embed-v1", "rank-v1"), **changes)


def transport(handler, config=None):
    return CloudTransport(config or settings(), lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))


def bailian_settings(**changes):
    values = {"RAG_CLOUD_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
              "RAG_CLOUD_API_KEY": "fixture-key-private",
              "RAG_LLM_MODEL": "deepseek-v4.1-flash-1017",
              "RAG_EMBEDDING_MODEL": "qwen3.7-text-embedding-flash",
              "RAG_RERANK_MODEL": "qwen3.7-text-rerank"}
    values.update(changes)
    return load_cloud_settings(values)


def chunk(name, text=None, tenant="tenant"):
    text = text or name
    return ChildChunk(tenant, "doc", "v1", "rev", name, "parent", text, text, "line:1", "store", (), "en")


def evidence(text="The warranty is twelve months.", key="e1"):
    digest = sha256(text.encode()).hexdigest()
    return Evidence(key, ("q1",), "tenant", "generation", "doc", "v1", "rev", "parent", text, "line:1", digest, digest)


class CloudConfigurationTests(unittest.TestCase):
    def test_settings_validate_destination_and_hide_credentials(self):
        self.assertNotIn("fixture-key-private", repr(settings()))
        self.assertEqual(settings().base_url, "https://models.example.test/v1/")
        self.assertEqual(settings(base_url="http://127.0.0.1:9000/v1").base_url, "http://127.0.0.1:9000/v1/")
        for url in ("http://models.example.test/v1", "https://user:secret@models.example.test",
                    "https://models.example.test?token=secret", "https://models.example.test/#fragment",
                    "https://models.example.test:invalid", "https://models.example.test/has space", ""):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                settings(base_url=url)
        for path in ("https://other.example.test", "//other.example.test", "../chat", "chat?key=x"):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                settings(chat_path=path)

    def test_mapping_loader_does_not_require_process_environment(self):
        loaded = load_cloud_settings({"RAG_CLOUD_BASE_URL": "https://models.example.test/v1",
            "RAG_CLOUD_API_KEY": "fixture-key-private", "RAG_LLM_MODEL": "llm",
            "RAG_EMBEDDING_MODEL": "embed", "RAG_RERANK_MODEL": "rerank"})
        self.assertEqual(loaded.embedding_model, "embed")
        with self.assertRaises(ValidationError) as caught:
            load_cloud_settings({"RAG_CLOUD_API_KEY": "fixture-key-private"})
        self.assertNotIn("fixture-key-private", str(caught.exception))

    def test_bailian_uses_explicit_native_rerank_root_and_documented_batch_default(self):
        config = bailian_settings()
        self.assertEqual(config.base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1/")
        self.assertEqual(config.rerank_base_url, "https://dashscope.aliyuncs.com/api/v1/")
        self.assertEqual(config.rerank_path, "services/rerank/text-rerank/text-rerank")
        self.assertEqual(config.rerank_format, "dashscope")
        self.assertEqual(config.embedding_batch_size, 20)
        self.assertIs(config.llm_enable_thinking, False)
        workspace = bailian_settings(RAG_CLOUD_BASE_URL="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
        self.assertEqual(workspace.rerank_base_url, "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1/")
        # Model identifiers never route credentials to a provider on their own.
        other = bailian_settings(RAG_CLOUD_BASE_URL="https://models.example.test/v1")
        self.assertIsNone(other.rerank_base_url)
        self.assertEqual(other.rerank_format, "flat")
        self.assertIsNone(other.llm_enable_thinking)

    def test_rerank_configuration_overrides_are_explicit_and_validated(self):
        config = bailian_settings(RAG_RERANK_BASE_URL="https://rerank.example.test/compatible-api/v1",
                                  RAG_RERANK_PATH="reranks", RAG_RERANK_FORMAT="flat")
        self.assertEqual(config.rerank_base_url, "https://rerank.example.test/compatible-api/v1/")
        self.assertEqual((config.rerank_path, config.rerank_format), ("reranks", "flat"))
        for changes in ({"RAG_RERANK_BASE_URL": "http://remote.example.test/v1"},
                        {"RAG_RERANK_FORMAT": "unknown"},
                        {"RAG_RERANK_PATH": "https://other.example.test/"},
                        {"RAG_CLOUD_BASE_URL": "https://[invalid"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                bailian_settings(**changes)


class CloudTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_failure_keeps_safe_diagnosis_without_repeating_request(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.RemoteProtocolError("fixture-key-private private upstream detail", request=request)
        token = start_usage()
        try:
            with self.assertRaises(CapabilityError) as caught:
                await transport(handler).post("chat/completions", {"model": "llm-v1"})
            record = usage_snapshot()["calls"][-1]
            self.assertEqual(record.get("error_code"), "cloud_network_error")
            self.assertEqual(record.get("error_type"), "RemoteProtocolError")
            self.assertEqual(len(calls), 1)
            self.assertNotIn("fixture-key-private", json.dumps(record) + str(caught.exception))
            self.assertNotIn("upstream detail", json.dumps(record) + str(caught.exception))
        finally:
            reset_usage(token)

    async def test_timeout_keeps_distinct_diagnosis_without_repeating_request(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("fixture-key-private timeout detail", request=request)
        token = start_usage()
        try:
            with self.assertRaises(CapabilityError):
                await transport(handler).post("chat/completions", {"model": "llm-v1"})
            record = usage_snapshot()["calls"][-1]
            self.assertEqual(record.get("error_code"), "cloud_timeout")
            self.assertEqual(record.get("error_type"), "ReadTimeout")
            self.assertEqual(len(calls), 1)
            self.assertNotIn("fixture-key-private", json.dumps(record))
        finally:
            reset_usage(token)

    async def test_http_failure_keeps_status_and_safe_diagnosis(self):
        token = start_usage()
        try:
            with self.assertRaises(CapabilityError):
                await transport(lambda request: httpx.Response(401,
                    json={"error": "fixture-key-private"})).post("chat/completions", {})
            record = usage_snapshot()["calls"][-1]
            self.assertEqual(record.get("error_code"), "cloud_http_error")
            self.assertEqual(record["http_status"], 401)
            self.assertNotIn("fixture-key-private", json.dumps(record))
        finally:
            reset_usage(token)

    async def test_429_retries_once_with_metered_attempts_and_tokens(self):
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, json={"error": "private upstream body"}, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"data": [], "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}})
        meter = start_usage()
        try:
            value = await transport(handler).post("embeddings", {"model": "embed-v1", "input": ["hello"]})
            self.assertEqual(value["data"], [])
            snapshot = usage_snapshot()
            self.assertEqual((snapshot["model_calls"], snapshot["network_calls"], snapshot["total_tokens"]), (2, 2, 10))
            self.assertEqual([c["status"] for c in snapshot["calls"]], ["retry", "ok"])
            self.assertNotIn("fixture-key-private", json.dumps(snapshot))
            snapshot["calls"][0]["status"] = "mutated"
            self.assertEqual(usage_snapshot()["calls"][0]["status"], "retry")
        finally:
            reset_usage(meter)
        self.assertEqual(usage_snapshot()["model_calls"], 0)
        self.assertEqual(calls[0].url, "https://models.example.test/v1/embeddings")
        self.assertEqual(calls[0].headers["Authorization"], "Bearer fixture-key-private")

    async def test_auth_redirect_and_network_errors_are_safe_and_not_retried(self):
        for status in (401, 403, 302):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, json={"error": "fixture-key-private private upstream body"}, headers={"Location": "https://other.example.test"})
            with self.subTest(status=status), self.assertRaises(CapabilityError) as caught:
                await transport(handler).post("embeddings", {"model": "embed-v1"})
            self.assertEqual(len(calls), 1)
            self.assertNotIn("fixture-key-private", str(caught.exception))
            self.assertNotIn("upstream body", str(caught.exception))
        for error in (httpx.ReadTimeout, httpx.ConnectError):
            calls = []
            def handler(request):
                calls.append(request)
                raise error("fixture-key-private private detail", request=request)
            with self.subTest(error=error), self.assertRaises(CapabilityError) as caught:
                await transport(handler).post("embeddings", {})
            self.assertEqual(len(calls), 1)
            self.assertNotIn("fixture-key-private", str(caught.exception))

    async def test_server_retry_budget_and_invalid_json(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(503, json={"error": "private"}, headers={"Retry-After": "0"})
        with self.assertRaises(CapabilityError):
            await transport(handler).post("embeddings", {})
        self.assertEqual(len(calls), 2)
        for content in (b"not json", b"[]", b'{"error":"private"}', b'{"code":"InvalidApiKey","message":"fixture-key-private"}'):
            with self.subTest(content=content), self.assertRaises(CapabilityError):
                await transport(lambda request: httpx.Response(200, content=content)).post("embeddings", {})

    async def test_native_error_response_is_safe_and_not_retried(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"code": "InvalidApiKey",
                "message": "fixture-key-private sensitive provider detail", "request_id": "private"})
        config = bailian_settings()
        with self.assertRaises(CapabilityError) as caught:
            await transport(handler, config).post(config.rerank_path, {"model": config.rerank_model})
        self.assertEqual(len(calls), 1)
        self.assertNotIn("fixture-key-private", str(caught.exception))
        self.assertNotIn("sensitive provider detail", str(caught.exception))

    async def test_usage_contexts_are_isolated(self):
        async def request_one():
            token = start_usage()
            try:
                await asyncio.sleep(0)
                await transport(lambda request: httpx.Response(200, json={})).post("embeddings", {})
                return usage_snapshot()["model_calls"]
            finally:
                reset_usage(token)
        self.assertEqual(await asyncio.gather(request_one(), request_one()), [1, 1])


class CloudEmbeddingTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_batch_splits_twenty_one_inputs_for_bailian(self):
        batches = []
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(str(request.url), "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings")
            batches.append(payload["input"])
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [1, 1]}
                for i in range(len(payload["input"]))]})
        result = await CloudEmbedder(transport(handler, bailian_settings())).embed(tuple(str(i) for i in range(21)))
        self.assertEqual(len(result), 21)
        self.assertEqual([len(batch) for batch in batches], [20, 1])

    async def test_batching_indexes_and_dimension_consistency(self):
        inputs = []
        def handler(request):
            value = json.loads(request.content)
            inputs.append(value["input"])
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [i + 1, 1]} for i in reversed(range(len(value["input"])))]})
        embedder = CloudEmbedder(transport(handler, settings(embedding_batch_size=2)))
        result = await embedder.embed(("a", "b", "c"))
        self.assertEqual(result, ((1.0, 1.0), (2.0, 1.0), (1.0, 1.0)))
        self.assertEqual(inputs, [["a", "b"], ["c"]])
        self.assertEqual(embedder.dimension, 2)
        self.assertEqual(await embedder.embed(()), ())

    async def test_rejects_malformed_vectors_and_indexes(self):
        cases = [[], [{"index": 1, "embedding": [1, 2]}],
                 [{"index": False, "embedding": [1, 2]}], [{"index": 0, "embedding": [0, 0]}],
                 [{"index": 0, "embedding": [True, 1]}], [{"index": 0, "embedding": [1, 2, 3]}]]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(CapabilityError):
                await CloudEmbedder(transport(lambda request: httpx.Response(200, json={"data": data}), settings(embedding_dimensions=2))).embed(("hello",))
        calls = []
        def changed(request):
            calls.append(request)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 1] if len(calls) == 1 else [1, 1, 1]}]})
        embedder = CloudEmbedder(transport(changed))
        await embedder.embed(("first",))
        with self.assertRaises(CapabilityError):
            await embedder.embed(("second",))

    async def test_dense_cache_restarts_model_versions_and_authorized_sets(self):
        calls = []
        def handler(request):
            payload = json.loads(request.content)
            calls.append(payload)
            vectors = {"question": [1, 0], "authorized-alpha": [1, 0], "authorized-beta": [0, 1], "hidden-secret": [-1, 0]}
            return httpx.Response(200, json={"data": [{"index": index, "embedding": vectors[text]} for index, text in enumerate(payload["input"])]})
        with TemporaryDirectory() as directory:
            path = Path(directory)
            alpha, beta, secret = chunk("a", "authorized-alpha"), chunk("b", "authorized-beta"), chunk("s", "hidden-secret")
            retriever = CloudDenseRetriever(CloudEmbedder(transport(handler)), path)
            result = await retriever.search(SubQuery("q1", "question"), (alpha, beta), 2)
            self.assertEqual([h.chunk for h in result], [alpha, beta])
            self.assertEqual(len(calls), 2)
            self.assertNotIn("hidden-secret", json.dumps(calls))
            restarted = CloudDenseRetriever(CloudEmbedder(transport(handler)), path)
            result = await restarted.search(SubQuery("q1", "question"), (beta,), 2)
            self.assertEqual([h.chunk for h in result], [beta])
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[-1]["input"], ["question"])
            new_version = CloudDenseRetriever(CloudEmbedder(transport(handler, settings(embedding_model="embed-v2"))), path)
            await new_version.search(SubQuery("q1", "question"), (alpha,), 1)
            self.assertEqual(len(calls), 5)
            self.assertEqual(calls[-1]["model"], "embed-v2")
            self.assertNotIn("fixture-key-private", path.joinpath("cloud-vectors.sqlite3").read_bytes().decode("latin1"))
            self.assertEqual(await new_version.search(SubQuery("q1", "question"), (), 1), ())

    async def test_dense_budget_and_changed_dimension_fail_closed(self):
        count = []
        def handler(request):
            count.append(request)
            payload = json.loads(request.content)
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [1, 0]} for i in range(len(payload["input"]))]})
        with TemporaryDirectory() as directory:
            retriever = CloudDenseRetriever(CloudEmbedder(transport(handler)), Path(directory), max_missing_chunks=1)
            with self.assertRaises(CapabilityError):
                await retriever.search(SubQuery("q1", "question"), (chunk("a"), chunk("b")), 2)
            self.assertEqual(len(count), 1)
            await retriever.search(SubQuery("q1", "question"), (chunk("a"),), 1)
            shifted = CloudDenseRetriever(CloudEmbedder(transport(lambda request: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 1, 1]}]}))), Path(directory))
            with self.assertRaises(CapabilityError):
                await shifted.search(SubQuery("q1", "question"), (chunk("a"),), 1)


class CloudRerankTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_bailian_contract_preserves_candidates_and_usage(self):
        query = SubQuery("q1", "question")
        hits = (Hit("q1", chunk("a"), "rrf", .1, 1), Hit("q1", chunk("b"), "rrf", .05, 2))
        def handler(request):
            self.assertEqual(str(request.url), "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "qwen3.7-text-rerank")
            self.assertEqual(payload["input"], {"query": "question", "documents": ["a", "b"]})
            self.assertEqual(payload["parameters"]["top_n"], 2)
            self.assertIn("instruct", payload["parameters"])
            self.assertNotIn("return_documents", payload["parameters"])
            return httpx.Response(200, json={"output": {"results": [
                {"index": 1, "relevance_score": .9}, {"index": 0, "relevance_score": .2}]},
                "usage": {"prompt_tokens": 79, "total_tokens": 79}, "request_id": "request"})
        token = start_usage()
        try:
            ranked = await CloudReranker(transport(handler, bailian_settings())).rank(query, hits, 2)
            self.assertEqual([hit.chunk for hit in ranked], [hits[1].chunk, hits[0].chunk])
            self.assertEqual((usage_snapshot()["input_tokens"], usage_snapshot()["total_tokens"]), (79, 79))
        finally:
            reset_usage(token)

    async def test_rank_preserves_candidates_and_query(self):
        query = SubQuery("q2", "question")
        hits = (Hit("q2", chunk("a"), "rrf", .1, 1), Hit("q2", chunk("b"), "rrf", .05, 2))
        def handler(request):
            self.assertEqual(json.loads(request.content)["documents"], ["a", "b"])
            self.assertEqual(request.url.path, "/v1/rerank")
            return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": .2}, {"index": 1, "relevance_score": .9}]})
        result = await CloudReranker(transport(handler)).rank(query, hits, 2)
        self.assertEqual([h.chunk.chunk_id for h in result], ["b", "a"])
        self.assertTrue(all(h.query_id == "q2" and h.lane == "rerank" for h in result))

    async def test_invalid_candidate_responses_are_rejected(self):
        query = SubQuery("q1", "question")
        hits = (Hit("q1", chunk("a"), "rrf", .1, 1),)
        for results in ([], [{"index": 1, "relevance_score": .5}], [{"index": 0, "relevance_score": True}]):
            with self.subTest(results=results), self.assertRaises(CapabilityError):
                await CloudReranker(transport(lambda request: httpx.Response(200, json={"results": results}))).rank(query, hits, 1)


class CloudGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_generator_receives_original_question_and_source_headings(self):
        seen = []
        def handler(request):
            seen.append(json.loads(json.loads(request.content)["messages"][1]["content"]))
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"claims":[]}'}}]})
        item = replace(evidence(), heading_path=("Warranty policy", "Required materials"))
        question = "How do the required materials differ between warranty and paid repairs?"
        await CloudGenerator(transport(handler)).generate(
            Plan((SubQuery("q1", "materials"),), original_question=question), (item,))
        self.assertEqual(seen[0]["original_question"], question)
        self.assertEqual(seen[0]["questions"], [{"query_id": "q1", "text": "materials"}])
        self.assertEqual(seen[0]["evidence"][0]["heading_path"], ["Warranty policy", "Required materials"])
        self.assertEqual(seen[0]["evidence"][0]["text"], item.text)

    async def test_quality_json_mode_explicitly_instructs_json_for_bailian(self):
        from adaptive_rag.quality import CloudEvidenceJudge

        def handler(request):
            payload = json.loads(request.content)
            messages = " ".join(item["content"] for item in payload["messages"])
            if payload.get("response_format") == {"type": "json_object"} and "json" not in messages.lower():
                return httpx.Response(400, json={"error": {"code": "invalid_parameter_error"}})
            self.assertIs(payload["enable_thinking"], False)
            content = {"queries": [{"query_id": "q1", "sufficient": True,
                "evidence_ids": ["e1"], "missing_query": ""}]}
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                "message": {"content": json.dumps(content)}}]})

        judge = CloudEvidenceJudge(transport(handler, bailian_settings()))
        result = await judge.assess((SubQuery("q1", "How long is the warranty?"),), (evidence(),))
        self.assertEqual(result.covered_query_ids, ("q1",))

    async def test_bailian_chat_keeps_compatible_root_and_disables_thinking(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
            self.assertIs(json.loads(request.content)["enable_thinking"], False)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"claims":[]}'}, "finish_reason": "stop"}]})
        draft = await CloudGenerator(transport(handler, bailian_settings())).generate(Plan((SubQuery("q1", "warranty"),)), (evidence(),))
        self.assertIsNone(draft.answer)

    async def test_cited_claims_are_rendered_and_metered(self):
        content = {"claims": [{"text": "The warranty is twelve months.", "evidence_ids": ["e1"]}]}
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertFalse(payload["stream"])
            self.assertEqual(request.url.path, "/v1/chat/completions")
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}]})
        draft = await CloudGenerator(transport(handler)).generate(Plan((SubQuery("q1", "warranty"),)), (evidence(),))
        self.assertEqual(draft.answer, "The warranty is twelve months. [e1]")
        verification = await CitationVerifier().verify(draft, (evidence(),))
        self.assertTrue(verification.valid)
        self.assertFalse(verification.support_checked)

    async def test_no_evidence_and_no_supported_claims_do_not_fabricate_answers(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"claims":[]}'}}]})
        generator = CloudGenerator(transport(handler))
        plan = Plan((SubQuery("q1", "warranty"),))
        self.assertIsNone((await generator.generate(plan, ())).answer)
        self.assertEqual(calls, [])
        self.assertIsNone((await generator.generate(plan, (evidence(),))).answer)

    async def test_invalid_json_unknown_citations_and_extra_uncited_answer_fail(self):
        cases = ["not-json", '{"claims":[{"text":"wrong","evidence_ids":["other"]}]}',
                 '{"claims":[{"text":"wrong","evidence_ids":[]}]}',
                 '{"claims":[],"answer":"uncited"}']
        plan = Plan((SubQuery("q1", "warranty"),))
        for content in cases:
            with self.subTest(content=content), self.assertRaises(CapabilityError):
                await CloudGenerator(transport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": content}}]}))).generate(plan, (evidence(),))


if __name__ == "__main__":
    unittest.main()
