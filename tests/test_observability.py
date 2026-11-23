"""Public workbench transport, timing and source-access invariants."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
import httpx

from adaptive_rag.api import create_api
from adaptive_rag.app import RagApplication
from adaptive_rag.config import Principal, Settings
from adaptive_rag.domain import AccessContext, CapabilityError, Hit, IngestRequest, NotFoundError, QueryRequest, ValidationError
from adaptive_rag.pipeline import RulePlanner
from adaptive_rag.cloud import CloudSettings, CloudTransport
from adaptive_rag.quality import CloudEvidenceJudge
from support import ADMIN_PASSWORD


ADMIN ="console-admin-test-0000000000000000"
READER = "console-reader-test-000000000000000"
OTHER = "console-other-test-0000000000000000"


def parse_events(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        if lines:
            events.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return events


class WorkbenchHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = Settings(data_dir=Path(self.temp.name), admin_password=ADMIN_PASSWORD, api_tokens={
            ADMIN: Principal("alpha", "admin", admin=True),
            READER: Principal("alpha", "reader"),
            OTHER: Principal("beta", "admin", admin=True),
        })
        self.api = create_api(settings)
        self.app = self.api.state.application
        self.client = TestClient(self.api, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def headers(self, token=ADMIN):
        return {"Authorization": "Bearer " + token}

    def publish(self, document="policy", sku=None):
        job = self.app.ingest(IngestRequest("alpha", document, "v1",
            "# Warranty\nAmber warranty lasts twelve months. <img src=x onerror=alert(1)>",
            scope="sku" if sku else "store", sku_ids=(sku,) if sku else ()))
        prior = self.app.store.open_snapshot("alpha")
        return self.app.publish("alpha", job.job_id, prior.generation if prior else None)

    def query(self, **body):
        return self.client.post("/query/stream", headers=self.headers(), json={"question": "amber warranty", **body})

    def test_static_is_public_but_console_and_stream_require_login(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("检索工作台", page.text)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        for path in ("/console/session", "/query/stream"):
            for token, status in ((None, 401), ("invalid", 401), (READER, 200)):
                headers = self.headers(token) if token else {}
                response = self.client.get(path, headers=headers) if path.endswith("session") else self.client.post(path, headers=headers, json={"question": "warranty"})
                self.assertEqual(response.status_code, status, response.text)
        session = self.client.get("/console/session", headers=self.headers()).json()
        self.assertTrue(session["admin"])
        self.assertNotIn(ADMIN, json.dumps(session))

    def test_stream_has_real_timing_validated_outputs_and_persisted_final_result(self):
        self.publish()
        response = self.query()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        events = parse_events(response.text)
        self.assertEqual(events[0][0], "trace")
        self.assertEqual(events[-1][0], "result")
        final = events[-1][1]
        self.assertEqual(final["result"]["status"], "evidence_preview")
        self.assertTrue(final["result"]["evidence"])
        trace = final["trace"]
        persisted = self.client.get("/traces/" + trace["trace_id"], headers=self.headers()).json()
        self.assertEqual(persisted, trace)
        self.assertEqual(trace["final_result"], final["result"])
        self.assertEqual([row["index"] for row in trace["stages"]], list(range(len(trace["stages"]))))
        for row in trace["stages"]:
            self.assertGreaterEqual(row["started_ms"], 0)
            self.assertGreaterEqual(row["duration_ms"], 0)
            self.assertIn("output", row, row)
        stages = [value for event, value in events if event == "stage"]
        self.assertTrue(any(row["status"] == "running" for row in stages))
        self.assertTrue(all("output" not in row for row in stages))
        lane = next(row for row in trace["stages"] if row["name"].startswith("lexical."))
        self.assertTrue(lane["output"]["hits"])
        self.assertIn("score", lane["output"]["hits"][0])
        self.assertNotIn("text", lane["output"]["hits"][0])
        self.assertIn("Amber warranty", lane["output"]["hits"][0]["text_preview"])
        self.assertFalse(lane["output"]["hits"][0]["preview_truncated"])
        parents = next(row["output"]["parents"] for row in trace["stages"] if row["name"] == "parent_backfill")
        self.assertEqual(parents[0]["heading_path"], ["Warranty"])
        self.assertIn("Amber warranty", parents[0]["text_preview"])
        self.assertNotIn("<img", json.dumps(stages))

    def test_trace_reads_recheck_revocation_and_tenant_including_sku_scope(self):
        self.publish(sku="product-a")
        events = parse_events(self.query(sku="product-a").text)
        self.assertEqual(events[-1][0], "result")
        trace_id = events[-1][1]["trace"]["trace_id"]
        self.assertEqual(self.client.get("/traces/" + trace_id, headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get("/traces/" + trace_id, headers=self.headers(OTHER)).status_code, 404)
        self.app.set_access("alpha", "policy", (), True)
        self.assertEqual(self.client.get("/traces/" + trace_id, headers=self.headers()).status_code, 404)

    def test_failed_stream_is_generic_and_does_not_emit_success(self):
        class FailingPlanner:
            async def plan(self, request):
                raise RuntimeError("secret-from-a-broken-provider")
        self.app.components["planner"] = FailingPlanner()
        response = self.query()
        events = parse_events(response.text)
        self.assertEqual(events[-1][0], "error")
        self.assertFalse(any(event == "result" for event, _ in events))
        self.assertNotIn("secret-from-a-broken-provider", response.text)
        trace_id = events[-1][1]["trace_id"]
        trace = self.client.get("/traces/" + trace_id, headers=self.headers()).json()
        self.assertEqual(trace["status"], "error")
        self.assertEqual(trace["stages"][-1]["error_type"], "RuntimeError")

    def test_failed_stream_returns_authorized_completed_outputs_and_safe_diagnosis(self):
        self.publish()
        def handler(request):
            raise httpx.RemoteProtocolError("provider-secret text", request=request)
        config = CloudSettings("https://models.example.test/v1", "fixture-private-key", "llm", "embed", "rank")
        transport = CloudTransport(config, lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kwargs))
        self.app.components["judge"] = CloudEvidenceJudge(transport)
        response = self.query()
        events = parse_events(response.text)
        self.assertEqual(events[-1][0], "error")
        final = events[-1][1]
        self.assertIn("trace", final)
        trace = final["trace"]
        self.assertEqual(final["error"]["code"], "cloud_network_error")
        self.assertEqual(trace["error"], final["error"])
        self.assertEqual(trace["stages"][-1]["error"], final["error"])
        self.assertTrue(any(row.get("output", {}).get("hits") for row in trace["stages"]))
        self.assertEqual(trace["usage"]["calls"][-1]["error_type"], "RemoteProtocolError")
        self.assertFalse(any(kind == "result" for kind, _ in events))
        self.assertNotIn("provider-secret", response.text)
        self.assertNotIn("fixture-private-key", response.text)
        self.assertEqual(self.client.get("/traces/" + final["trace_id"], headers=self.headers()).json(), trace)

    def test_failed_stream_withdraws_completed_outputs_when_sources_are_revoked(self):
        self.publish()
        def handler(request):
            self.app.set_access("alpha", "policy", (), True)
            raise httpx.ConnectError("fixture-private-key provider detail", request=request)
        config = CloudSettings("https://models.example.test/v1", "fixture-private-key", "llm", "embed", "rank")
        self.app.components["judge"] = CloudEvidenceJudge(CloudTransport(config,
            lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)))
        events = parse_events(self.query().text)
        kind, final = events[-1]
        self.assertEqual(kind, "error")
        self.assertTrue(final["trace_id"])
        self.assertNotIn("trace", final)
        self.assertNotIn("Amber warranty", json.dumps(events))
        self.assertNotIn("fixture-private-key", json.dumps(events))
        self.assertEqual(self.client.get("/traces/" + final["trace_id"], headers=self.headers()).status_code, 404)

    def test_transient_final_trace_read_returns_persisted_result_in_recovery_trace(self):
        self.publish()
        original = self.app.get_accessible_trace
        reads = []
        def transient(access, trace_id):
            reads.append(trace_id)
            if len(reads) == 1:
                raise RuntimeError("private storage failure")
            return original(access, trace_id)
        self.app.get_accessible_trace = transient
        events = parse_events(self.query().text)
        kind, final = events[-1]
        self.assertEqual(kind, "error")
        self.assertEqual(reads, [final["trace_id"], final["trace_id"]])
        self.assertEqual(final["trace"]["status"], "evidence_preview")
        result = final["trace"]["final_result"]
        self.assertEqual(result["status"], "evidence_preview")
        self.assertTrue(result["evidence"])
        self.assertNotIn("private storage failure", json.dumps(events))

    def test_sink_failure_cannot_emit_a_success_result(self):
        def fail(_trace):
            raise RuntimeError("secret-trace-store")
        self.app.components["trace"].save = fail
        events = parse_events(self.query().text)
        self.assertEqual(events[-1][0], "error")
        self.assertFalse(any(event == "result" for event, _ in events))
        self.assertNotIn("secret-trace-store", json.dumps(events))

    def test_no_index_is_a_real_terminal_result(self):
        events = parse_events(self.query().text)
        final = events[-1][1]
        self.assertEqual(final["result"]["status"], "no_index")
        snapshot = next(row for row in final["trace"]["stages"] if row["name"] == "snapshot")
        self.assertEqual(snapshot["output"], {"generation": None})


class PipelineObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name), admin_password=ADMIN_PASSWORD)
        self.app = RagApplication(self.settings)
        self.access = AccessContext("alpha", "admin")
        job = self.app.ingest(IngestRequest("alpha", "policy", "v1", "Amber warranty lasts twelve months."))
        self.app.publish("alpha", job.job_id, None)

    async def asgi_stream(self, *, disconnect=False):
        api = create_api(replace(self.settings, api_tokens={ADMIN: Principal("alpha", "admin", admin=True)}))
        application = api.state.application
        gate, cancelled, running, disconnected = (asyncio.Event() for _ in range(4))
        class SlowPlanner:
            async def plan(self, request):
                try:
                    await gate.wait()
                    return await RulePlanner().plan(request)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
        application.components["planner"] = SlowPlanner()
        body = json.dumps({"question": "amber warranty"}).encode()
        first = True
        data = bytearray()
        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            if message["type"] == "http.response.body":
                data.extend(message.get("body", b""))
                if b'"name": "planner", "status": "running"' in data:
                    running.set()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/query/stream",
            "raw_path": b"/query/stream", "query_string": b"", "root_path": "",
            "server": ("test", 80), "client": ("test", 1),
            "headers": [(b"authorization", ("Bearer " + ADMIN).encode()), (b"content-type", b"application/json")]}
        task = asyncio.create_task(api(scope, receive, send))
        try:
            await asyncio.wait_for(running.wait(), 3)
            self.assertFalse(task.done())
            self.assertNotIn(b"event: result", data)
            if disconnect:
                disconnected.set()
            else:
                gate.set()
            await asyncio.wait_for(task, 4)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        return application, parse_events(data.decode()), cancelled.is_set()

    async def test_http_sends_running_event_before_planner_finishes(self):
        _, events, was_cancelled = await self.asgi_stream()
        self.assertFalse(was_cancelled)
        self.assertEqual(events[-1][0], "result")

    async def test_http_disconnect_cancels_model_work_and_saves_trace(self):
        app, events, was_cancelled = await self.asgi_stream(disconnect=True)
        self.assertTrue(was_cancelled)
        self.assertFalse(any(event == "result" for event, _ in events))
        trace = app.get_trace("alpha", events[0][1]["trace_id"])
        self.assertEqual(trace["status"], "cancelled")
        self.assertEqual(trace["stages"][-1]["status"], "cancelled")

    async def test_observation_precedes_completion_and_cancel_is_persisted(self):
        entered = asyncio.Event()
        gate = asyncio.Event()
        class SlowPlanner:
            async def plan(self, request):
                entered.set()
                await gate.wait()
                return await RulePlanner().plan(request)
        self.app.components["planner"] = SlowPlanner()
        observed = []
        task = asyncio.create_task(self.app.query(QueryRequest(self.access, "warranty"),
            observer=lambda event, value: observed.append((event, dict(value)))))
        await asyncio.wait_for(entered.wait(), 2)
        self.assertFalse(task.done())
        self.assertTrue(any(event == "stage" and row["name"] == "planner" and row["status"] == "running" for event, row in observed))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        trace_id = observed[0][1]["trace_id"]
        trace = self.app.get_trace("alpha", trace_id)
        self.assertEqual(trace["status"], "cancelled")
        self.assertEqual(trace["stages"][-1]["status"], "cancelled")
        self.assertEqual(observed[-1][1]["status"], "cancelled")
        self.assertEqual(trace["error"]["code"], "query_cancelled")
        self.assertEqual(trace["stages"][-1]["error"], trace["error"])
        self.assertIn("已取消", trace["error"]["message"])
        self.assertNotIn("final_result", trace)

    async def test_timeout_has_a_terminal_trace(self):
        class SlowPlanner:
            async def plan(self, request):
                await asyncio.Event().wait()
        # This budget also bounds real trace persistence. Leave enough time
        # for file I/O under load while still exercising the stage timeout.
        app = RagApplication(replace(self.settings, stage_timeout_seconds=1))
        app.components["planner"] = SlowPlanner()
        observed = []
        with self.assertRaises(CapabilityError):
            await app.query(QueryRequest(self.access, "warranty"), observer=lambda event, value: observed.append((event, dict(value))))
        trace = app.get_trace("alpha", observed[0][1]["trace_id"])
        self.assertEqual(trace["status"], "timeout")
        self.assertEqual(trace["stages"][-1]["status"], "timeout")
        self.assertEqual(trace.get("error", {}).get("code"), "query_timeout")
        self.assertEqual(trace["stages"][-1].get("error", {}).get("code"), "query_timeout")

    async def test_whole_query_deadline_is_timeout_including_its_interrupted_stage(self):
        class SlowPlanner:
            async def plan(self, request):
                await asyncio.Event().wait()
        # Exercise the outer deadline while leaving real file persistence a
        # useful reserve on Windows as well as faster local filesystems.
        app = RagApplication(replace(self.settings, timeout_seconds=2, stage_timeout_seconds=3))
        app.components["planner"] = SlowPlanner()
        observed = []
        with self.assertRaises(CapabilityError):
            await app.query(QueryRequest(self.access, "warranty"), observer=lambda event, value: observed.append((event, dict(value))))
        trace = app.get_trace("alpha", observed[0][1]["trace_id"])
        self.assertEqual(trace["status"], "timeout")
        self.assertEqual(trace["stages"][-1]["status"], "timeout")
        self.assertEqual(observed[-1][1]["status"], "timeout")

    async def test_validated_previews_are_bounded_and_retain_source_character_counts(self):
        app = RagApplication(replace(self.settings, child_chars=500))
        text = "Amber warranty " + "durable fabric warranty. " * 80
        job = app.ingest(IngestRequest("alpha", "long-policy", "v1", "# Materials\n" + text))
        snapshot = app.store.open_snapshot("alpha")
        published = app.publish("alpha", job.job_id, snapshot.generation)
        result = await app.query(QueryRequest(self.access, "durable fabric warranty"))
        trace = app.get_accessible_trace(self.access, result.trace_id)
        snapshot = app.store.open_snapshot("alpha", published.generation)
        chunks = {c.chunk_id: c for c in app.store.list_chunks(snapshot, self.access, None)}
        parents = {p.parent_id: p for p in app.store.get_parents(snapshot, tuple(trace["source_parent_ids"]), self.access)}
        truncated_hit = truncated_parent = False
        for row in trace["stages"]:
            for hit in row.get("output", {}).get("hits", ()):
                source = chunks[hit["chunk_id"]].text
                self.assertEqual(hit["text_preview"], source[:320])
                self.assertEqual(hit["text_chars"], len(source))
                self.assertEqual(hit["preview_truncated"], len(source) > 320)
                truncated_hit |= hit["preview_truncated"]
            for parent in row.get("output", {}).get("parents", ()):
                source = parents[parent["parent_id"]]
                self.assertEqual(parent["text_preview"], source.text[:600])
                self.assertEqual(parent["text_chars"], len(source.text))
                self.assertEqual(parent["heading_path"], list(source.heading_path))
                self.assertEqual(parent["preview_truncated"], len(source.text) > 600)
                truncated_parent |= parent["preview_truncated"]
        self.assertTrue(truncated_hit)
        self.assertTrue(truncated_parent)

    async def test_unselected_hit_sources_and_failed_traces_remain_acl_guarded(self):
        job = self.app.ingest(IngestRequest("alpha", "other", "v1", "Amber warranty is explained here."))
        current = self.app.store.open_snapshot("alpha")
        self.app.publish("alpha", job.job_id, current.generation)
        result = await self.app.query(QueryRequest(self.access, "amber warranty", top_k=1))
        trace = self.app.get_trace("alpha", result.trace_id)
        self.assertGreater(len(trace["source_parent_ids"]), len(result.evidence))
        selected = {e.document_id for e in result.evidence}
        unselected = ({"policy", "other"} - selected).pop()
        self.app.set_access("alpha", unselected, (), True)
        with self.assertRaises(NotFoundError):
            self.app.get_accessible_trace(self.access, result.trace_id)

    async def test_unvalidated_hit_never_becomes_stage_output(self):
        class ForgedRetriever:
            async def search(self, query, chunks, limit):
                forged = replace(chunks[0], text="private-forged-chunk")
                return (Hit(query.query_id, forged, "lexical", 1.0, 1),)
        self.app.components["lexical"] = ForgedRetriever()
        observed = []
        with self.assertRaises(ValidationError):
            await self.app.query(QueryRequest(self.access, "warranty"), observer=lambda event, value: observed.append((event, dict(value))))
        trace = self.app.get_trace("alpha", observed[0][1]["trace_id"])
        self.assertNotIn("private-forged-chunk", json.dumps(trace))
        self.assertNotIn("final_result", trace)
        self.assertEqual(trace["stages"][-1]["status"], "error")
        self.assertEqual(trace["stages"][-1]["reason"], "output_validation_failed")
        self.assertNotIn("output", trace["stages"][-1])
        self.assertFalse(any(event == "stage" and row["name"].startswith("lexical.") and row["status"] == "ok" for event, row in observed))

    async def test_invalid_contracts_fail_the_producing_stage_before_success_is_emitted(self):
        class InvalidPlanner:
            async def plan(self, request):
                return {"queries": []}
        class InvalidDecision:
            async def decide(self, state):
                return {"action": "invented"}
        class InvalidJudge:
            async def assess(self, queries, evidence):
                return {"valid": True}
        class InvalidGenerator:
            async def generate(self, plan, evidence):
                return {"answer": "unverified-secret"}
        class InvalidVerifier:
            async def verify(self, draft, evidence):
                return {"valid": True}
        for role, adapter in (("planner", InvalidPlanner()), ("decision", InvalidDecision()),
                              ("judge", InvalidJudge()), ("generator", InvalidGenerator()),
                              ("verifier", InvalidVerifier())):
            with self.subTest(role=role):
                app = RagApplication(self.settings)
                app.components[role] = adapter
                if role == "generator":
                    app.descriptions["generator"]["enabled"] = True
                observed = []
                with self.assertRaises(ValidationError):
                    await app.query(QueryRequest(self.access, "amber warranty"), observer=lambda event, value: observed.append((event, dict(value))))
                trace = app.get_trace("alpha", observed[0][1]["trace_id"])
                failed = trace["stages"][-1]
                self.assertTrue(failed["name"].startswith(role))
                self.assertEqual(failed["status"], "error")
                self.assertEqual(failed["reason"], "output_validation_failed")
                self.assertNotIn("output", failed)
                self.assertFalse(any(event == "stage" and row["index"] == failed["index"] and row["status"] == "ok" for event, row in observed))
                self.assertNotIn("unverified-secret", json.dumps(trace))

    async def test_parent_preview_is_not_recorded_before_source_integrity_validation(self):
        original = self.app.store.get_parents
        def forged(*args):
            return tuple(replace(parent, text="private-forged-parent") for parent in original(*args))
        self.app.store.get_parents = forged
        observed = []
        with self.assertRaises(ValidationError):
            await self.app.query(QueryRequest(self.access, "amber warranty"), observer=lambda event, value: observed.append((event, dict(value))))
        trace = self.app.get_trace("alpha", observed[0][1]["trace_id"])
        self.assertNotIn("private-forged-parent", json.dumps(trace))
        self.assertEqual(trace["stages"][-1]["name"], "parent_backfill")
        self.assertEqual(trace["stages"][-1]["status"], "error")
        self.assertNotIn("output", trace["stages"][-1])

    async def test_failed_trace_with_validated_hits_is_acl_checked(self):
        class FailingJudge:
            async def assess(self, queries, evidence):
                raise RuntimeError("provider failure")
        self.app.components["judge"] = FailingJudge()
        observed = []
        with self.assertRaises(CapabilityError):
            await self.app.query(QueryRequest(self.access, "warranty"), observer=lambda event, value: observed.append((event, dict(value))))
        trace_id = observed[0][1]["trace_id"]
        self.assertTrue(self.app.get_accessible_trace(self.access, trace_id)["source_parent_ids"])
        self.app.set_access("alpha", "policy", (), True)
        with self.assertRaises(NotFoundError):
            self.app.get_accessible_trace(self.access, trace_id)

    async def test_concurrent_queries_keep_observer_events_and_results_separate(self):
        first, second = [], []
        results = await asyncio.gather(
            self.app.query(QueryRequest(self.access, "amber"), observer=lambda event, value: first.append((event, dict(value)))),
            self.app.query(QueryRequest(self.access, "warranty"), observer=lambda event, value: second.append((event, dict(value)))),
        )
        self.assertNotEqual(results[0].trace_id, results[1].trace_id)
        self.assertEqual(first[0][1]["trace_id"], results[0].trace_id)
        self.assertEqual(second[0][1]["trace_id"], results[1].trace_id)
        for result, question in zip(results, ("amber", "warranty")):
            trace = self.app.get_accessible_trace(self.access, result.trace_id)
            self.assertEqual(trace["question"], question)
            self.assertEqual(trace["model_calls"], 0)


if __name__ == "__main__":
    unittest.main()
