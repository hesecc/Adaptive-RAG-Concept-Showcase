"""Data-domain grants through real account, ingestion and retrieval HTTP seams."""

import base64
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from adaptive_rag.api import create_api
from adaptive_rag.config import Principal, Settings
from support import ADMIN_PASSWORD


class DataDomainHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(data_dir=Path(directory.name), browser_tenant="company", admin_password=ADMIN_PASSWORD)
        self.api = create_api(self.settings)
        self.admin = self.client()
        self.assertEqual(self.admin.post("/auth/login", json={
            "username": "ragadmin", "password": ADMIN_PASSWORD}).status_code, 200)
        self.generation = None

    def client(self, api=None):
        client = TestClient(api or self.api, raise_server_exceptions=False)
        self.addCleanup(client.close)
        return client

    def reader(self, name, domains):
        response = self.admin.post("/admin/users", json={
            "username": name, "password": "test-password", "data_domains": domains})
        self.assertEqual(response.status_code, 201, response.text)
        client = self.client()
        response = client.post("/auth/login", json={"username": name, "password": "test-password"})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def document(self, name, domains, marker, **changes):
        response = self.admin.post("/documents", json={
            "document_id": name, "version": "v1", "title": name,
            "content": "# Company policy\ncompany policy " + marker,
            "data_domains": domains, **changes})
        self.assertEqual(response.status_code, 201, response.text)
        response = self.admin.post("/publish", json={
            "job_id": response.json()["job_id"], "expected_generation": self.generation})
        self.assertEqual(response.status_code, 200, response.text)
        self.generation = response.json()["generation"]

    def query(self, reader):
        response = reader.post("/query", json={"question": "company policy", "top_k": 20})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_operations_finance_public_and_unclassified_are_isolated_everywhere(self):
        operator = self.reader("operator", ["operations"])
        finance = self.reader("finance", ["finance"])
        self.document("operations-manual", ["operations"], "OPERATIONSECRET771")
        self.document("financial-report", ["finance"], "FINANCESECRET882")
        self.document("shared-policy", ["public"], "PUBLICDOC993")
        self.document("legacy-unclassified", [], "UNCLASSIFIED114")
        operations = self.query(operator)
        self.assertEqual({e["document_id"] for e in operations["evidence"]},
                         {"operations-manual", "shared-policy"})
        self.assertEqual({e["document_id"] for e in self.query(finance)["evidence"]},
                         {"financial-report", "shared-policy"})
        self.assertEqual(len(self.query(self.admin)["evidence"]), 4)
        trace = operator.get("/traces/" + operations["trace_id"])
        self.assertEqual(trace.status_code, 200, trace.text)
        self.assertNotIn("FINANCESECRET882", trace.text)
        self.assertNotIn("UNCLASSIFIED114", trace.text)
        self.assertEqual(finance.get("/traces/" + operations["trace_id"]).status_code, 404)
        peer = self.reader("peer-operator", ["operations"])
        self.assertEqual(peer.get("/traces/" + operations["trace_id"]).status_code, 404)
        privileged = self.query(self.admin)
        denied = next(e for e in privileged["evidence"] if e["document_id"] == "financial-report")
        evidence_path = f"/evidence/{self.generation}/{denied['evidence_id']}"
        self.assertEqual(operator.get(evidence_path).status_code, 404)
        stream = operator.post("/query/stream", json={"question": "company policy", "top_k": 20})
        self.assertEqual(stream.status_code, 200)
        self.assertIn("event: result", stream.text)
        self.assertNotIn("FINANCESECRET882", stream.text)
        self.assertNotIn("UNCLASSIFIED114", stream.text)
        self.assertEqual(operator.get("/documents").status_code, 403)
        self.assertEqual(len(self.admin.get("/documents").json()["documents"]), 4)

    def test_account_grants_change_existing_session_and_historical_access(self):
        operator = self.reader("operator", ["operations"])
        self.document("operations-manual", ["operations"], "OPERATIONSECRET771")
        self.document("financial-report", ["finance"], "FINANCESECRET882")
        old = self.query(operator)
        source = old["evidence"][0]
        response = self.admin.patch("/admin/users/operator/domains", json={"data_domains": ["finance"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(operator.get("/console/session").json()["data_domains"]), {"public", "finance"})
        self.assertEqual({e["document_id"] for e in self.query(operator)["evidence"]}, {"financial-report"})
        self.assertEqual(operator.get("/traces/" + old["trace_id"]).status_code, 404)
        self.assertEqual(operator.get(f"/evidence/{old['generation']}/{source['evidence_id']}").status_code, 404)
        restarted = self.client(create_api(self.settings))
        restarted.cookies.update(operator.cookies)
        self.assertEqual(set(restarted.get("/console/session").json()["data_domains"]), {"public", "finance"})
        self.assertEqual(operator.patch("/admin/users/operator/domains", json={"data_domains": ["operations"]}).status_code, 403)

    def test_reclassification_preserves_individual_acl_and_revocation(self):
        operator = self.reader("operator", ["operations"])
        subject = operator.get("/console/session").json()["subject_id"]
        self.document("private-finance", ["finance"], "PRIVATE117", allowed_subjects=[subject])
        self.assertEqual(self.query(operator)["evidence"], [])
        response = self.admin.post("/documents/access", json={"document_id": "private-finance", "data_domains": ["operations"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.query(operator)["evidence"]), 1)
        other = self.reader("other-ops", ["operations"])
        self.assertEqual(self.query(other)["evidence"], [])
        self.assertEqual(self.admin.post("/access", json={"document_id": "private-finance", "revoked": True}).status_code, 200)
        self.assertEqual(self.admin.post("/documents/access", json={"document_id": "private-finance", "data_domains": ["public"]}).status_code, 200)
        self.assertEqual(self.query(operator)["evidence"], [])

    def test_request_cannot_grant_itself_finance_or_use_an_unknown_domain(self):
        operator = self.reader("operator", ["operations"])
        for extra in ({"data_domains": ["finance"]}, {"groups": ["finance"]}, {"admin": True}):
            self.assertEqual(operator.post("/query", json={"question": "policy", **extra}).status_code, 422)
        response = self.admin.post("/admin/users", json={"username": "invalid", "password": "test-password", "data_domains": ["made-up"]})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.admin.patch("/admin/users/ragadmin/domains", json={"data_domains": []}).status_code, 409)
        self.assertEqual(operator.post("/documents/access", json={"document_id": "report", "data_domains": ["operations"]}).status_code, 403)

    def test_permissions_revoked_before_query_return_block_normal_and_stream_results(self):
        operator = self.reader("operator", ["operations"])
        self.document("operations-manual", ["operations"], "OPERATIONSECRET771")
        application = self.api.state.application
        original = application.query
        admin = self.api.state.accounts.session(self.admin.cookies.get("an_rag_session")).principal

        async def revoked(request, observer=None):
            result = await original(request, observer)
            self.api.state.accounts.set_domains(admin, "operator", ("finance",))
            return result

        application.query = revoked
        denied = operator.post("/query", json={"question": "company policy"})
        self.assertEqual(denied.status_code, 404, denied.text)
        self.assertNotIn("OPERATIONSECRET771", denied.text)
        self.api.state.accounts.set_domains(admin, "operator", ("operations",))
        stream = operator.post("/query/stream", json={"question": "company policy"})
        self.assertNotIn("event: result", stream.text)
        self.assertIn("event: error", stream.text)
        self.assertNotIn("OPERATIONSECRET771", stream.text)

    def test_image_domains_gate_search_bytes_trace_and_live_account_changes(self):
        operator = self.reader("operator", ["operations"])
        output = BytesIO()
        Image.new("RGB", (20, 20), "blue").save(output, "PNG")
        for asset_id, domain in (("ops-chart", "operations"), ("finance-chart", "finance"), ("old-chart", None)):
            response = self.admin.post("/assets", json={"asset_id": asset_id, "title": "company policy chart " + asset_id,
                "content_base64": base64.b64encode(output.getvalue()).decode(),
                **({"data_domains": [domain]} if domain else {})})
            self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual([a["asset_id"] for a in operator.get("/assets").json()["assets"]], ["ops-chart"])
        result = operator.post("/assets/search", json={"question": "company policy"}).json()
        self.assertEqual([hit["asset"]["asset_id"] for hit in result["hits"]], ["ops-chart"])
        self.assertEqual(operator.get("/assets/finance-chart/content").status_code, 404)
        self.assertEqual(operator.get("/assets/finance-chart/thumbnail").status_code, 404)
        self.assertEqual(operator.get("/assets/old-chart/content").status_code, 404)
        self.assertEqual(operator.get("/assets/ops-chart/content").status_code, 200)
        self.admin.patch("/admin/users/operator/domains", json={"data_domains": ["finance"]})
        self.assertEqual(operator.get("/assets/ops-chart/content").status_code, 404)
        self.assertEqual(operator.get("/assets/traces/" + result["trace_id"]).status_code, 404)
        self.assertEqual(operator.get("/assets/finance-chart/content").status_code, 200)

    def test_legacy_bearer_reader_is_public_only_and_explicit_scopes_are_respected(self):
        self.document("shared-policy", ["public"], "PUBLICDOC993")
        self.document("financial-report", ["finance"], "FINANCESECRET882")
        self.document("legacy-unclassified", [], "UNCLASSIFIED114")
        settings = replace(self.settings, api_tokens={
            "legacy-reader-test-token": Principal("company", "legacy-reader"),
            "finance-reader-test-token": Principal("company", "finance-reader", data_domains=("finance",)),
        })
        api = create_api(settings)
        legacy = self.client(api)
        legacy.headers["Authorization"] = "Bearer legacy-reader-test-token"
        self.assertEqual(legacy.get("/console/session").json()["data_domains"], ["public"])
        self.assertEqual({e["document_id"] for e in self.query(legacy)["evidence"]}, {"shared-policy"})
        finance = self.client(api)
        finance.headers["Authorization"] = "Bearer finance-reader-test-token"
        self.assertEqual({e["document_id"] for e in self.query(finance)["evidence"]},
                         {"shared-policy", "financial-report"})

    def test_image_permissions_are_rechecked_after_search_and_object_read(self):
        operator = self.reader("operator", ["operations"])
        output = BytesIO()
        Image.new("RGB", (20, 20), "blue").save(output, "PNG")
        response = self.admin.post("/assets", json={
            "asset_id": "ops-chart", "title": "company policy chart",
            "data_domains": ["operations"],
            "content_base64": base64.b64encode(output.getvalue()).decode(),
        })
        self.assertEqual(response.status_code, 201, response.text)
        assets = self.api.state.application.assets
        accounts = self.api.state.accounts
        admin = accounts.session(self.admin.cookies.get("an_rag_session")).principal
        original_search, original_content = assets.search, assets.content

        async def revoked_search(*args, **kwargs):
            result = await original_search(*args, **kwargs)
            accounts.set_domains(admin, "operator", ("finance",))
            return result

        assets.search = revoked_search
        denied = operator.post("/assets/search", json={"question": "company policy"})
        self.assertEqual(denied.status_code, 404, denied.text)
        self.assertNotIn("company policy chart", denied.text)

        def revoked_content(*args, **kwargs):
            result = original_content(*args, **kwargs)
            accounts.set_domains(admin, "operator", ("finance",))
            return result

        assets.content = revoked_content
        for path in ("content", "thumbnail"):
            accounts.set_domains(admin, "operator", ("operations",))
            denied = operator.get(f"/assets/ops-chart/{path}")
            self.assertEqual(denied.status_code, 404, denied.text)
            self.assertNotEqual(denied.content, output.getvalue())

    def test_failed_stream_hides_completed_outputs_after_account_permission_change(self):
        operator = self.reader("operator", ["operations"])
        self.document("operations-manual", ["operations"], "OPERATIONSECRET771")
        accounts = self.api.state.accounts
        admin = accounts.session(self.admin.cookies.get("an_rag_session")).principal

        class RevokingJudge:
            async def assess(self, queries, evidence):
                accounts.set_domains(admin, "operator", ("finance",))
                raise RuntimeError("synthetic-provider-failure")

        self.api.state.application.components["judge"] = RevokingJudge()
        stream = operator.post("/query/stream", json={"question": "company policy"})
        self.assertIn("event: error", stream.text)
        self.assertNotIn("event: result", stream.text)
        self.assertNotIn("OPERATIONSECRET771", stream.text)
        self.assertNotIn("synthetic-provider-failure", stream.text)
        self.assertNotIn('"trace":', stream.text)

    def test_document_listing_exposes_every_pending_version_for_the_publish_page(self):
        """The /knowledge page publishes from `staged_jobs`; it must list every unpublished version."""
        def stage(version):
            response = self.admin.post("/documents", json={
                "document_id": "handbook", "version": version, "title": "Handbook",
                "content": f"# Handbook\ncompany policy HANDBOOK-{version}", "data_domains": ["public"]})
            self.assertEqual(response.status_code, 201, response.text)
            return response.json()["job_id"]

        def listing():
            response = self.admin.get("/documents")
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(len(body["documents"]), 1)
            return body["generation"], body["documents"][0]

        first = stage("v1")
        generation, record = listing()
        self.assertFalse(record["published"])
        self.assertEqual([job["job_id"] for job in record["staged_jobs"]], [first])

        response = self.admin.post("/publish", json={"job_id": first, "expected_generation": generation})
        self.assertEqual(response.status_code, 200, response.text)
        generation, record = listing()
        self.assertTrue(record["published"])
        self.assertEqual(record["staged_jobs"], [])

        second, third = stage("v2"), stage("v3")
        generation, record = listing()
        self.assertTrue(record["published"])
        self.assertEqual(record["version"], "v1")
        self.assertEqual({job["job_id"]: job["version"] for job in record["staged_jobs"]},
                         {second: "v2", third: "v3"})

        response = self.admin.post("/publish", json={"job_id": second, "expected_generation": generation})
        self.assertEqual(response.status_code, 200, response.text)
        _, record = listing()
        self.assertEqual(record["version"], "v2")
        self.assertEqual([job["job_id"] for job in record["staged_jobs"]], [third])

    def test_staging_another_version_never_changes_the_document_classification(self):
        """Classification belongs to the document. The /knowledge page relies on `data_domains`
        in the listing being the live value so it can warn when a new version asked for another."""
        operator = self.reader("operator", ["operations"])
        finance = self.reader("finance", ["finance"])
        self.document("handbook", ["operations"], "HANDBOOK-V1")

        response = self.admin.post("/documents", json={
            "document_id": "handbook", "version": "v2", "title": "Handbook",
            "content": "# Company policy\ncompany policy HANDBOOK-V2", "data_domains": ["finance"]})
        self.assertEqual(response.status_code, 201, response.text)
        staged = response.json()["job_id"]

        listing = self.admin.get("/documents").json()
        record = listing["documents"][0]
        self.assertEqual(record["data_domains"], ["operations"])
        self.assertEqual([job["job_id"] for job in record["staged_jobs"]], [staged])

        response = self.admin.post("/publish", json={"job_id": staged, "expected_generation": listing["generation"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.admin.get("/documents").json()["documents"][0]["data_domains"], ["operations"])
        self.assertEqual(len(self.query(operator)["evidence"]), 1)
        self.assertEqual(self.query(finance)["evidence"], [])


if __name__ == "__main__":
    unittest.main()
