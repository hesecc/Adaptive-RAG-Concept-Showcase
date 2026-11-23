"""PDF sources flow through the public ingest/publish/query interface."""

from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from adaptive_rag.app import RagApplication
from adaptive_rag.cli import main
from adaptive_rag.config import Settings
from adaptive_rag.domain import AccessContext, QueryRequest, ValidationError
from adaptive_rag.pdf import ParsedPdf
from test_pdf import pdf_bytes


class PdfIngestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "fixture.pdf"
        self.raw = pdf_bytes(("Amber warranty lasts twelve months.",))
        self.path.write_bytes(self.raw)
        self.app = RagApplication(Settings(data_dir=self.root / "data"))

    async def test_original_pdf_is_archived_and_markdown_is_queryable_after_restart(self):
        with patch.object(self.app, "ingest", wraps=self.app.ingest) as ingest:
            staged = self.app.ingest_pdf(self.path, tenant_id="tenant", document_id="pdf", version="v1", title="PDF policy")
        request = ingest.call_args.args[0]
        self.assertEqual(request.source_object_key, sha256(self.raw).hexdigest())
        self.assertEqual(request.source_parser, "local")
        self.assertEqual(self.app.objects.get(request.source_object_key), self.raw)
        self.assertIn("Amber warranty", request.content)
        self.app.publish("tenant", staged.job_id, None)
        restarted = RagApplication(self.app.settings)
        result = await restarted.query(QueryRequest(AccessContext("tenant", "reader"), "Amber warranty"))
        self.assertTrue(result.evidence)
        self.assertIn("twelve months", result.evidence[0].text)
        self.assertEqual(restarted.objects.get(request.source_object_key), self.raw)

    async def test_changed_source_between_parse_and_archive_is_rejected(self):
        def changed_source(*args, **kwargs):
            self.path.write_bytes(pdf_bytes(("Different source after parsing.",)))
            return ParsedPdf("Original warranty.", 1, "local", .01, {"source_sha256": sha256(self.raw).hexdigest()})
        with patch("adaptive_rag.pdf.parse_pdf", side_effect=changed_source), patch.object(self.app, "ingest", wraps=self.app.ingest) as ingest:
            with self.assertRaises(ValidationError):
                self.app.ingest_pdf(self.path, tenant_id="tenant", document_id="pdf", version="v1")
        ingest.assert_not_called()
        self.assertIsNone(self.app.store.open_snapshot("tenant"))

    async def test_cli_parse_writes_markdown_and_safe_summary(self):
        output = self.root / "parsed" / "output.md"
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(stdout), redirect_stderr(stderr), patch("socket.socket.connect", side_effect=AssertionError("unexpected network")):
            code = main(["parse-pdf", "--file", str(self.path), "--backend", "local", "--output", str(output)])
        self.assertEqual(code, 0, stderr.getvalue())
        result = json.loads(stdout.getvalue())
        markdown = output.read_text(encoding="utf-8")
        self.assertEqual(result["characters"], len(markdown))
        self.assertEqual(result["pages"], 1)
        self.assertIn("Amber warranty", markdown)
        self.assertNotIn("twelve months", stdout.getvalue())

    async def test_cli_rejects_output_over_source_or_checkpoint_before_parsing(self):
        checkpoint = self.root / "checkpoint.json"
        checkpoint.write_text("synthetic recovery state", encoding="utf-8")
        for output, extra in ((self.path, []), (checkpoint, ["--checkpoint", str(checkpoint)])):
            with self.subTest(output=output.name):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.dict(os.environ, {}, clear=True), redirect_stdout(stdout), redirect_stderr(stderr), patch("adaptive_rag.pdf.parse_pdf") as parse:
                    code = main(["parse-pdf", "--file", str(self.path), "--backend", "mineru",
                                 "--output", str(output), *extra])
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "validation_error")
                parse.assert_not_called()
                self.assertEqual(self.path.read_bytes(), self.raw)
                self.assertEqual(checkpoint.read_text(encoding="utf-8"), "synthetic recovery state")


if __name__ == "__main__":
    unittest.main()
