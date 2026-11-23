from dataclasses import replace
import errno
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

import httpx

from adaptive_rag.domain import CapabilityError, ValidationError
from adaptive_rag.pdf import LocalPdfParser, MinerUPdfParser, PdfSettings, parse_pdf


def pdf_bytes(texts=("First page contains amber warranty.", "Second page contains cobalt return.")):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        if text:
            stream = DecodedStreamObject()
            stream.set_data((f"BT /F1 12 Tf 50 740 Td ({text}) Tj ET").encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def archive_bytes(entries=None):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, value in (entries or [("full.md", "# Heading\n\n$$x^2$$\n<table><tr><td>A</td></tr></table>")]):
            archive.writestr(name, value)
    return buffer.getvalue()


class PdfTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "fixture.pdf"
        self.path.write_bytes(pdf_bytes())
        self.settings = PdfSettings(api_key="private-test-key-never-output", poll_interval_seconds=.001)

    def client(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def handler(self, *, archive=None, states=("running", "done"), capture=None):
        calls = [] if capture is None else capture
        pending = iter(states)
        digest = __import__("hashlib").sha256(self.path.read_bytes()).hexdigest()
        def handle(request):
            calls.append(request)
            if request.url.path.endswith("/file-urls/batch"):
                self.assertEqual(request.headers["authorization"], "Bearer " + self.settings.api_key)
                body = json.loads(request.content)
                self.assertEqual(body["model_version"], "vlm")
                self.assertTrue(body["enable_formula"])
                self.assertTrue(body["enable_table"])
                self.assertEqual(body["files"][0]["data_id"], digest)
                return httpx.Response(200, json={"code": 0, "data": {"batch_id": "batch-1",
                    "file_urls": ["https://mineru.oss-cn-shanghai.aliyuncs.com/upload?signature=secret"]}})
            if request.method == "PUT":
                self.assertNotIn("authorization", request.headers)
                self.assertNotIn("content-type", request.headers)
                self.assertEqual(request.content, self.path.read_bytes())
                return httpx.Response(200)
            if "extract-results" in request.url.path:
                self.assertIn("authorization", request.headers)
                state = next(pending)
                item = {"state": state, "file_name": self.path.name, "data_id": digest}
                if state == "done":
                    item["full_zip_url"] = "https://cdn-mineru.openxlab.org.cn/result.zip?signature=secret"
                return httpx.Response(200, json={"code": 0, "data": {"extract_result": [item]}})
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, content=archive if archive is not None else archive_bytes())
        return handle

    def test_local_multipage_text_and_integrity_metadata(self):
        parsed = parse_pdf(self.path)
        self.assertEqual(parsed.pages, 2)
        self.assertEqual(parsed.backend, "local")
        self.assertIn("## Page 1", parsed.markdown)
        self.assertIn("amber warranty", parsed.markdown)
        self.assertLess(parsed.markdown.index("amber"), parsed.markdown.index("cobalt"))
        self.assertEqual(parsed.metadata["source_sha256"], __import__("hashlib").sha256(self.path.read_bytes()).hexdigest())
        self.assertFalse(parsed.metadata["ocr"])

    def test_local_input_failures_and_empty_scanned_text(self):
        self.path.write_bytes(b"not PDF")
        with self.assertRaises(ValidationError):
            parse_pdf(self.path)
        self.path.write_bytes(b"%PDF-1.7 invalid")
        with self.assertRaises(ValidationError):
            parse_pdf(self.path)
        self.path.write_bytes(pdf_bytes(("",)))
        with self.assertRaisesRegex(CapabilityError, "OCR"):
            parse_pdf(self.path)

    def test_local_page_and_size_limits(self):
        with self.assertRaisesRegex(ValidationError, "page limit"):
            LocalPdfParser(replace(self.settings, max_pages=1)).parse(self.path)
        with self.assertRaisesRegex(ValidationError, "size limit"):
            LocalPdfParser(replace(self.settings, max_input_bytes=5)).parse(self.path)
        with self.assertRaisesRegex(ValidationError, "text exceeds"):
            LocalPdfParser(replace(self.settings, max_text_bytes=5)).parse(self.path)

    def test_encrypted_pdf_rejected_before_upload(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(612, 792)
        writer.encrypt("secret-password")
        writer.write(self.path)
        calls = []
        with self.assertRaisesRegex(ValidationError, "encrypted"):
            parse_pdf(self.path, "mineru", self.settings, client=self.client(lambda req: calls.append(req)))
        self.assertEqual(calls, [])

    def test_mineru_upload_poll_download_preserves_structure_without_secrets(self):
        calls = []
        parsed = parse_pdf(self.path, "mineru", self.settings, client=self.client(self.handler(capture=calls)))
        self.assertEqual(parsed.backend, "mineru")
        self.assertEqual(parsed.pages, 2)
        self.assertIn("$$x^2$$", parsed.markdown)
        self.assertIn("<table>", parsed.markdown)
        self.assertEqual(parsed.metadata["states"], ["running", "done"])
        self.assertEqual(parsed.metadata["network_calls"], 5)
        self.assertEqual(parsed.metadata["formula_blocks"], 1)
        self.assertEqual(parsed.metadata["html_tables"], 1)
        self.assertNotIn(self.settings.api_key, repr(parsed))
        self.assertNotIn("signature", repr(parsed))
        self.assertNotIn(self.settings.api_key, repr(self.settings))

    def test_mineru_authentication_and_error_body_are_sanitized(self):
        for status, payload in ((401, {"error": self.settings.api_key}),
                                (429, {"secret": self.settings.api_key}),
                                (200, {"code": -1, "msg": self.settings.api_key})):
            with self.subTest(status=status):
                client = self.client(lambda req: httpx.Response(status, json=payload))
                with self.assertRaises(CapabilityError) as error:
                    parse_pdf(self.path, "mineru", self.settings, client=client)
                self.assertNotIn(self.settings.api_key, str(error.exception))

    def test_mineru_missing_key_unknown_backend_bad_settings(self):
        with self.assertRaisesRegex(CapabilityError, "not configured"):
            parse_pdf(self.path, "mineru")
        with self.assertRaises(ValidationError):
            parse_pdf(self.path, "other")
        for kwargs in ({"base_url": "http://mineru.net"}, {"base_url": "https://localhost"},
                       {"timeout_seconds": float("nan")}, {"max_pages": True},
                       {"model_version": "invented"}, {"api_key": "key with whitespace"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                PdfSettings(**kwargs)

    def test_mineru_network_errors_do_not_expose_signed_urls(self):
        def handler(request):
            raise httpx.ReadTimeout("private signed url and token", request=request)
        with self.assertRaises(CapabilityError) as error:
            parse_pdf(self.path, "mineru", self.settings, client=self.client(handler))
        self.assertNotIn("private signed", str(error.exception))

    def test_mineru_timeout_does_not_resubmit(self):
        now = [0.0]
        calls = []
        def handler(request):
            calls.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"code": 0, "data": {"batch_id": "batch",
                    "file_urls": ["https://mineru.oss-cn-shanghai.aliyuncs.com/upload"]}})
            if request.method == "PUT":
                return httpx.Response(200)
            return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{"state": "pending"}]}})
        parser = MinerUPdfParser(replace(self.settings, max_wait_seconds=.1, poll_interval_seconds=.1),
            client=self.client(handler), clock=lambda: now[0], sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds))
        with self.assertRaisesRegex(CapabilityError, "timed out"):
            parser.parse(self.path)
        self.assertEqual(sum(req.method == "POST" for req in calls), 1)

    def test_mineru_rejects_unknown_state_and_failed_jobs(self):
        for state in ("failed", "invented"):
            with self.subTest(state=state), self.assertRaises(CapabilityError):
                parse_pdf(self.path, "mineru", self.settings, client=self.client(self.handler(states=(state,))))

    def test_mineru_rejects_untrusted_upload_url_without_uploading(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"code": 0, "data": {"batch_id": "batch",
                "file_urls": ["https://untrusted.example/upload?secret=hidden"]}})
        with self.assertRaisesRegex(ValidationError, "untrusted"):
            parse_pdf(self.path, "mineru", self.settings, client=self.client(handler))
        self.assertEqual(len(calls), 1)

    def test_mineru_archive_safety_and_text_limits(self):
        link = ZipInfo("link")
        link.external_attr = 0o120777 << 16
        cases = [
            archive_bytes([("../full.md", "bad")]),
            archive_bytes([("C:/full.md", "bad")]),
            archive_bytes([(link, "target"), ("full.md", "text")]),
            archive_bytes([("wrong.md", "text")]),
            archive_bytes([("a/full.md", "one"), ("b/full.md", "two")]),
            archive_bytes([("full.md", b"\xff")]),
            archive_bytes([("full.md", " " )]),
            b"not a zip",
        ]
        for value in cases:
            with self.subTest(size=len(value)), self.assertRaises(CapabilityError):
                parse_pdf(self.path, "mineru", self.settings, client=self.client(self.handler(archive=value, states=("done",))))
        with self.assertRaisesRegex(CapabilityError, "Markdown exceeds"):
            parse_pdf(self.path, "mineru", replace(self.settings, max_text_bytes=5),
                client=self.client(self.handler(states=("done",))))
        with self.assertRaisesRegex(CapabilityError, "archive exceeds"):
            parse_pdf(self.path, "mineru", replace(self.settings, max_download_bytes=5),
                client=self.client(self.handler(states=("done",))))

    def test_mineru_removes_only_nuls_and_records_loss(self):
        original = "# Heading\n\nCode is at \x00\x00 here.\n\n$$x^2$$"
        parsed = parse_pdf(self.path, "mineru", self.settings, client=self.client(
            self.handler(archive=archive_bytes([("full.md", original)]), states=("done",))))
        self.assertEqual(parsed.markdown, original.replace("\x00", "") + "\n")
        self.assertEqual(parsed.metadata["removed_nul_characters"], 2)
        self.assertEqual(parsed.metadata["markdown_bytes"], len(original.encode()))
        with self.assertRaisesRegex(CapabilityError, "empty Markdown"):
            parse_pdf(self.path, "mineru", self.settings, client=self.client(
                self.handler(archive=archive_bytes([("full.md", " \x00\x00 ")]), states=("done",))))

    def checkpoint(self):
        return self.path.parent / "recovery" / "mineru.json"

    def test_mineru_checkpoint_success_and_resume_skip_submission_and_upload(self):
        checkpoint = self.checkpoint()
        parsed = parse_pdf(self.path, "mineru", self.settings, checkpoint_path=checkpoint,
            client=self.client(self.handler(states=("done",))))
        saved = json.loads(checkpoint.read_text())
        self.assertEqual(saved["phase"], "complete")
        self.assertEqual(saved["batch_id"], "batch-1")
        self.assertEqual(saved["version"], 1)
        self.assertNotIn(self.settings.api_key, checkpoint.read_text())
        self.assertNotIn("signature", checkpoint.read_text())
        self.assertFalse(parsed.metadata["resumed_from_checkpoint"])
        calls = []
        resumed = parse_pdf(self.path, "mineru", self.settings, checkpoint_path=checkpoint,
            client=self.client(self.handler(states=("done",), capture=calls)))
        self.assertTrue(resumed.metadata["resumed_from_checkpoint"])
        self.assertEqual(resumed.metadata["batch_id"], "batch-1")
        self.assertEqual([request.method for request in calls], ["GET", "GET"])
        self.assertEqual(list(checkpoint.parent.glob("*.tmp")), [])

    def test_mineru_checkpoint_timeout_and_resume_do_not_resubmit(self):
        checkpoint = self.checkpoint()
        now = [0.0]
        calls = []
        parser = MinerUPdfParser(replace(self.settings, max_wait_seconds=.1, poll_interval_seconds=.1),
            client=self.client(self.handler(states=("pending",), capture=calls)),
            clock=lambda: now[0], sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds))
        with self.assertRaisesRegex(CapabilityError, "timed out"):
            parser.parse(self.path, checkpoint_path=checkpoint)
        self.assertEqual(json.loads(checkpoint.read_text())["phase"], "uploaded")
        parse_pdf(self.path, "mineru", self.settings, checkpoint_path=checkpoint,
            client=self.client(self.handler(states=("done",), capture=calls)))
        self.assertEqual(sum(request.method == "POST" for request in calls), 1)
        self.assertEqual(sum(request.method == "PUT" for request in calls), 1)

    def test_mineru_lost_submission_response_blocks_automatic_resubmission(self):
        checkpoint = self.checkpoint()
        calls = []
        def broken(request):
            calls.append(request)
            raise httpx.ReadTimeout("hidden token", request=request)
        with self.assertRaisesRegex(CapabilityError, "network request failed"):
            parse_pdf(self.path, "mineru", self.settings, checkpoint_path=checkpoint, client=self.client(broken))
        self.assertEqual(json.loads(checkpoint.read_text())["phase"], "submitting")
        with self.assertRaisesRegex(CapabilityError, "outcome is unknown"):
            parse_pdf(self.path, "mineru", self.settings, checkpoint_path=checkpoint, client=self.client(broken))
        self.assertEqual(len(calls), 1)

    def test_mineru_uncertain_upload_resumes_polling_but_never_reuploads(self):
        checkpoint = self.checkpoint()
        regular = self.handler(states=("done",))
        def upload_timeout(request):
            if request.method == "PUT":
                raise httpx.ReadTimeout("private signed URL", request=request)
            return regular(request)
        with self.assertRaisesRegex(CapabilityError, "network request failed"):
            parse_pdf(self.path, "mineru", self.settings, checkpoint_path=checkpoint,
                client=self.client(upload_timeout))
        self.assertEqual(json.loads(checkpoint.read_text())["phase"], "allocated")
        calls = []
        with self.assertRaisesRegex(CapabilityError, "unconfirmed upload"):
            parse_pdf(self.path, "mineru", self.settings, checkpoint_path=checkpoint,
                client=self.client(self.handler(states=("waiting-file",), capture=calls)))
        self.assertEqual([request.method for request in calls], ["GET"])
        parse_pdf(self.path, "mineru", self.settings, checkpoint_path=checkpoint,
            client=self.client(self.handler(states=("running", "done"), capture=calls)))
        self.assertTrue(all(request.method == "GET" for request in calls))

    def test_mineru_checkpoint_binding_phase_and_version_reject_before_network(self):
        checkpoint = self.checkpoint()
        parse_pdf(self.path, "mineru", self.settings, checkpoint_path=checkpoint,
            client=self.client(self.handler(states=("done",))))
        original = json.loads(checkpoint.read_text())
        calls = []
        client = self.client(lambda request: calls.append(request))
        for field, value in (("version", 2), ("version", True), ("backend", "other"),
                             ("source_sha256", "0" * 64), ("file_name", "other.pdf"),
                             ("model_version", "pipeline"), ("language", "en"),
                             ("base_url", "https://different.example/api"),
                             ("phase", "unknown"), ("batch_id", "../invalid")):
            with self.subTest(field=field, value=value):
                checkpoint.write_text(json.dumps({**original, field: value}))
                with self.assertRaises(ValidationError):
                    parse_pdf(self.path, "mineru", self.settings, checkpoint_path=checkpoint, client=client)
        for value in ("{", " " * 16_385, json.dumps({**original, "unexpected": "value"})):
            checkpoint.write_text(value)
            with self.assertRaises(ValidationError):
                parse_pdf(self.path, "mineru", self.settings, checkpoint_path=checkpoint, client=client)
        with self.assertRaisesRegex(ValidationError, "cannot replace"):
            parse_pdf(self.path, "mineru", self.settings, checkpoint_path=self.path, client=client)
        with self.assertRaisesRegex(ValidationError, "requires the mineru"):
            parse_pdf(self.path, checkpoint_path=checkpoint)
        self.assertEqual(calls, [])

    def test_mineru_checkpoint_survives_archive_failure_and_atomic_write_failure(self):
        checkpoint = self.checkpoint()
        with self.assertRaisesRegex(CapabilityError, "empty Markdown"):
            parse_pdf(self.path, "mineru", self.settings, checkpoint_path=checkpoint, client=self.client(
                self.handler(archive=archive_bytes([("full.md", " " )]), states=("done",))))
        before = checkpoint.read_bytes()
        self.assertEqual(json.loads(before)["phase"], "uploaded")
        calls = []
        failure = OSError(errno.EACCES, "private path and hidden token")
        with patch("adaptive_rag.pdf.os.replace", side_effect=failure):
            with self.assertRaisesRegex(CapabilityError, "during replace.*errno=13") as error:
                parse_pdf(self.path, "mineru", self.settings, checkpoint_path=checkpoint,
                    client=self.client(self.handler(states=("done",), capture=calls)))
        self.assertNotIn("private path", str(error.exception))
        self.assertNotIn("hidden token", str(error.exception))
        self.assertTrue(all(request.method == "GET" for request in calls))
        self.assertEqual(checkpoint.read_bytes(), before)
        self.assertEqual(list(checkpoint.parent.glob("*.tmp")), [])

    def test_mineru_redirect_is_not_followed(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "https://untrusted.example/"})
        with self.assertRaises(CapabilityError):
            parse_pdf(self.path, "mineru", self.settings, client=self.client(handler))
        self.assertEqual(len(calls), 1)

    def test_injected_client_auth_cannot_leak_to_storage(self):
        calls = []
        client = httpx.Client(transport=httpx.MockTransport(self.handler(states=("done",), capture=calls)),
                             headers={"Authorization": "Bearer client-default-secret"},
                             auth=("default-user", "default-password"))
        self.addCleanup(client.close)
        parse_pdf(self.path, "mineru", self.settings, client=client)
        self.assertEqual(len(calls), 4)
        self.assertNotIn("authorization", calls[1].headers)
        self.assertNotIn("authorization", calls[3].headers)

    def test_mineru_api_payload_shape_and_size_are_bounded(self):
        for payload in ({"code": True, "data": {}}, {"code": 0, "data": []},
                        {"code": 0, "data": {"batch_id": "../secret", "file_urls": []}}):
            with self.subTest(payload=payload), self.assertRaises(CapabilityError):
                parse_pdf(self.path, "mineru", self.settings,
                    client=self.client(lambda request: httpx.Response(200, json=payload)))
        with self.assertRaisesRegex(CapabilityError, "size limit"):
            parse_pdf(self.path, "mineru", self.settings,
                client=self.client(lambda request: httpx.Response(200, content=b"x" * 2_000_001)))


if __name__ == "__main__":
    unittest.main()
