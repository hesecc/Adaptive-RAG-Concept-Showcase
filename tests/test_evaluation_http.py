"""The evaluation page and its report API: administrators only, one tenant's reports at a time."""

import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from adaptive_rag.api import create_api
from adaptive_rag.config import Principal, Settings
from adaptive_rag.evaluation_reports import ReportStore
from support import ADMIN_PASSWORD

ADMIN_TOKEN = "admin-test-token-0000000000000001"
READER_TOKEN = "reader-test-token-000000000000001"
OTHER_TOKEN = "other-test-token-0000000000000001"


def retrieval_report(tenant):
    return {
        "created_at": "2026-11-17T08:00:00+00:00",
        "parameters": {"tenant_id": tenant, "top_k": 5, "data_domains": None, "fixture_path": "C:\\data\\eval.jsonl"},
        "model_calls": 0, "case_count": 1, "positive_case_count": 1,
        "metrics": {"hit_at_k": 1.0, "recall_at_k": 1.0, "mrr_at_k": 1.0, "ndcg_at_k": 1.0},
        "latency_ms": {"count": 1, "p50": 4.0, "p95": 4.0, "max": 4.0},
        "cases": [{"id": "保修", "question": "保修期限是多久？", "duration_ms": 4.0}],
    }


def permission_report(tenant):
    return {
        "created_at": "2026-11-17T08:05:00+00:00", "parameters": {"tenant_id": tenant}, "model_calls": 0,
        "summary": {"cells": 1, "ok": 1, "leaks": 0, "missing": 0, "broad_checks": 1, "broad_failures": 0, "passed": True},
        "cells": [{"identity": "admin", "document_id": "x", "expected": True, "visible": True, "result": "ok"}],
    }


class EvaluationHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.settings = Settings(data_dir=self.data, browser_tenant="company", admin_password=ADMIN_PASSWORD, api_tokens={
            ADMIN_TOKEN: Principal("company", "token-admin", admin=True),
            READER_TOKEN: Principal("company", "token-reader"),
            OTHER_TOKEN: Principal("other", "other-admin", admin=True),
        })
        self.api = create_api(self.settings)
        self.store = ReportStore(self.data)
        self.retrieval = self.store.save(retrieval_report("company"), "retrieval")
        self.permissions = self.store.save(permission_report("company"), "permissions")
        self.foreign = self.store.save(retrieval_report("other"), "retrieval")

    def client(self):
        client = TestClient(self.api, raise_server_exceptions=False)
        self.addCleanup(client.close)
        return client

    def bearer(self, token=ADMIN_TOKEN):
        return {"Authorization": f"Bearer {token}"}

    def browser(self, username="ragadmin", password=ADMIN_PASSWORD):
        client = self.client()
        response = client.post("/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def browser_reader(self, admin):
        response = admin.post("/admin/users", json={"username": "reader", "password": "test-password", "data_domains": ["public"]})
        self.assertEqual(response.status_code, 201, response.text)
        return self.browser("reader", "test-password")

    def test_the_page_itself_needs_no_session_like_the_other_pages(self):
        response = self.client().get("/evaluation")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])

    def test_the_page_loads_only_assets_the_server_actually_serves(self):
        client = self.client()
        html = client.get("/evaluation").text
        scripts = re.findall(r"<script\b([^>]*)>", html)
        stylesheets = re.findall(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', html)
        self.assertTrue(scripts and stylesheets)
        # The CSP allows only same-origin scripts, so an inline script would be blocked in the browser.
        self.assertTrue(all("src=" in attributes for attributes in scripts), scripts)
        sources = [re.search(r'src="([^"]+)"', attributes).group(1) for attributes in scripts]
        self.assertIn("/static/evaluation.js", sources)
        self.assertIn("/static/evaluation.css", stylesheets)
        for path in sources + stylesheets:
            self.assertTrue(path.startswith("/static/"), path)
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertGreater(len(response.content), 0, path)

    def test_the_other_pages_link_to_the_evaluation_page(self):
        client = self.client()
        self.assertIn('href="/evaluation"', client.get("/knowledge").text)
        tab = re.search(r'<a id="evaluation-open"([^>]*)>', client.get("/").text)
        self.assertIsNotNone(tab)
        # The workbench reveals the tab only after the session proves an administrator, so the markup starts hidden.
        self.assertIn('href="/evaluation"', tab.group(1))
        self.assertRegex(tab.group(1), r"\bhidden\b")

    def test_reports_are_for_administrators_only(self):
        admin = self.browser()
        reader = self.browser_reader(admin)
        anonymous = self.client()
        for path in ("/evaluation/reports", f"/evaluation/reports/{self.retrieval['report_id']}"):
            self.assertEqual(anonymous.get(path).status_code, 401, path)
            self.assertEqual(anonymous.get(path, headers=self.bearer("incorrect")).status_code, 401, path)
            self.assertEqual(anonymous.get(path, headers=self.bearer(READER_TOKEN)).status_code, 403, path)
            denied = reader.get(path)
            self.assertEqual(denied.status_code, 403, path)
            self.assertEqual(denied.json()["error"]["code"], "forbidden")
            self.assertEqual(anonymous.get(path, headers=self.bearer()).status_code, 200, path)
            self.assertEqual(admin.get(path).status_code, 200, path)

    def test_listing_is_newest_first_and_leaves_out_the_detail(self):
        body = self.client().get("/evaluation/reports", headers=self.bearer()).json()
        listed = body["reports"]
        self.assertEqual([item["report_id"] for item in listed],
                         sorted([self.retrieval["report_id"], self.permissions["report_id"]], reverse=True))
        by_kind = {item["kind"]: item for item in listed}
        self.assertEqual(by_kind["retrieval"]["metrics"]["hit_at_k"], 1.0)
        self.assertEqual(by_kind["retrieval"]["fixture"], "eval.jsonl")
        self.assertTrue(by_kind["permissions"]["passed"])
        self.assertNotIn("cases", by_kind["retrieval"])
        self.assertNotIn("parameters", by_kind["retrieval"])

    def test_a_browser_administrator_and_an_admin_token_see_the_same_tenant(self):
        token = self.client().get("/evaluation/reports", headers=self.bearer()).json()
        cookie = self.browser().get("/evaluation/reports").json()
        self.assertEqual(token, cookie)
        self.assertEqual(len(cookie["reports"]), 2)

    def test_another_tenant_sees_only_its_own_reports(self):
        client = self.client()
        listed = client.get("/evaluation/reports", headers=self.bearer(OTHER_TOKEN)).json()["reports"]
        self.assertEqual([item["report_id"] for item in listed], [self.foreign["report_id"]])
        for report in (self.retrieval, self.permissions):
            response = client.get(f"/evaluation/reports/{report['report_id']}", headers=self.bearer(OTHER_TOKEN))
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "not_found")
        # The other tenant's report is indistinguishable from one that does not exist.
        theirs = client.get(f"/evaluation/reports/{self.foreign['report_id']}", headers=self.bearer())
        unknown = client.get("/evaluation/reports/20260101T000000Z-retrieval-aaaaaaaa", headers=self.bearer())
        self.assertEqual((theirs.status_code, theirs.json()), (unknown.status_code, unknown.json()))
        self.assertEqual(theirs.status_code, 404)

    def test_detail_returns_the_stored_report_in_full(self):
        client = self.client()
        report = client.get(f"/evaluation/reports/{self.retrieval['report_id']}", headers=self.bearer()).json()
        self.assertEqual(report, self.retrieval)
        self.assertEqual(report["cases"][0]["question"], "保修期限是多久？")
        permissions = client.get(f"/evaluation/reports/{self.permissions['report_id']}", headers=self.bearer()).json()
        self.assertEqual(permissions["cells"], self.permissions["cells"])

    def test_malformed_or_unknown_ids_are_plain_not_found(self):
        (self.data / "secret.json").write_text(json.dumps(retrieval_report("company")), encoding="utf-8")
        client = self.client()
        for bad in ("secret", "..%2Fsecret", "..%5Csecret", "%2e%2e%2fsecret", "not-an-id",
                    self.retrieval["report_id"].upper(), self.retrieval["report_id"] + ".json",
                    "20260101T000000Z-retrieval-aaaaaaaa"):
            response = client.get(f"/evaluation/reports/{bad}", headers=self.bearer())
            self.assertEqual(response.status_code, 404, bad)
            # An encoded slash never reaches the handler, so the code may be the router's own.
            self.assertIn(response.json()["error"]["code"], ("not_found", "http_error"), bad)
            self.assertNotIn("Traceback", response.text)
        for reached in ("secret", "not-an-id", "20260101T000000Z-retrieval-aaaaaaaa"):
            response = client.get(f"/evaluation/reports/{reached}", headers=self.bearer())
            self.assertEqual(response.json()["error"]["code"], "not_found", reached)

    def test_damaged_files_never_break_the_listing(self):
        folder = self.data / "evaluations"
        (folder / "20990101T000000Z-retrieval-dddddddd.json").write_text("{not json", encoding="utf-8")
        (folder / "20990102T000000Z-permissions-eeeeeeee.json").write_bytes(b"\xff\xfe\x00")
        client = self.client()
        response = client.get("/evaluation/reports", headers=self.bearer())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["reports"]), 2)
        self.assertEqual(client.get("/evaluation/reports/20990101T000000Z-retrieval-dddddddd", headers=self.bearer()).status_code, 404)

    def test_a_tenant_with_no_reports_gets_an_empty_list(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as empty:
            api = create_api(Settings(data_dir=Path(empty), browser_tenant="company", admin_password=ADMIN_PASSWORD))
            with TestClient(api) as client:
                self.assertEqual(client.post("/auth/login", json={"username": "ragadmin", "password": ADMIN_PASSWORD}).status_code, 200)
                self.assertEqual(client.get("/evaluation/reports").json(), {"reports": []})


if __name__ == "__main__":
    unittest.main()
