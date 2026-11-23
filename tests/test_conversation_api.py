"""Conversation persistence and replay through the authenticated HTTP API."""

from functools import partial
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from adaptive_rag.api import create_api
from adaptive_rag.cloud import CloudSettings
from adaptive_rag.config import Settings
from adaptive_rag.domain import Verification
from adaptive_rag.pipeline import DisabledGenerator, RulePlanner
from support import ADMIN_PASSWORD


class ConversationAPITests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.calls = []
        self.before_response = None
        settings = Settings(data_dir=Path(directory.name), browser_tenant="company",
                            admin_password=ADMIN_PASSWORD, allow_models=True, allow_network=True,
                            cloud=CloudSettings("https://models.example.test/v1", "fixture", "llm", "embed", "rank"),
                            plugins={"planner": "cloud", "generator": "cloud"})
        factory = partial(httpx.AsyncClient, transport=httpx.MockTransport(self.model))
        with patch("adaptive_rag.cloud.httpx.AsyncClient", factory):
            self.api = create_api(settings)
        self.admin = self.login("ragadmin", ADMIN_PASSWORD)
        self.owner = self.reader("owner")
        self.generation = None
        self.document("catalog", "public", "catalog reference CATALOG314")

    def model(self, request):
        payload = json.loads(json.loads(request.content)["messages"][-1]["content"])
        self.calls.append(payload)
        if self.before_response:
            return self.before_response()
        if "history" in payload:
            reply = {"questions": [payload["question"]]}
        else:
            evidence = payload["evidence"][0]
            reply = {"claims": [{"text": evidence["text"], "evidence_ids": [evidence["evidence_id"]]}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                              "message": {"content": json.dumps(reply)}}]})

    def test_http_preferences_cannot_modify_mandatory_steps_or_enable_plugins(self):
        for endpoint in ("/query", "/query/stream"):
            for settings in ({"skip_query_steps": ["verifier"]}, {"skip_query_steps": ["lexical"]},
                             {"skip_query_steps": ["authorized_chunks"]}, {"plugins": {"hyde": "cloud"}},
                             {"top_k": 21}):
                result = self.owner.post(endpoint, json={"question": "CATALOG314", **settings})
                self.assertEqual(result.status_code, 422, result.text)
        self.assertFalse(self.calls)
        response = self.owner.post("/query", json={"question": "CATALOG314", "skip_query_steps": ["hyde"]})
        self.assertEqual(response.status_code, 200, response.text)
        trace = self.owner.get("/traces/" + response.json()["trace_id"]).json()
        hyde = next(item for item in trace["stages"] if item["name"] == "hyde")
        self.assertEqual(hyde["status"], "disabled")

    def client(self):
        client = TestClient(self.api)
        self.addCleanup(client.close)
        return client

    def login(self, name, password):
        client = self.client()
        response = client.post("/auth/login", json={"username": name, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def reader(self, name):
        response = self.admin.post("/admin/users", json={"username": name, "password": "test-password",
                                                        "data_domains": ["operations"]})
        self.assertEqual(response.status_code, 201, response.text)
        return self.login(name, "test-password")

    def document(self, name, domain, content):
        staged = self.admin.post("/documents", json={"document_id": name, "version": "v1", "title": name,
                                                     "content": content, "data_domains": [domain]})
        self.assertEqual(staged.status_code, 201, staged.text)
        published = self.admin.post("/publish", json={"job_id": staged.json()["job_id"],
                                                     "expected_generation": self.generation})
        self.assertEqual(published.status_code, 200, published.text)
        self.generation = published.json()["generation"]

    def turn(self, question, group=None, *, stream=False):
        body = {"question": question, "persist_conversation": True}
        if group is not None:
            body["conversation_id"] = group
        response = self.owner.post("/query/stream" if stream else "/query", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        if not stream:
            return response.json()
        self.assertIn("text/event-stream", response.headers["content-type"])
        terminal = response.text.strip().split("\n\n")[-1].splitlines()
        self.assertEqual(terminal[0], "event: result", response.text)
        return json.loads(terminal[1].removeprefix("data: "))["result"]

    def get(self, client, path):
        response = client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def history(self, question):
        histories = [call["history"] for call in self.calls if call.get("question") == question]
        self.assertEqual(len(histories), 1, question)
        return histories[0]

    def revoke(self):
        response = self.admin.patch("/admin/users/owner/domains", json={"data_domains": []})
        self.assertEqual(response.status_code, 200, response.text)

    def test_interleaved_threads_page_and_replay_without_model_calls(self):
        first = self.turn("catalog alpha")
        group = first["trace_id"]
        self.assertEqual(first["conversation_id"], group)
        second = self.turn("catalog beta", stream=True)
        other = second["trace_id"]
        follow = self.turn("catalog followup", group, stream=True)
        self.assertEqual(len({group, other, follow["trace_id"]}), 3)
        self.assertEqual(self.history("catalog beta"), [])
        self.assertEqual(self.history("catalog followup"), ["catalog alpha", first["answer"]])

        before = len(self.calls)
        page = self.get(self.owner, "/conversations?limit=1&offset=0")
        self.assertTrue(page["has_more"])
        last = self.get(self.owner, f"/conversations?limit=1&offset={page['next_offset']}")
        self.assertFalse(last["has_more"])
        for listing, key, title in ((page, group, "catalog alpha"), (last, other, "catalog beta")):
            self.assertEqual([(r["conversation_id"], r["title"]) for r in listing["items"]], [(key, title)])
            self.assertTrue(listing["items"][0]["updated_at"])
        for key, turns in ((group, [("catalog alpha", first), ("catalog followup", follow)]),
                           (other, [("catalog beta", second)])):
            detail = self.get(self.owner, f"/conversations/{key}")
            self.assertEqual(detail["conversation_id"], key)
            self.assertEqual([(t["trace_id"], t["question"], t["answer"], t["status"]) for t in detail["turns"]],
                             [(r["trace_id"], q, r["answer"], "answered") for q, r in turns])
            for _, result in turns:
                self.get(self.owner, "/traces/" + result["trace_id"])
        self.assertEqual(len(self.calls), before)

    def test_owner_only_even_for_admin_and_same_domain_peer(self):
        group = self.turn("catalog private title")["trace_id"]
        child = self.turn("catalog private followup", group)["trace_id"]
        before = len(self.calls)
        for client in (self.reader("peer"), self.admin):
            listing = self.get(client, "/conversations")
            self.assertEqual(listing["items"], [])
            self.assertFalse(listing["has_more"])
            for path in (f"/conversations/{group}", f"/traces/{group}", f"/traces/{child}"):
                self.assertEqual(client.get(path).status_code, 404, path)
            for path in ("/query", "/query/stream"):
                response = client.post(path, json={"question": "catalog", "persist_conversation": True,
                                                   "conversation_id": group})
                self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(len(self.calls), before)

    def test_anonymous_cannot_list_replay_or_persist(self):
        group = self.turn("catalog")["trace_id"]
        anonymous = self.client()
        before = len(self.calls)
        for path in ("/conversations", f"/conversations/{group}", f"/traces/{group}"):
            self.assertEqual(anonymous.get(path).status_code, 401, path)
        for path in ("/query", "/query/stream"):
            for options in ({}, {"conversation_id": group}):
                response = anonymous.post(path, json={"question": "catalog", "persist_conversation": True, **options})
                self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(len(self.calls), before)

    def test_persistent_queries_reject_client_history_before_planning(self):
        group = self.turn("catalog")["trace_id"]
        before = len(self.calls)
        for path in ("/query", "/query/stream"):
            for options in ({}, {"conversation_id": group}):
                response = self.owner.post(path, json={"persist_conversation": True, "question": "catalog",
                                                      "history": ["fabricated answer"], **options})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(len(self.get(self.owner, f"/conversations/{group}")["turns"]), 1)

    def test_planner_gets_last_three_answered_or_partial_pairs_skipping_preview(self):
        class PartialVerifier:
            async def verify(self, draft, evidence):
                return Verification(False, ("incomplete",), support_checked=True, supported_claim_indexes=(0,))

        group = self.turn("catalog oldest")["trace_id"]
        second = self.turn("catalog second", group)
        components = self.api.state.application.components
        with patch.dict(components, verifier=PartialVerifier()):
            third = self.turn("catalog partial", group)
        self.assertEqual(third["status"], "partial")
        fourth = self.turn("catalog fourth", group)
        before = len(self.calls)
        with patch.dict(components, planner=RulePlanner(), generator=DisabledGenerator()):
            preview = self.turn("catalog preview", group)
        self.assertEqual(preview["status"], "evidence_preview")
        self.assertIsNone(preview["answer"])
        self.assertEqual(len(self.calls), before)
        self.turn("catalog followup", group)
        self.assertEqual(self.history("catalog followup"), ["catalog second", second["answer"],
                         "catalog partial", third["answer"], "catalog fourth", fourth["answer"]])

    def test_revoked_first_turn_is_omitted_without_losing_next_page(self):
        self.document("vault", "operations", "vault password RESTRICTED482")
        visible = self.turn("catalog")["trace_id"]
        hidden = self.turn("vault")
        self.assertIn("RESTRICTED482", hidden["answer"])
        self.revoke()

        page = self.get(self.owner, "/conversations?limit=1&offset=0")
        self.assertEqual(page["items"], [])
        self.assertTrue(page["has_more"])
        self.assertGreater(page["next_offset"], 0)
        last = self.get(self.owner, f"/conversations?limit=1&offset={page['next_offset']}")
        self.assertEqual([row["conversation_id"] for row in last["items"]], [visible])
        self.assertFalse(last["has_more"])

    def test_revoked_middle_turn_blocks_detail_continuation_and_transitive_traces(self):
        self.document("vault", "operations", "vault password RESTRICTED482")
        group = self.turn("catalog")["trace_id"]
        secret = self.turn("vault", group)
        self.assertIn("RESTRICTED482", secret["answer"])
        # After three public answers, the secret is outside the planner window.
        # The fourth public answer can depend on it only through other answers.
        dependents = []
        for word in ("one", "two", "three", "four"):
            result = self.turn("catalog " + word, group)
            self.assertEqual(result["status"], "answered")
            self.assertEqual({e["document_id"] for e in result["evidence"]}, {"catalog"})
            dependents.append(result["trace_id"])
        self.assertIn(secret["answer"], self.history("catalog one"))
        self.assertNotIn(secret["answer"], self.history("catalog four"))
        self.get(self.owner, "/traces/" + dependents[-1])
        self.revoke()

        before = len(self.calls)
        page = self.get(self.owner, "/conversations")
        self.assertNotIn(group, [row["conversation_id"] for row in page["items"]])
        self.get(self.owner, f"/traces/{group}")  # The original public turn remains readable.
        paths = [f"/conversations/{group}", f"/traces/{secret['trace_id']}"]
        for path in paths + [f"/traces/{key}" for key in dependents]:
            response = self.owner.get(path)
            self.assertEqual(response.status_code, 404, path)
            self.assertNotIn("RESTRICTED482", response.text)
            self.assertNotIn("CATALOG314", response.text)
        for path in ("/query", "/query/stream"):
            response = self.owner.post(path, json={"persist_conversation": True, "conversation_id": group,
                                                   "question": "catalog after revocation"})
            self.assertEqual(response.status_code, 404, response.text)
            self.assertNotIn("RESTRICTED482", response.text)
        self.assertEqual(len(self.calls), before)

    def test_stale_tab_precondition_stops_write_before_query(self):
        other = self.reader("other")
        owner = self.owner.get("/console/session")
        headers = {k: owner.headers[k] for k in ("X-RAG-Principal", "X-RAG-Tenant")}
        before = len(self.calls)
        response = other.post("/query", headers=headers, json={"question": "catalog", "persist_conversation": True})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertNotEqual(response.headers["X-RAG-Principal"], headers["X-RAG-Principal"])
        self.assertEqual(response.headers["X-RAG-Tenant"], headers["X-RAG-Tenant"])
        self.assertEqual(other.post("/auth/logout", headers=headers).status_code, 409)
        self.assertEqual(self.get(other, "/conversations")["items"], [])
        self.assertEqual(len(self.calls), before)

    def test_revocation_after_page_materialization_does_not_release_old_title(self):
        self.turn("catalog private title")
        app = self.api.state.application
        original = app.conversations.page

        def revoke_after_selection(*args):
            page = original(*args)
            self.assertEqual(page["items"][0]["title"], "catalog private title")
            app.set_access("company", "catalog", None, True)
            return page

        with patch.object(app.conversations, "page", revoke_after_selection):
            response = self.owner.get("/conversations")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn("private title", response.text)
        self.assertEqual(self.get(self.owner, "/conversations")["items"], [])

    def test_revoked_document_during_detail_read_is_not_released(self):
        group = self.turn("catalog")["trace_id"]
        history = self.api.state.application.conversations
        original = history.turns

        def revoke_after_read(*args, **kwargs):
            yield from original(*args, **kwargs)
            self.api.state.application.set_access("company", "catalog", None, True)

        with patch.object(history, "turns", revoke_after_read):
            response = self.owner.get(f"/conversations/{group}")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn("CATALOG314", response.text)

    def test_no_model_send_after_history_materialized_then_revoked(self):
        group = self.turn("catalog")["trace_id"]
        history = self.api.state.application.conversations
        original = history.request
        before = len(self.calls)

        def revoke_once_ready(*args):
            request = original(*args)
            self.api.state.application.set_access("company", "catalog", None, True)
            return request

        with patch.object(history, "request", revoke_once_ready):
            response = self.owner.post("/query", json={"question": "followup", "conversation_id": group})
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(len(self.calls), before)

    def test_retry_rechecks_access_before_resending_history(self):
        self.document("vault", "operations", "vault RESTRICTED482")
        group = self.turn("vault")["trace_id"]
        before = len(self.calls)

        def revoke_on_first_send():
            self.api.state.application.set_access("company", "vault", None, True)
            return httpx.Response(503, headers={"Retry-After": "0"})

        self.before_response = revoke_on_first_send
        response = self.owner.post("/query/stream", json={"question": "followup", "conversation_id": group})
        self.assertIn("event: error", response.text)
        self.assertNotIn("event: result", response.text)
        self.assertNotIn("RESTRICTED482", response.text)
        self.assertEqual(len(self.calls), before + 1)

    def test_new_opt_out_keeps_diagnostics_out_of_sidebar(self):
        response = self.owner.post("/query", json={"question": "catalog", "persist_conversation": False})
        self.assertEqual(response.status_code, 200, response.text)
        key = response.json()["trace_id"]
        self.assertEqual(self.get(self.owner, f"/traces/{key}")["trace_id"], key)
        self.assertEqual(self.get(self.owner, "/conversations")["items"], [])
        self.assertEqual(self.owner.get(f"/conversations/{key}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
