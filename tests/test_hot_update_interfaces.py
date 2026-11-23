"""Operator commands run against a disposable store; no ambient deployment config or paid calls."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from adaptive_rag.api import create_api
from adaptive_rag.cli import dispatch, parser
from adaptive_rag.config import Principal, Settings
from adaptive_rag.domain import AccessContext, IngestRequest, QueryRequest, ValidationError
from support import ADMIN_PASSWORD


class HotUpdateInterfaces(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def cli(self, command, *args, code=0, extra_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("RAG_")}
        env.update(PYTHONIOENCODING="utf-8", **(extra_env or {}))
        result = subprocess.run([sys.executable, "-m", "adaptive_rag", "--data-dir", str(self.root),
                                 command, "--tenant", "demo", *args], cwd=Path(__file__).resolve().parents[1],
                                env=env, capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        return json.loads(result.stderr if code else result.stdout)

    def test_cli_batch_prewarm_preview_activate_and_rollback(self):
        def ingest(name, version, text):
            path = self.root / (name + ".md")
            path.write_text(text, encoding="utf-8")
            return self.cli("ingest", "--document", name, "--version", version, "--file", str(path))["job_id"]

        original = ingest("policy", "v1", "# Policy\nOld warranty lasts one month.")
        base = self.cli("publish", "--job", original)["generation"]
        first = ingest("policy", "v2", "# Policy\nCobalt warranty lasts two months.")
        second = ingest("returns", "v1", "# Returns\nReturns need a receipt.")
        expected = ["--expected-generation", base]
        gen = self.cli("candidate-create", "--job", first, "--job", second, *expected)["generation"]
        candidate = ["--generation", gen]
        self.assertTrue(self.cli("candidate-status", *candidate)["unactivated"])
        self.assertEqual(self.cli("candidate-prewarm", *candidate)["status"], "dense_disabled")
        self.assertEqual(self.cli("candidate-prewarm", *candidate, "--execute", code=2)["error"]["code"],
                         "capability_unavailable")
        self.assertEqual(self.cli("prewarm", *candidate, code=2)["error"]["code"], "not_found")
        preview = self.cli("candidate-preview", *candidate, "--question", "Cobalt warranty")
        self.assertIn("two months", preview["evidence"][0]["text"])
        self.assertEqual(preview["capabilities"]["usage"]["model_calls"], 0)
        self.assertEqual(self.cli("query", "--question", "warranty")["generation"], base)
        self.assertEqual(self.cli("rollback", *candidate, *expected, code=2)["error"]["code"], "not_found")
        self.assertEqual(self.cli("candidate-activate", *candidate, *expected)["generation"], gen)
        self.assertEqual(self.cli("query", "--question", "returns")["generation"], gen)
        self.assertFalse(self.cli("candidate-status", *candidate)["unactivated"])
        self.assertEqual(self.cli("prewarm", *candidate, "--dry-run")["embedded"], 0)
        reverted = self.cli("rollback", "--generation", base, "--expected-generation", gen)
        self.assertEqual(reverted["revisions"], [["policy", self.cli("job", "--id", original)["revision_id"]]])

    def test_public_routes_cannot_open_candidate_or_preview_trace(self):
        admin = "hot-update-admin-token-0000001"
        reader = "hot-update-reader-token-000001"
        settings = Settings(data_dir=self.root, admin_password=ADMIN_PASSWORD, api_tokens={
            admin: Principal("demo", "operator", admin=True), reader: Principal("demo", "reader", data_domains=("operations",))})
        api = create_api(settings)
        app = api.state.application
        from test_figures import archive, ingest_archive, picture
        from test_pdf import pdf_bytes
        path = self.root / "survey.pdf"
        path.write_bytes(pdf_bytes(("Cobalt warranty.",)))
        png = picture()
        parsed = archive("Cobalt warranty lasts two months.\n\n![](images/survey.png)", {"images/survey.png": png})
        job = ingest_archive(app, path, parsed, tenant_id="demo", document_id="policy", version="v1",
                             allowed_subjects=("reader",), data_domains=("operations",))
        candidate = app.store.create_candidate_snapshot("demo", (job.job_id,), None).generation
        request = QueryRequest(AccessContext("demo", "reader"), "Cobalt warranty", generation=candidate)
        preview = asyncio.run(app.preview_candidate(request))
        denied = asyncio.run(app.preview_candidate(QueryRequest(AccessContext("demo", "outsider"),
                                                                "Cobalt warranty", generation=candidate)))
        self.assertFalse(denied.evidence)
        evidence = f"/evidence/{candidate}/{preview.evidence[0].evidence_id}"
        figure = f"{evidence}/images/{preview.evidence[0].images[0].key}"
        trace = f"/traces/{preview.trace_id}"
        body = {"question": "Cobalt warranty", "generation": candidate}
        with TestClient(api) as client:
            for token in (reader, admin):
                headers = {"Authorization": f"Bearer {token}"}
                self.assertEqual(client.post("/query", headers=headers, json=body).status_code, 404)
                stream = client.post("/query/stream", headers=headers, json=body)
                self.assertIn("event: error", stream.text)
                self.assertNotIn("two months", stream.text)
                self.assertEqual(client.get(trace, headers=headers).status_code, 404)
                self.assertEqual(client.get(evidence, headers=headers).status_code, 404)
                with patch.object(app.objects, "get", side_effect=AssertionError("candidate blob read")):
                    self.assertEqual(client.get(figure, headers=headers).status_code, 404)
                self.assertEqual(client.post("/query", headers=headers, json=body | {"allow_candidate": True}).status_code, 422)
            self.assertEqual(client.post("/rollback", headers=headers, json={"generation": candidate}).status_code, 404)
            self.assertIsNone(app.store.open_snapshot("demo"))
            app.store.activate_candidate_snapshot("demo", candidate, None)
            self.assertEqual(client.get(trace, headers=headers).status_code, 404)
            self.assertEqual(client.get(evidence, headers={"Authorization": f"Bearer {reader}"}).status_code, 200)
            self.assertEqual(client.get(figure, headers={"Authorization": f"Bearer {reader}"}).content, png)

    def test_trusted_full_scope_dry_run_and_explicit_filter_conflicts(self):
        from adaptive_rag.app import RagApplication
        app = RagApplication(Settings(data_dir=self.root))
        job = app.ingest(IngestRequest("demo", "restricted", "v1", "A private warranty.", scope="sku",
                                       sku_ids=("private",), data_domains=("finance",), allowed_subjects=("finance",)))
        gen = app.store.create_candidate_snapshot("demo", (job.job_id,), None).generation
        with patch.dict(os.environ, {}, clear=True):
            for command in ("candidate-prewarm", "prewarm"):
                if command == "prewarm":
                    app.store.activate_candidate_snapshot("demo", gen, None)
                argv = ["--data-dir", str(self.root), command, "--tenant", "demo", "--generation", gen]
                self.assertEqual(dispatch(parser().parse_args(argv))["authorized_chunks"], 0)
                report = dispatch(parser().parse_args(argv + ["--all-scopes"]))
                self.assertEqual(report["scope"], "all_scopes")
                self.assertGreater(report["authorized_chunks"], 0)
                for option in (("--subject", "local"), ("--group", "sales"), ("--data-domain", "finance"), ("--sku", "private")):
                    with self.subTest(command=command, option=option), self.assertRaises(ValidationError):
                        dispatch(parser().parse_args(argv + ["--all-scopes", *option]))

    def test_configured_dense_cli_plans_without_provider_calls_and_execution_requires_opt_in(self):
        from adaptive_rag.app import RagApplication
        app = RagApplication(Settings(data_dir=self.root))
        job = app.ingest(IngestRequest("demo", "doc", "v1", "The cobalt warranty lasts two months."))
        gen = app.store.create_candidate_snapshot("demo", (job.job_id,), None).generation
        env = {"RAG_ENABLE_CLOUD": "true", "RAG_CLOUD_BASE_URL": "http://127.0.0.1:1/v1",
               "RAG_CLOUD_API_KEY": "test-only", "RAG_EMBEDDING_MODEL": "embed", "RAG_LLM_MODEL": "chat",
               "RAG_RERANK_MODEL": "rerank"}
        args = ["--data-dir", str(self.root), "candidate-prewarm", "--tenant", "demo", "--generation", gen, "--all-scopes"]
        with patch.dict(os.environ, env, clear=True), patch("adaptive_rag.cloud.CloudTransport.post",
                side_effect=AssertionError("unexpected transport call")) as post:
            plan = dispatch(parser().parse_args(args))
            self.assertEqual((plan["dry_run"], plan["missing"], plan["embedded"]), (True, 1, 0))
            with patch("adaptive_rag.cloud.CloudEmbedder.embed", return_value=((1.0, 0.0),)) as embed:
                done = dispatch(parser().parse_args(args + ["--execute"]))
                embed.assert_awaited_once()
            self.assertEqual((done["dry_run"], done["remaining"]), (False, 0))
            self.assertEqual(dispatch(parser().parse_args(args))["missing"], 0)
            preview = dispatch(parser().parse_args(["--data-dir", str(self.root), "candidate-preview", "--tenant", "demo",
                                                    "--generation", gen, "--question", "cobalt warranty"]))
            self.assertTrue(preview.evidence)
            self.assertEqual(preview.capabilities["usage"]["model_calls"], 0)
            post.assert_not_awaited()

    def test_stale_candidate_abandon_and_rebuild(self):
        from adaptive_rag.app import RagApplication
        from adaptive_rag.domain import ConflictError
        app = RagApplication(Settings(data_dir=self.root))
        job = app.ingest(IngestRequest("demo", "first", "v1", "First staged document."))
        next_job = app.ingest(IngestRequest("demo", "second", "v1", "Second document for independent release."))

        def command(name, *flags):
            return dispatch(parser().parse_args(["--data-dir", str(self.root), name, "--tenant", "demo", *flags]))

        with patch.dict(os.environ, {}, clear=True):
            old = command("candidate-create", "--job", job.job_id).generation
            live = app.publish("demo", next_job.job_id, None).generation
            with self.assertRaises(ConflictError):
                command("candidate-activate", "--generation", old)
            self.assertEqual(command("candidate-status", "--generation", old)["live_generation"], live)
            self.assertTrue(command("candidate-abandon", "--generation", old)["abandoned"])
            self.assertFalse(command("candidate-abandon", "--generation", old)["abandoned"])
            fresh = command("candidate-create", "--job", job.job_id, "--expected-generation", live).generation
            merged = command("candidate-activate", "--generation", fresh, "--expected-generation", live)
            self.assertEqual(set(dict(merged.revisions)), {"first", "second"})
