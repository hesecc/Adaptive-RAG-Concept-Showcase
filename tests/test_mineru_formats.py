"""Hosted v4 contract for original Office files and the PDF compatibility path."""

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from struct import pack_into
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import httpx

from adaptive_rag.app import RagApplication
from adaptive_rag.config import Principal, Settings
from adaptive_rag.domain import CapabilityError, ValidationError
from adaptive_rag.mineru_formats import ORIGINAL_MIME, validate_office
from adaptive_rag.pdf import PdfSettings
from test_pdf import archive_bytes, pdf_bytes


def package(ext, *, prefix="", extra_override=""):
    part = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}[ext]
    manifest = (prefix + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                f'<Override PartName="/{part}" ContentType="{ORIGINAL_MIME[ext]}.main+xml"/>{extra_override}</Types>')
    return archive_bytes([("[Content_Types].xml", manifest), (part, "<fixture/>")])


def compound(stream_name):
    # Small CFB directory fixture. Format identification doesn't decode Office stream contents.
    raw = bytearray(1536)
    raw[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    pack_into("<HHHH", raw, 26, 3, 0xfffe, 9, 6)
    pack_into("<II", raw, 44, 1, 0)  # FAT count and directory sector
    pack_into("<I", raw, 76, 1)  # FAT is sector 1
    pack_into("<II", raw, 1024, 0xfffffffe, 0xfffffffd)
    for start, name, kind, child in ((512, "Root Entry", 5, 1), (640, stream_name, 2, 0xffffffff)):
        encoded = (name + "\x00").encode("utf-16le")
        raw[start:start + len(encoded)] = encoded
        pack_into("<HBBIII", raw, start + 64, len(encoded), kind, 1, 0xffffffff, 0xffffffff, child)
    return bytes(raw)


class MinerUFormatTests(unittest.TestCase):
    def test_workbook_default_content_type_and_override_precedence(self):
        mime = ORIGINAL_MIME['xlsx'] + '.main+xml'
        def workbook(default, override=None):
            entries = f'<Default Extension="xml" ContentType="{default}"/>'
            if override:
                entries += f'<Override PartName="/xl/workbook.xml" ContentType="{override}"/>'
            manifest = f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">{entries}</Types>'
            return archive_bytes([('[Content_Types].xml', manifest), ('xl/workbook.xml', '<workbook/>')])
        validate_office(workbook(mime), 'xlsx')
        validate_office(workbook('application/xml', mime), 'xlsx')
        for raw in (workbook('application/xml'), workbook(mime, 'application/xml')):
            with self.assertRaises(ValidationError):
                validate_office(raw, 'xlsx')

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.app = RagApplication(Settings(data_dir=self.root / "data"))
        self.settings = PdfSettings(api_key="test-key")
        self.principal = Principal("tenant", "uploader")

    def ingest(self, path, **kwargs):
        return self.app.ingest_mineru(path, pdf_settings=kwargs.pop("settings", self.settings),
            tenant_id="tenant", document_id="source-" + path.suffix[1:].lower(), version="v1",
            principal=self.principal, **kwargs)

    def transport(self, raw, ext, *, model="vlm", archive=None):
        calls = []
        digest = sha256(raw).hexdigest()
        def handle(req):
            calls.append(req.method)
            if req.method == "POST":
                body = json.loads(req.content)
                self.assertEqual(req.url.path, "/api/v4/file-urls/batch")
                self.assertEqual(body["model_version"], model)
                self.assertEqual(body["files"], [{"name": "upload." + ext, "data_id": digest}])
                return httpx.Response(200, json={"code": 0, "data": {"batch_id": "id", "file_urls": [
                    "https://storage.mineru.net/input"]}})
            if req.method == "PUT":
                self.assertEqual(req.content, raw)
                self.assertNotIn("authorization", req.headers)
                return httpx.Response(200)
            if req.url.path == "/api/v4/extract-results/batch/id":
                return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{"state": "done",
                    "file_name": "upload." + ext, "data_id": digest, "full_zip_url": "https://storage.mineru.net/out"}]}})
            self.assertEqual(req.url.path, "/out")
            return httpx.Response(200, content=archive_bytes() if archive is None else archive)
        client = httpx.Client(transport=httpx.MockTransport(handle))
        return calls, patch("adaptive_rag.pdf.httpx.Client", return_value=client)

    def test_all_supported_types_stage_markdown_and_exact_original(self):
        originals = {"pdf": pdf_bytes(), "doc": compound("WordDocument"), "xls": compound("Workbook"),
                     "ppt": compound("PowerPoint Document"), **{ext: package(ext) for ext in ("docx", "xlsx", "pptx")}}
        self.assertEqual(set(originals), set(ORIGINAL_MIME))
        for ext, raw in originals.items():
            with self.subTest(ext=ext):
                path = self.root / ("confidential board papers #1." + ext.upper())
                path.write_bytes(raw)
                model = "pipeline" if ext == "ppt" else "vlm"
                calls, transport = self.transport(raw, ext, model=model)
                def authorize():
                    self.assertEqual(calls, ["POST", "PUT", "GET", "GET"])
                    path.write_bytes(b"concurrent replacement")
                with transport, patch.object(self.app.store, "stage", wraps=self.app.store.stage) as stage:
                    result = self.ingest(path, settings=replace(self.settings, model_version=model), before_stage=authorize)
                self.assertEqual(stage.call_args.kwargs["principal"], self.principal)
                with self.app.store._connection() as db:
                    row = db.execute("SELECT raw_key,metadata_json FROM revisions WHERE revision_id=?",
                                     (result.revision_id,)).fetchone()
                meta = json.loads(row["metadata_json"])
                self.assertEqual(meta["source_parser"], "mineru" if ext == "pdf" else f"mineru:{ext}")
                self.assertEqual(meta["source_object_key"], sha256(raw).hexdigest())
                self.assertEqual(self.app.objects.get(meta["source_object_key"]), raw)
                self.assertIn(b"# Heading", self.app.objects.get(row["raw_key"]))

    def test_invalid_source_never_contacts_provider(self):
        samples = [("rtf", compound("WordDocument")), ("doc", compound("Workbook")),
                   ("ppt", bytes.fromhex("d0cf11e0a1b11ae1")), ("docx", package("xlsx")),
                   ("xls", package("xlsx")), ("xlsx", b"PK\x03\x04invalid"), ("pdf", package("docx")),
                   ("xlsx", package("xlsx", prefix="<!DOCTYPE Types>")),
                   ("xlsx", package("xlsx", extra_override=f'<Override PartName="/word/document.xml" '
                                                          f'ContentType="{ORIGINAL_MIME["docx"]}.main+xml"/>')),
                   ("docx", archive_bytes([("word/document.xml", "<fixture/>")])), ("xls", b"")]
        with patch("adaptive_rag.pdf.httpx.Client") as client:
            for ext, raw in samples:
                with self.subTest(ext=ext):
                    path = self.root / ("fixture." + ext)
                    path.write_bytes(raw)
                    with self.assertRaises(ValidationError):
                        self.ingest(path)
            path.write_bytes(compound("Book"))
            with self.assertRaisesRegex(ValidationError, "size limit"):
                self.ingest(path, settings=replace(self.settings, max_input_bytes=10))
            with self.assertRaisesRegex(CapabilityError, "key"):
                self.ingest(path, settings=PdfSettings())
            client.assert_not_called()

    def test_reauthorization_and_provider_errors_never_write(self):
        path = self.root / "payroll.xls"
        raw = compound("Workbook")
        path.write_bytes(raw)
        with patch.object(self.app.objects, "put") as put, patch.object(self.app.store, "stage") as stage:
            for archive, error in ((None, PermissionError), (b"broken ZIP", CapabilityError)):
                calls, transport = self.transport(raw, "xls", archive=archive)
                gate = Mock(side_effect=PermissionError("revoked"))
                with transport, self.assertRaises(error):
                    self.ingest(path, before_stage=gate)
                self.assertEqual(len(calls), 4)
                self.assertEqual(gate.call_count, int(archive is None))
                put.assert_not_called()
                stage.assert_not_called()
