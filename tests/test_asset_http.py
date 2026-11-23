"""Exercise image-library behavior through authenticated HTTP, without cloud calls."""

import base64
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from adaptive_rag.api import create_api
from adaptive_rag.config import Principal, Settings
from support import ADMIN_PASSWORD


class AssetHTTPTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = Settings(data_dir=Path(temp.name), browser_tenant="alpha", admin_password=ADMIN_PASSWORD,
                                 api_tokens={"other-tenant-test-token-000000": Principal("beta", "operator", admin=True)})
        self.api = create_api(self.settings)
        self.admin = self.client()
        self.login(self.admin)
        image = BytesIO()
        Image.new("RGB", (120, 80), "#2f5bd3").save(image, "PNG")
        self.content = image.getvalue()
        self.body = {"asset_id": "rag-flow", "title": "RAG 架构图 🔎",
                     "description": "文档解析、检索和带引用回答的流程。",
                     "tags": ["RAG", "架构"], "content_base64": base64.b64encode(self.content).decode(),
                     "data_domains": ["public"]}

    def client(self, api=None):
        client = TestClient(api or self.api, raise_server_exceptions=False)
        self.addCleanup(client.close)
        return client

    def login(self, client, username="ragadmin", password=ADMIN_PASSWORD):
        response = client.post("/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def create(self, **changes):
        response = self.admin.post("/assets", json=self.body | changes)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_crud_content_thumbnail_cas_and_restart(self):
        asset = self.create()
        self.assertNotIn("object_key", asset)
        self.assertEqual(self.admin.post("/assets", json=self.body).json()["revision_id"], asset["revision_id"])
        original = self.admin.get(asset["content_url"])
        self.assertEqual(original.content, self.content)
        self.assertEqual(original.headers["content-type"], "image/png")
        self.assertIn("no-store", original.headers["cache-control"])
        preview = self.admin.get(asset["thumbnail_url"])
        self.assertEqual(preview.headers["content-type"], "image/jpeg")
        with Image.open(BytesIO(preview.content)) as image:
            self.assertLessEqual(max(image.size), 640)

        restarted = self.client(create_api(self.settings))
        restarted.cookies.update(self.admin.cookies)
        self.assertEqual(restarted.get("/assets").json()["assets"][0]["title"], self.body["title"])
        update = {"expected_revision": asset["revision_id"], "title": "采购流程图",
                  "description": "采购审批与付款", "ocr_text": "", "tags": ["采购"]}
        response = restarted.patch("/assets/rag-flow", json=update)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data_domains"], ["public"])
        revision = response.json()["revision_id"]
        self.assertNotEqual(revision, asset["revision_id"])
        self.assertEqual(self.admin.patch("/assets/rag-flow", json=update).status_code, 409)
        self.assertEqual(self.admin.delete("/assets/rag-flow", params={"expected_revision": asset["revision_id"]}).status_code, 409)
        self.assertEqual(self.admin.delete("/assets/rag-flow", params={"expected_revision": revision}).status_code, 204)
        self.assertEqual(restarted.get("/assets/rag-flow/content").status_code, 404)
        self.assertEqual(restarted.get("/assets").json()["assets"], [])

    def test_lexical_search_and_trace_replay_do_not_call_models(self):
        self.create()
        response = self.admin.post("/assets/search", json={"question": "RAG 架构", "top_k": 5})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["search_mode"], "lexical")
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(len(result["hits"]), 1)
        self.assertTrue(result["stages"])
        self.assertGreaterEqual(result["duration_ms"], 0)
        restarted = self.client(create_api(self.settings))
        restarted.cookies.update(self.admin.cookies)
        self.assertEqual(restarted.get("/assets/traces/" + result["trace_id"]).json(), result)
        empty = self.admin.post("/assets/search", json={"question": "unmatched_word_82910"}).json()
        self.assertEqual(empty["status"], "empty")
        self.assertEqual(empty["hits"], [])

    def test_reader_permissions_tenant_isolation_and_immediate_revocation(self):
        response = self.admin.post("/admin/users", json={"username": "alice", "password": "alice-password"})
        self.assertEqual(response.status_code, 201)
        reader = self.client()
        subject = self.login(reader, "alice", "alice-password")["subject_id"]
        admin_subject = self.admin.get("/console/session").json()["subject_id"]
        asset = self.create(allowed_subjects=[subject, admin_subject])
        result = reader.post("/assets/search", json={"question": "RAG"}).json()
        self.assertEqual(len(result["hits"]), 1)
        for method, path, body in (
            ("POST", "/assets", self.body), ("POST", "/assets/index", {}),
            ("POST", "/assets/rag-flow/reindex", None),
            ("PATCH", "/assets/rag-flow", {}), ("DELETE", "/assets/rag-flow", None),
        ):
            response = reader.request(method, path, **({"json": body} if body is not None else {}))
            self.assertEqual(response.status_code, 403, (method, path, response.text))
        foreign = self.client()
        foreign.headers["Authorization"] = "Bearer other-tenant-test-token-000000"
        self.assertEqual(foreign.get("/assets").json()["assets"], [])
        self.assertEqual(foreign.get(asset["content_url"]).status_code, 404)
        self.assertEqual(foreign.get("/assets/traces/" + result["trace_id"]).status_code, 404)
        update = {"expected_revision": asset["revision_id"], "title": asset["title"],
                  "description": asset["description"], "ocr_text": "", "tags": asset["tags"],
                  "allowed_subjects": [admin_subject]}
        self.assertEqual(self.admin.patch("/assets/rag-flow", json=update).status_code, 200)
        self.assertEqual(reader.get(asset["content_url"]).status_code, 404)
        self.assertEqual(reader.get(asset["thumbnail_url"]).status_code, 404)
        self.assertEqual(reader.get("/assets/traces/" + result["trace_id"]).status_code, 404)

    def test_invalid_images_and_untrusted_identity_are_rejected(self):
        anonymous = self.client()
        self.assertEqual(anonymous.get("/assets").status_code, 401)
        self.assertEqual(anonymous.post("/assets/search", json={"question": "RAG"}).status_code, 401)
        for change in ({"content_base64": "bad!"}, {"content_base64": base64.b64encode(b"<svg/>").decode()},
                       {"title": "  "}, {"asset_id": "../private"}, {"tenant_id": "beta"}):
            response = self.admin.post("/assets", json=self.body | change)
            self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(self.admin.get("/assets").json()["assets"], [])
        for query in ({"question": " "}, {"question": "RAG", "top_k": 21},
                      {"question": "RAG", "subject_id": "ragadmin"}):
            self.assertIn(self.admin.post("/assets/search", json=query).status_code, (400, 422))

    def test_deleted_asset_id_can_be_uploaded_again(self):
        asset = self.create()
        self.assertEqual(self.admin.delete("/assets/rag-flow").status_code, 204)
        again = self.admin.post("/assets", json=self.body | {"title": "新版流程图"})
        self.assertEqual(again.status_code, 201, again.text)
        self.assertNotEqual(again.json()["revision_id"], asset["revision_id"])
        self.assertEqual(self.admin.get("/assets/rag-flow").json()["title"], "新版流程图")
        # A live asset_id still conflicts, and the message names the supported way forward.
        conflict = self.admin.post("/assets", json=self.body | {"title": "又一版"})
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertIn("expected_revision", conflict.text)

    def test_index_failure_and_search_failure_remain_replayable_and_sanitized(self):
        class BrokenEmbedder:
            async def embed(self, texts):
                raise RuntimeError("https://provider.test?key=synthetic-secret")

        service = self.api.state.application.assets
        service.embedder = BrokenEmbedder()
        asset = self.create()
        response = self.admin.post("/assets/index", json={"limit": 10})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["failed"], 1)
        self.assertNotIn("synthetic-secret", response.text)
        failed = self.admin.get("/assets/rag-flow").json()
        self.assertEqual(failed["indexing_status"], "failed")
        response = self.admin.post("/assets/rag-flow/reindex")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["indexing_status"], "pending")
        result = self.admin.post("/assets/search", json={"question": "RAG"}).json()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["hits"], [])
        self.assertTrue(result["trace_id"])
        self.assertNotIn("synthetic-secret", str(result))
        self.assertEqual(self.admin.get("/assets/traces/" + result["trace_id"]).json(), result)
        # A failed vector index must not make stored images unavailable.
        self.assertEqual(self.admin.get(asset["content_url"]).content, self.content)


if __name__ == "__main__":
    unittest.main()
