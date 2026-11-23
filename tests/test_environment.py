"""Explicit credential configuration, with only synthetic test values."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from adaptive_rag.cli import main
from adaptive_rag.config import Settings
from adaptive_rag.domain import ValidationError
from adaptive_rag.environment import apply_environment, pdf_settings, read_environment
from adaptive_rag.server import create
from support import ADMIN_PASSWORD


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "fixture.env"
        self.process = patch.dict(os.environ, {}, clear=True)
        self.process.start()
        self.addCleanup(self.process.stop)

    def test_explicit_file_preserves_literal_values_and_process_overrides(self):
        self.path.write_text("""﻿# comment
RAG_CLOUD_API_KEY="synthetic-private-key"
LITERAL='$(never-execute) ${NO_EXPANSION} # literal'
EMPTY=
""", encoding="utf-8")
        with patch.dict(os.environ, {"RAG_CLOUD_API_KEY": "injected-test-key", "EXTRA": "yes"}):
            values = read_environment(self.path)
        self.assertEqual(values["RAG_CLOUD_API_KEY"], "injected-test-key")
        self.assertEqual(values["LITERAL"], "$(never-execute) ${NO_EXPANSION} # literal")
        self.assertEqual(values["EMPTY"], "")
        self.assertEqual(values["EXTRA"], "yes")

    def test_absent_path_does_not_discover_dotenv(self):
        with patch.object(Path, "read_text", side_effect=AssertionError("implicit file read")):
            self.assertEqual(read_environment(), {})

    def test_malformed_files_report_only_line_numbers(self):
        for content in ("SYNTHETIC_PRIVATE_KEY", "lower=synthetic-private-key",
                        "KEY=first\nKEY=synthetic-private-key",
                        'KEY="synthetic-private-key', "KEY=synthetic\x00private-key"):
            self.path.write_text(content, encoding="utf-8")
            with self.subTest(content=repr(content)), self.assertRaises(ValidationError) as caught:
                read_environment(self.path)
            self.assertIn("line", str(caught.exception))
            self.assertNotIn("synthetic", str(caught.exception).lower())
        with self.assertRaises(ValidationError) as caught:
            read_environment(self.root / "private-key-does-not-exist.env")
        self.assertNotIn("private-key", str(caught.exception))

    def test_cloud_requires_explicit_true_and_complete_configuration(self):
        for values in ({}, {"RAG_CLOUD_API_KEY": "synthetic-private-key"},
                       {"RAG_ENABLE_CLOUD": "false", "RAG_CLOUD_BASE_URL": "invalid"}):
            config = apply_environment(Settings(data_dir=self.root), values)
            self.assertFalse(config.allow_models)
            self.assertFalse(config.allow_network)
            self.assertIsNone(config.cloud)
            self.assertEqual(config.plugins["dense"], "disabled")
        for values in ({"RAG_ENABLE_CLOUD": "yes"}, {"RAG_ENABLE_CLOUD": "true", "RAG_CLOUD_API_KEY": "synthetic-private-key"}):
            with self.assertRaises(ValidationError) as caught:
                apply_environment(Settings(data_dir=self.root), values)
            self.assertNotIn("synthetic-private-key", str(caught.exception))

    def test_opt_in_only_replaces_three_model_selections(self):
        base = Settings(data_dir=self.root, plugins={"rewrite": "disabled"})
        values = {"RAG_ENABLE_CLOUD": "true", "RAG_CLOUD_BASE_URL": "https://models.example.test/v1",
                  "RAG_CLOUD_API_KEY": "synthetic-private-key", "RAG_LLM_MODEL": "llm",
                  "RAG_EMBEDDING_MODEL": "embed", "RAG_RERANK_MODEL": "rank"}
        config = apply_environment(base, values)
        self.assertTrue(config.allow_models and config.allow_network)
        self.assertEqual({k for k in base.plugins if config.plugins[k] != base.plugins[k]}, {"dense", "reranker", "generator"})
        self.assertNotIn("synthetic-private-key", repr(config))
        self.assertEqual(base.plugins["dense"], "disabled")

    def test_pdf_settings_keep_cloud_and_parser_credentials_separate(self):
        config = pdf_settings({"MINERU_API_KEY": "synthetic-mineru-key", "RAG_CLOUD_API_KEY": "synthetic-cloud-key"})
        self.assertEqual(config.api_key, "synthetic-mineru-key")
        self.assertNotIn("synthetic-mineru-key", repr(config))

    def test_quality_and_query_modules_require_explicit_cloud_flags(self):
        values = {"RAG_ENABLE_CLOUD": "true", "RAG_CLOUD_BASE_URL": "https://models.example.test/v1",
                  "RAG_CLOUD_API_KEY": "synthetic-private-key", "RAG_LLM_MODEL": "llm",
                  "RAG_EMBEDDING_MODEL": "embed", "RAG_RERANK_MODEL": "rank"}
        for name in ("RAG_ENABLE_QUERY_PREPARATION", "RAG_ENABLE_QUALITY_CHECKS",
                     "RAG_ENABLE_MULTI_QUERY", "RAG_ENABLE_HYDE"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                apply_environment(Settings(data_dir=self.root), {name: "true"})
        enabled = apply_environment(Settings(data_dir=self.root), values | {
            "RAG_ENABLE_QUERY_PREPARATION": "true", "RAG_ENABLE_QUALITY_CHECKS": "true",
            "RAG_ENABLE_MULTI_QUERY": "true", "RAG_ENABLE_HYDE": "true"})
        from adaptive_rag.app import RagApplication
        app = RagApplication(enabled)
        for role in ("planner", "rewrite", "multi_query", "hyde", "judge", "gap", "verifier"):
            self.assertEqual(enabled.plugins[role], "cloud")
        self.assertEqual(enabled.plugins["decision"], "rules")
        self.assertTrue(app.capabilities()["semantic_support_verification"])
        self.assertTrue(app.capabilities()["history_resolution"])

    def test_admin_password_comes_from_the_environment_and_is_never_echoed(self):
        config = apply_environment(Settings(data_dir=self.root), {"RAG_ADMIN_PASSWORD": "synthetic-admin-password"})
        self.assertEqual(config.admin_password, "synthetic-admin-password")
        self.assertNotIn("synthetic-admin-password", repr(config))
        # An empty value counts as unset, like the other optional variables.
        for values in ({}, {"RAG_ADMIN_PASSWORD": ""}):
            self.assertIsNone(apply_environment(Settings(data_dir=self.root), values).admin_password)

    def test_new_database_needs_the_admin_password_before_the_server_starts(self):
        with patch.dict(os.environ, {"RAG_DATA_DIR": str(self.root / "fresh")}):
            with self.assertRaises(ValidationError) as caught:
                create()
        self.assertIn("RAG_ADMIN_PASSWORD", str(caught.exception))

    def test_server_supports_login_without_token_and_accepts_injected_identity(self):
        with patch.dict(os.environ, {"RAG_DATA_DIR": str(self.root / "login-data"), "RAG_ADMIN_PASSWORD": ADMIN_PASSWORD}):
            with TestClient(create()) as client:
                response = client.post("/auth/login", json={"username": "ragadmin", "password": ADMIN_PASSWORD})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["tenant_id"], "demo")
        with patch.dict(os.environ, {"RAG_DATA_DIR": str(self.root / "data"), "RAG_ADMIN_PASSWORD": ADMIN_PASSWORD}):
            with patch.dict(os.environ, {"RAG_HTTP_API_TOKEN": "synthetic-api-token-0000000001",
                                         "RAG_HTTP_TENANT": "tenant", "RAG_HTTP_SUBJECT": "operator"}):
                api = create()
        with TestClient(api) as client:
            self.assertEqual(client.get("/health").json(), {"status": "ok", "mode": "model_free"})
            self.assertEqual(client.get("/capabilities").status_code, 401)
            headers = {"Authorization": "Bearer synthetic-api-token-0000000001"}
            response = client.post("/documents", headers=headers, json={"document_id": "doc", "version": "v1", "content": "Warranty twelve months."})
            self.assertEqual(response.status_code, 201, response.text)
            job = response.json()
            stored = api.state.application.get_job("tenant", job["job_id"])
            self.assertEqual(stored["tenant_id"], "tenant")

    def test_cli_bad_environment_never_prints_value(self):
        self.path.write_text("RAG_CLOUD_API_KEY=synthetic-private-key\nRAG_CLOUD_API_KEY=duplicated-private-key\n", encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--env-file", str(self.path), "--data-dir", str(self.root / "data"), "capabilities"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "validation_error")
        self.assertNotIn("private-key", stderr.getvalue())

    def test_cloud_server_health_requires_no_paid_call(self):
        injected = {"RAG_DATA_DIR": str(self.root / "cloud-data"), "RAG_ADMIN_PASSWORD": ADMIN_PASSWORD,
                    "RAG_HTTP_API_TOKEN": "synthetic-api-token-0000000001",
                    "RAG_ENABLE_CLOUD": "true",
                    "RAG_CLOUD_BASE_URL": "https://models.example.test/v1",
                    "RAG_CLOUD_API_KEY": "synthetic-private-key",
                    "RAG_LLM_MODEL": "llm", "RAG_EMBEDDING_MODEL": "embed",
                    "RAG_RERANK_MODEL": "rank"}
        with patch.dict(os.environ, injected), patch("adaptive_rag.cloud.CloudTransport.post", side_effect=AssertionError("health must not call cloud")):
            with TestClient(create()) as client:
                self.assertEqual(client.get("/health").json(), {"status": "ok", "mode": "cloud"})
                response = client.get("/capabilities", headers={"Authorization": "Bearer synthetic-api-token-0000000001"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn("synthetic-private-key", response.text)
                self.assertIsNone(response.json()["model_calls"])


if __name__ == "__main__":
    unittest.main()
