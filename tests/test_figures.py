"""Figures inside documents: from a MinerU archive to a cited picture on the evidence route.

Everything runs on small generated pictures, fake transports and a temporary data
directory. No test reaches the network or a paid model.
"""

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from adaptive_rag.api import create_api
from adaptive_rag.app import RagApplication
from adaptive_rag.cloud import CloudDenseRetriever, CloudEmbedder, CloudReranker, evidence_chat_rows
from adaptive_rag.config import Settings
from adaptive_rag.domain import (AccessContext, EvidenceImage, Hit, IngestRequest, NotFoundError, PdfIngestResult,
                                 QueryRequest, SubQuery, ValidationError)
from adaptive_rag.figures import (MARKER, Figure, attach_figures, clean_caption, cut_before_figure, find_figures,
                                  marker, plain_text, readable_text, sniff_media_type)
from adaptive_rag.ingestion import ParentChildChunker, StructuredParser, TextCleaner
from adaptive_rag.pdf import MinerUPdfParser, ParsedPdf, PdfSettings, parse_pdf
from support import ADMIN_PASSWORD
from test_cloud import chunk, evidence, transport
from test_pdf import pdf_bytes

KEY = "a" * 64


def picture(color=(200, 30, 30), kind="PNG", size=(12, 8)):
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, kind)
    return buffer.getvalue()


def noisy_picture(size=(64, 64)):
    """A PNG that does not compress, so its size is predictable."""
    noise = random.Random(7).randbytes(size[0] * size[1] * 3)
    buffer = BytesIO()
    Image.frombytes("RGB", size, noise).save(buffer, "PNG")
    return buffer.getvalue()


def archive(markdown, images=None, content_list=None, *, folder="", stored=False, extra=()):
    """A MinerU result archive: full.md, the pictures it points at and, optionally, a v1 content list."""
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_STORED if stored else ZIP_DEFLATED) as zipped:
        zipped.writestr(f"{folder}full.md", markdown)
        for name, content in (images or {}).items():
            zipped.writestr(f"{folder}{name}", content)
        if content_list is not None:
            zipped.writestr(f"{folder}abc_content_list.json", content_list if isinstance(content_list, (bytes, str))
                            else json.dumps(content_list))
        for name, content in extra:
            zipped.writestr(name, content)
    return buffer.getvalue()


def extract(raw):
    """Markdown, figures and metadata the MinerU parser reads out of a result archive."""
    return MinerUPdfParser(PdfSettings())._markdown(raw)


def mineru_client(path, result):
    digest = sha256(path.read_bytes()).hexdigest()

    def handler(request):
        if request.url.path.endswith("/file-urls/batch"):
            return httpx.Response(200, json={"code": 0, "data": {"batch_id": "batch-1", "file_urls": [
                "https://mineru.oss-cn-shanghai.aliyuncs.com/upload?signature=secret"]}})
        if request.method == "PUT":
            return httpx.Response(200)
        if "extract-results" in request.url.path:
            return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{
                "state": "done", "file_name": path.name, "data_id": digest,
                "full_zip_url": "https://cdn-mineru.openxlab.org.cn/result.zip?signature=secret"}]}})
        return httpx.Response(200, content=result)

    return httpx.Client(transport=httpx.MockTransport(handler))


def ingest_archive(app, path, result, **metadata):
    with mineru_client(path, result) as client, patch("adaptive_rag.pdf.httpx.Client", return_value=client):
        return app.ingest_pdf(path, backend="mineru", pdf_settings=PdfSettings(api_key="test-key"), **metadata)


MARKDOWN = ("# Attention\n\n"
            "![](images/one.png)  \n\n"
            "![](images/two.png)  \n\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
            "![](images/three.png)  \n")
IMAGES = {"images/one.png": picture((200, 30, 30)), "images/two.png": picture((30, 200, 30), "JPEG"),
          "images/three.png": picture((30, 30, 200))}
CONTENT_LIST = [
    {"type": "text", "text": "Attention", "text_level": 1, "page_idx": 0},
    {"type": "image", "img_path": "images/one.png", "image_caption": ["Figure 1: ", "The architecture."],
     "image_footnote": [], "page_idx": 2},
    # MinerU puts a chart's caption into its footnote.
    {"type": "chart", "img_path": "images/two.png", "chart_caption": [], "chart_footnote": ["Figure 2: Scores by head"],
     "page_idx": 3},
    {"type": "table", "img_path": "images/table.png", "table_caption": ["Table 1"], "page_idx": 4},
]


class FigureTextTests(unittest.TestCase):
    def test_marker_round_trips_caption_and_page(self):
        text = marker(KEY, "Figure 1: 架构", 3)
        self.assertEqual(text, f'![Figure 1: 架构](ragfig:{KEY} "第 3 页")')
        self.assertEqual(find_figures(f"before\n\n{text}\n\nafter"), (EvidenceImage(KEY, "Figure 1: 架构", 3),))
        self.assertEqual(marker(KEY), f"![](ragfig:{KEY})")
        self.assertEqual(find_figures(marker(KEY)), (EvidenceImage(KEY, "", None),))

    def test_figures_are_listed_once_per_image_in_reading_order(self):
        other = "b" * 64
        text = "\n".join((marker(other, "second"), marker(KEY, "first"), marker(other, "again")))
        self.assertEqual([image.key for image in find_figures(text)], [other, KEY])
        self.assertEqual(find_figures(text)[0].caption, "second")
        self.assertEqual(find_figures("no figure here"), ())

    def test_caption_stays_on_one_line_and_cannot_end_the_marker(self):
        self.assertEqual(clean_caption("a [b] c\nd\\e\x00f"), "a (b) c d e f")
        # Decomposed input comes out composed, so a search for the caption matches.
        self.assertEqual(clean_caption("e" + chr(0x301)), chr(0xE9))
        hostile = "x](ragfig:" + "b" * 64 + ")\n![y](ragfig:" + "c" * 64 + ")"
        text = marker(KEY, hostile)
        self.assertIsNotNone(MARKER.fullmatch(text), text)
        self.assertEqual([image.key for image in find_figures(text)], [KEY])
        self.assertNotIn("\n", text)

    def test_long_caption_is_cut_to_the_limit(self):
        cleaned = clean_caption("字" * 500)
        self.assertEqual(len(cleaned), 160)
        self.assertTrue(cleaned.endswith("…"))
        self.assertEqual(clean_caption("short"), "short")

    def test_models_and_word_matching_never_see_the_storage_key(self):
        text = f"intro\n\n{marker(KEY, 'Figure 1: 架构', 3)}\n\n{marker(KEY, '', None)}\n\n{marker('b' * 64, '', 8)}\n\n{marker('c' * 64, 'only')}"
        readable = readable_text(text)
        self.assertIn("［插图：Figure 1: 架构，第 3 页］", readable)
        self.assertIn("［插图］", readable)
        self.assertIn("［插图：第 8 页］", readable)
        self.assertIn("［插图：only］", readable)
        for form in (readable, plain_text(text)):
            self.assertNotIn("ragfig", form)
            for key in (KEY, "b" * 64, "c" * 64):
                self.assertNotIn(key, form)
        self.assertIn("Figure 1: 架构", plain_text(text))
        self.assertEqual(readable_text("plain text"), "plain text")

    def test_media_type_comes_from_the_bytes(self):
        self.assertEqual(sniff_media_type(picture(kind="PNG")), "image/png")
        self.assertEqual(sniff_media_type(picture(kind="JPEG")), "image/jpeg")
        self.assertEqual(sniff_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        for content in (b"", b"GIF89a", b"%PDF-1.7", b"<svg onload=alert(1)>", b"<html>"):
            self.assertIsNone(sniff_media_type(content), content)

    def test_a_cut_never_lands_inside_a_figure_line(self):
        line = marker(KEY, "Figure 1", 2)
        text = "abc " + line + " tail"
        start, end = 4, 4 + len(line)
        for length in range(start + 1, end):
            self.assertEqual(cut_before_figure(text, length), start, length)
        for length in (0, 1, start, end, end + 3, len(text), len(text) + 50):
            self.assertEqual(cut_before_figure(text, length), length, length)
        # A figure line at the very start is cut as asked rather than to nothing.
        self.assertEqual(cut_before_figure(line + " tail", 10), 10)
        two = line + "\n" + marker("b" * 64, "second") + "\nend"
        self.assertEqual(cut_before_figure(two, len(line) + 5), len(line) + 1)


class MinerUArchiveTests(unittest.TestCase):
    def test_figures_carry_the_caption_and_page_from_the_content_list(self):
        markdown, figures, metadata = extract(archive(MARKDOWN, IMAGES, CONTENT_LIST))
        by_path = {figure.path: figure for figure in figures}
        self.assertEqual(list(by_path), ["images/one.png", "images/two.png", "images/three.png"])
        self.assertEqual((by_path["images/one.png"].caption, by_path["images/one.png"].page),
                         ("Figure 1: The architecture.", 3))
        self.assertEqual((by_path["images/two.png"].caption, by_path["images/two.png"].page),
                         ("Figure 2: Scores by head", 4))
        # The content list does not describe this picture, which is still extracted.
        self.assertEqual((by_path["images/three.png"].caption, by_path["images/three.png"].page), ("", None))
        self.assertEqual(by_path["images/one.png"].content, IMAGES["images/one.png"])
        self.assertEqual((metadata["figures_referenced"], metadata["figures_extracted"]), (3, 3))
        # The Markdown itself is untouched here: rewriting references is attach_figures' job.
        self.assertIn("![](images/one.png)", markdown)
        self.assertIn("| a | b |", markdown)

    def test_only_pictures_the_markdown_points_at_are_read(self):
        images = {**IMAGES, "images/table.png": picture((9, 9, 9)), "images/strip.png": picture((8, 8, 8))}
        _, figures, metadata = extract(archive(MARKDOWN, images, CONTENT_LIST))
        self.assertEqual([figure.path for figure in figures], ["images/one.png", "images/two.png", "images/three.png"])
        self.assertEqual(metadata["figures_referenced"], 3)

    def test_without_a_content_list_the_pictures_have_no_caption(self):
        _, figures, metadata = extract(archive(MARKDOWN, IMAGES))
        self.assertEqual([(figure.caption, figure.page) for figure in figures], [("", None)] * 3)
        self.assertEqual(metadata["figures_extracted"], 3)

    def test_content_list_in_the_markdown_folder_is_used(self):
        _, figures, _ = extract(archive(MARKDOWN, IMAGES, CONTENT_LIST, folder="result/"))
        self.assertEqual(figures[0].caption, "Figure 1: The architecture.")
        # A list that lies elsewhere or is not the only one is not trusted to describe these pictures.
        two_lists = archive(MARKDOWN, IMAGES, CONTENT_LIST, extra=[("other_content_list.json", json.dumps(CONTENT_LIST))])
        self.assertEqual([figure.caption for figure in extract(two_lists)[1]], ["", "", ""])
        elsewhere = archive(MARKDOWN, IMAGES, extra=[("nested/abc_content_list.json", json.dumps(CONTENT_LIST))])
        self.assertEqual([figure.caption for figure in extract(elsewhere)[1]], ["", "", ""])

    def test_a_damaged_or_odd_content_list_never_fails_the_parse(self):
        for broken in (b"{not json", b"\xff\xfe", json.dumps({"type": "image"}), json.dumps(["text", 7, None]), "[]"):
            with self.subTest(broken=broken):
                markdown, figures, metadata = extract(archive(MARKDOWN, IMAGES, broken))
                self.assertIn("Attention", markdown)
                self.assertEqual([figure.caption for figure in figures], ["", "", ""])
                self.assertEqual(metadata["figures_extracted"], 3)
        with patch("adaptive_rag.pdf._CONTENT_LIST_BYTES", 10):
            self.assertEqual([figure.caption for figure in extract(archive(MARKDOWN, IMAGES, CONTENT_LIST))[1]],
                             ["", "", ""])

    def test_caption_and_page_fields_tolerate_odd_shapes(self):
        content_list = [
            "not an object",
            {"type": "image", "img_path": "images/one.png", "image_caption": "Figure 1: plain string", "page_idx": True},
            {"type": "image", "img_path": "images/two.png", "image_caption": [1, None, "  ", "Figure 2: kept"], "page_idx": -1},
            {"type": "image", "img_path": 7},
            {"type": "image", "img_path": "images/three.png", "image_caption": [], "image_footnote": ["A footnote"],
             "page_idx": 99999},
            {"type": "image", "img_path": "images/one.png", "image_caption": ["Later duplicate"], "page_idx": 5},
        ]
        _, figures, _ = extract(archive(MARKDOWN, IMAGES, content_list))
        self.assertEqual([(figure.caption, figure.page) for figure in figures],
                         [("Figure 1: plain string", None), ("Figure 2: kept", None), ("A footnote", None)])
        zero_based = [{"type": "image", "img_path": "images/one.png", "image_caption": ["First page"], "page_idx": 0}]
        self.assertEqual(extract(archive(MARKDOWN, IMAGES, zero_based))[1][0].page, 1)

    def test_missing_unsafe_and_empty_entries_are_left_out(self):
        markdown = ("![](images/one.png)\n![](images/missing.png)\n![](images/../full.md)\n![](images/empty.png)\n"
                    "![](https://example.test/remote.png)\n![](/etc/passwd)\n")
        images = {"images/one.png": IMAGES["images/one.png"], "images/empty.png": b""}
        _, figures, metadata = extract(archive(markdown, images))
        self.assertEqual([figure.path for figure in figures], ["images/one.png"])
        self.assertEqual((metadata["figures_referenced"], metadata["figures_extracted"]), (4, 1))

    def test_oversized_pictures_and_too_many_pictures_are_left_out(self):
        small, big = IMAGES["images/one.png"], noisy_picture()
        markdown = "![](images/big.png)\n![](images/one.png)\n![](images/two.png)\n![](images/three.png)\n"
        raw = archive(markdown, {"images/big.png": big, "images/one.png": small, "images/two.png": IMAGES["images/two.png"],
                                 "images/three.png": IMAGES["images/three.png"]})
        self.assertGreater(len(big), len(small))
        with patch("adaptive_rag.pdf.MAX_FIGURE_BYTES", len(big) - 1):
            self.assertEqual([figure.path for figure in extract(raw)[1]], ["images/one.png", "images/two.png", "images/three.png"])
        with patch("adaptive_rag.pdf.MAX_FIGURES", 2):
            self.assertEqual([figure.path for figure in extract(raw)[1]], ["images/big.png", "images/one.png"])
        with patch("adaptive_rag.pdf.MAX_FIGURE_TOTAL_BYTES", len(big) + len(small)):
            self.assertEqual([figure.path for figure in extract(raw)[1]], ["images/big.png", "images/one.png"])

    def test_a_corrupt_picture_is_skipped_and_the_text_still_arrives(self):
        damaged = picture((11, 22, 33), "PNG", (30, 30))
        raw = bytearray(archive("# Text\n\n![](images/bad.png)\n![](images/one.png)\n",
                               {"images/bad.png": damaged, "images/one.png": IMAGES["images/one.png"]}, stored=True))
        raw[raw.index(damaged) + len(damaged) // 2] ^= 0xFF
        markdown, figures, metadata = extract(bytes(raw))
        self.assertIn("# Text", markdown)
        self.assertEqual([figure.path for figure in figures], ["images/one.png"])
        self.assertEqual((metadata["figures_referenced"], metadata["figures_extracted"]), (2, 1))

    def test_a_document_without_pictures_reports_zero(self):
        markdown, figures, metadata = extract(archive("# Heading\n\nJust text."))
        self.assertEqual(figures, ())
        self.assertEqual((metadata["figures_referenced"], metadata["figures_extracted"]), (0, 0))

    def test_the_public_parser_hands_the_figures_to_its_caller(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "fixture.pdf"
        path.write_bytes(pdf_bytes())
        client = mineru_client(path, archive(MARKDOWN, IMAGES, CONTENT_LIST))
        self.addCleanup(client.close)
        parsed = parse_pdf(path, "mineru", PdfSettings(api_key="private-test-key-never-output", poll_interval_seconds=.001),
                           client=client)
        self.assertEqual([figure.page for figure in parsed.figures], [3, 4, None])
        self.assertEqual(parsed.metadata["figures_extracted"], 3)
        self.assertNotIn("private-test-key-never-output", repr(parsed))
        self.assertNotIn(repr(IMAGES["images/one.png"]), repr(parsed))  # bytes stay out of logs and reprs
        # The local backend extracts no pictures; code written before figures existed still builds a ParsedPdf.
        self.assertEqual(ParsedPdf("text", 1, "local", .01, {}).figures, ())


class AttachFiguresTests(unittest.TestCase):
    def setUp(self):
        self.stored = {}

    def store(self, content):
        key = sha256(content).hexdigest()
        self.stored[key] = content
        return key

    def test_references_become_markers_and_the_bytes_are_stored_by_content(self):
        figures = [Figure("images/one.png", IMAGES["images/one.png"], "Figure 1", 3),
                   Figure("images/two.png", IMAGES["images/two.png"], "", None)]
        text, summary, validated = attach_figures("A\n\n![](images/one.png)  \n\nB ![x](images/two.png) C", figures, self.store)
        one, two = (sha256(IMAGES[name]).hexdigest() for name in ("images/one.png", "images/two.png"))
        self.assertEqual(text, f'A\n\n![Figure 1](ragfig:{one} "第 3 页")  \n\nB ![](ragfig:{two}) C')
        self.assertEqual(self.stored, {one: IMAGES["images/one.png"], two: IMAGES["images/two.png"]})
        self.assertEqual(validated, (one, two))
        self.assertEqual(summary, {"referenced": 2, "stored": 2, "skipped": 0})

    def test_a_picture_that_cannot_be_used_is_removed_but_its_caption_text_stays(self):
        figures = [Figure("images/text.png", b"this is not an image", "ignored", 1),
                   Figure("images/vector.png", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "", None),
                   Figure("images/ok.png", IMAGES["images/one.png"], "Fine", 2)]
        markdown = "Intro\n\n![](images/text.png)\n\n![](images/vector.png)\n\nFigure 2: the caption.\n\n![](images/ok.png)\n![](images/gone.png)"
        text, summary, validated = attach_figures(markdown, figures, self.store)
        self.assertEqual(validated, tuple(self.stored))
        self.assertNotIn("images/", text)
        self.assertIn("Figure 2: the caption.", text)
        self.assertIn("ragfig:" + sha256(IMAGES["images/one.png"]).hexdigest(), text)
        self.assertEqual(len(self.stored), 1)
        self.assertEqual(summary, {"referenced": 4, "stored": 1, "skipped": 3})

    def test_a_picture_used_twice_gets_the_same_key_and_is_stored_once_per_reference_path(self):
        content = IMAGES["images/one.png"]
        figures = [Figure("images/a.png", content, "A", 1), Figure("images/b.png", content, "B", 2)]
        text, summary, validated = attach_figures("![](images/a.png)\n![](images/a.png)\n![](images/b.png)", figures, self.store)
        self.assertEqual(validated, (sha256(content).hexdigest(),))
        self.assertEqual({image.key for image in find_figures(text)}, {sha256(content).hexdigest()})
        self.assertEqual(text.count("ragfig:"), 3)
        self.assertEqual(summary["referenced"], 2)
        self.assertEqual(len(self.stored), 1)

    def test_text_without_archive_references_is_returned_unchanged(self):
        self.assertEqual(attach_figures("plain text", [], self.store),
                         ("plain text", {"referenced": 0, "stored": 0, "skipped": 0}, ()))
        # Only archive-relative paths are rewritten; a link to another site is left as the author wrote it.
        linked = "No pictures. ![alt](https://example.test/x.png) stays."
        self.assertEqual(attach_figures(linked, [], self.store)[0], linked)
        self.assertEqual(self.stored, {})


class FigureChunkingTests(unittest.TestCase):
    def chunks(self, content, parent=1600, child=320, overlap=40):
        request = IngestRequest(tenant_id="demo", document_id="paper", version="1", content=content)
        sections = TextCleaner().clean(StructuredParser().parse(request))
        return ParentChildChunker(parent, child, overlap).chunk(request, "rev", sections)

    def test_a_figure_line_is_never_split_between_chunks(self):
        line = marker(KEY, "Figure 1: Transformer 模型架构，编码器在左，解码器在右。", 3)
        for lead in range(150, 340, 9):
            with self.subTest(lead=lead):
                content = "前言。" * (lead // 3) + "\n\n" + line + "\n\n" + "结尾。" * 60
                parents, children = self.chunks(content)
                for chunk_ in (*parents, *children):
                    self.assertEqual(chunk_.text.count("ragfig:"), len(MARKER.findall(chunk_.text)), chunk_.text)
                self.assertTrue(any(line in child.text for child in children))
                self.assertTrue(any(line in parent.text for parent in parents))

    def test_each_child_text_is_still_a_slice_of_its_source(self):
        line = marker(KEY, "Figure 1", 3)
        content = "# 标题\n\n" + "前言。" * 90 + "\n\n" + line + "\n\n" + "结尾。" * 90
        request = IngestRequest(tenant_id="demo", document_id="paper", version="1", content=content)
        parents, children = ParentChildChunker().chunk(request, "rev", TextCleaner().clean(StructuredParser().parse(request)))
        by_id = {parent.parent_id: parent for parent in parents}
        for child in children:
            self.assertIn(child.text, by_id[child.parent_id].text)
            self.assertIn(child.text, child.retrieval_text)


class FigureQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.access = AccessContext("demo", "reader")

    def application(self, **changes):
        return RagApplication(Settings(data_dir=self.directory / "data", **changes))

    def publish(self, app, content, document_id="paper"):
        staged = app.ingest(IngestRequest(tenant_id="demo", document_id=document_id, version="v1", content=content,
                                          title=document_id))
        app.publish("demo", staged.job_id, None)

    async def test_a_figure_is_found_through_its_caption_and_listed_on_the_evidence(self):
        app = self.application()
        key = app.objects.put(IMAGES["images/one.png"])
        self.publish(app, f"# Models\n\nThe encoder stacks six layers.\n\n{marker(key, 'Figure 1: Encoder stack', 3)}\n\nOther prose.")
        result = await app.query(QueryRequest(self.access, "Encoder stack"))
        self.assertEqual(result.status, "evidence_preview")
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0].images, (EvidenceImage(key, "Figure 1: Encoder stack", 3),))
        item = result.evidence[0]
        fetched = app.get_evidence(self.access, result.generation, item.evidence_id)
        self.assertEqual((fetched.text, fetched.images), (item.text, item.images))
        with self.assertRaises(NotFoundError):
            app.get_evidence_image(self.access, result.generation, item.evidence_id, key)

    async def test_the_storage_key_alone_is_not_a_reason_to_show_a_source(self):
        app = self.application()
        key = app.objects.put(IMAGES["images/one.png"])
        self.publish(app, f"# Models\n\nThe encoder stacks six layers.\n\n{marker(key, 'Figure 1: Encoder stack', 3)}")
        for question in ("ragfig", key, key[:20]):
            with self.subTest(question=question[:12]):
                result = await app.query(QueryRequest(self.access, question))
                self.assertEqual(result.status, "empty")
                self.assertEqual(result.evidence, ())

    def test_the_relevance_gate_reads_a_figure_line_as_its_caption(self):
        from adaptive_rag.relevance import RelevanceGate
        figure_chunk = chunk("figure", f"Intro.\n\n{marker(KEY, 'Figure 1: Encoder stack', 3)}")
        hit = Hit("q1", figure_chunk, "bm25", 1.0, 1)
        gate = RelevanceGate()
        for question in ("ragfig", KEY, KEY[:20], "ragfig:" + KEY):
            with self.subTest(question=question[:12]):
                self.assertEqual(gate.assess(SubQuery("q1", question), [hit]).hits, ())
        self.assertEqual(gate.assess(SubQuery("q1", "encoder stack"), [hit]).hits, (hit,))

    async def test_an_evidence_cut_never_ends_inside_a_figure_line(self):
        app = self.application(evidence_chars=300)
        key = app.objects.put(IMAGES["images/one.png"])
        self.publish(app, "# Notes\n\n" + "alpha " * 42 + "\n\n" + marker(key, "Figure 9: Alpha chart", 4) + "\n\nomega")
        result = await app.query(QueryRequest(self.access, "alpha", top_k=1))
        item, = result.evidence
        self.assertTrue(item.truncated)
        self.assertNotIn("ragfig", item.text)
        self.assertNotIn("![", item.text)
        self.assertEqual(item.images, ())
        # The id records the length actually used, so the same evidence can be rebuilt from it.
        fetched = app.get_evidence(self.access, result.generation, item.evidence_id)
        self.assertEqual((fetched.text, fetched.images), (item.text, item.images))
        # A wider cut that holds the whole line keeps the figure.
        wide = self.application(evidence_chars=1200)
        result = await wide.query(QueryRequest(self.access, "alpha", top_k=1))
        self.assertEqual([image.caption for image in result.evidence[0].images], ["Figure 9: Alpha chart"])

    async def test_evidence_sent_to_a_model_names_the_figure_without_its_key(self):
        text = f"The loop.\n\n{marker(KEY, 'Figure 4: Training loop', 12)}"
        rows = evidence_chat_rows([evidence(text)])
        self.assertIn("［插图：Figure 4: Training loop，第 12 页］", rows[0]["text"])
        self.assertNotIn("ragfig", json.dumps(rows, ensure_ascii=False))
        self.assertNotIn(KEY, json.dumps(rows, ensure_ascii=False))

    async def test_dense_and_rerank_inputs_use_the_readable_form(self):
        text = f"Loop chart.\n\n{marker(KEY, 'Figure 4: Training loop', 12)}"
        seen = []

        def embed(request):
            payload = json.loads(request.content)
            seen.append(payload["input"])
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [1, 0]} for i in range(len(payload["input"]))]})

        figure_chunk = chunk("figure", text)
        retriever = CloudDenseRetriever(CloudEmbedder(transport(embed)), self.directory)
        await retriever.search(SubQuery("q1", "training loop"), (figure_chunk,), 1)
        # The question is embedded first, then the one chunk that is not cached yet.
        self.assertEqual(seen[0], ["training loop"])
        self.assertEqual(len(seen), 2)
        self.assertIn("［插图：Figure 4: Training loop，第 12 页］", seen[1][0])
        self.assertNotIn("ragfig", seen[1][0])
        # The vector cache is keyed on the stored text, so a second search embeds only the question.
        await retriever.search(SubQuery("q1", "training loop"), (figure_chunk,), 1)
        self.assertEqual(seen[2:], [["training loop"]])

        def rank(request):
            documents = json.loads(request.content)["documents"]
            self.assertNotIn("ragfig", json.dumps(documents))
            self.assertIn("［插图：Figure 4: Training loop，第 12 页］", documents[0])
            return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": .9}]})

        hits = (Hit("q1", figure_chunk, "rrf", .1, 1),)
        ranked = await CloudReranker(transport(rank)).rank(SubQuery("q1", "training loop"), hits, 1)
        self.assertEqual([hit.chunk for hit in ranked], [figure_chunk])


class PdfFigureIngestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.pdf = self.root / "paper.pdf"
        self.raw = pdf_bytes(("Attention paper.",))
        self.pdf.write_bytes(self.raw)
        self.app = RagApplication(Settings(data_dir=self.root / "data"))
        self.access = AccessContext("demo", "reader")

    def ingest(self, raw_archive):
        return ingest_archive(self.app, self.pdf, raw_archive, tenant_id="demo", document_id="paper", version="v1",
                              title="Attention paper")

    async def test_an_archive_becomes_a_searchable_document_with_citable_pictures(self):
        markdown = "# Model\n\nThe encoder has six layers.\n\n![](images/one.png)  \n\n![](images/broken.png)  \n\nFigure 1: Encoder.\n"
        broken = b"\x89PNG\r\n\x1a\nnot really a picture"
        result = self.ingest(archive(markdown, {"images/one.png": IMAGES["images/one.png"], "images/broken.png": broken},
                                     [{"type": "image", "img_path": "images/one.png",
                                       "image_caption": ["Figure 1: Encoder stack"], "page_idx": 1}]))
        self.assertIsInstance(result, PdfIngestResult)
        self.assertEqual(result.figures, {"referenced": 2, "stored": 1, "skipped": 1})
        self.assertEqual(result.state, "staged")
        self.app.publish("demo", result.job_id, None)
        restarted = RagApplication(self.app.settings)
        answer = await restarted.query(QueryRequest(self.access, "Encoder stack"))
        item, = answer.evidence
        image, = item.images
        self.assertEqual((image.caption, image.page), ("Figure 1: Encoder stack", 2))
        self.assertEqual(restarted.get_evidence_image(self.access, answer.generation, item.evidence_id, image.key),
                         (IMAGES["images/one.png"], "image/png"))
        self.assertNotIn("images/", item.text)

    async def test_the_source_pdf_and_every_figure_are_in_the_object_store(self):
        result = self.ingest(archive("![](images/one.png)\n\ntext", {"images/one.png": IMAGES["images/one.png"]}))
        self.assertEqual(self.app.objects.get(sha256(self.raw).hexdigest()), self.raw)
        self.assertEqual(self.app.objects.get(sha256(IMAGES["images/one.png"]).hexdigest()), IMAGES["images/one.png"])
        self.assertEqual(result.figures["stored"], 1)

    async def test_legacy_revision_stays_text_only_after_an_identical_pdf_is_reparsed(self):
        image = IMAGES["images/one.png"]
        key = self.app.objects.put(image)
        old = self.app.ingest(IngestRequest("demo", "paper", "v1", "Encoder layers.\n\n" + marker(key),
                              title="Attention paper", source_parser="mineru", source_object_key=sha256(self.raw).hexdigest()))
        old_gen = self.app.publish("demo", old.job_id, None).generation
        parsed = self.ingest(archive("Encoder layers.\n\n![](images/one.png)", {"images/one.png": image}))
        self.assertNotEqual(old.revision_id, parsed.revision_id)
        new_gen = self.app.publish("demo", parsed.job_id, old_gen).generation
        app = RagApplication(self.app.settings)
        prior, = (await app.query(QueryRequest(self.access, "Encoder layers", generation=old_gen))).evidence
        current, = (await app.query(QueryRequest(self.access, "Encoder layers", generation=new_gen))).evidence
        self.assertEqual((prior.text, prior.images), (current.text, current.images))
        with patch.object(app.objects, "get", wraps=app.objects.get) as get:
            with self.assertRaises(NotFoundError):
                app.get_evidence_image(self.access, old_gen, prior.evidence_id, key)
            self.assertNotIn((key,), [call.args for call in get.call_args_list])
        self.assertEqual(app.get_evidence_image(self.access, new_gen, current.evidence_id, key), (image, "image/png"))

    async def test_pdf_manifest_only_includes_validated_archive_references(self):
        foreign = self.app.objects.put(IMAGES["images/two.png"])
        job = self.ingest(archive("Encoder layers.\n\n![](images/one.png)\n\n" + marker(foreign),
                                  {"images/one.png": IMAGES["images/one.png"]}))
        generation = self.app.publish("demo", job.job_id, None).generation
        item, = (await self.app.query(QueryRequest(self.access, "Encoder layers"))).evidence
        parsed, literal = item.images
        self.assertEqual(literal.key, foreign)
        with self.assertRaises(NotFoundError):
            self.app.get_evidence_image(self.access, generation, item.evidence_id, foreign)
        self.assertEqual(self.app.get_evidence_image(self.access, generation, item.evidence_id, parsed.key),
                         (IMAGES["images/one.png"], "image/png"))

    async def test_a_bound_figure_is_not_bound_to_another_document_or_tenant(self):
        job = self.ingest(archive("Encoder.\n\n![](images/one.png)", {"images/one.png": IMAGES["images/one.png"]}))
        source_gen = self.app.publish("demo", job.job_id, None).generation
        source, = (await self.app.query(QueryRequest(self.access, "Encoder"))).evidence
        image, = source.images
        for tenant, document, previous in (("demo", "notes", source_gen), ("second", "paper", None)):
            with self.subTest(tenant=tenant, document=document):
                job = self.app.ingest(IngestRequest(tenant, document, "v1", "Appendix.\n\n" + marker(image.key)))
                generation = self.app.publish(tenant, job.job_id, previous).generation
                access = AccessContext(tenant, "reader")
                item, = (await self.app.query(QueryRequest(access, "Appendix"))).evidence
                self.assertEqual(item.images, (image,))
                with self.assertRaises(NotFoundError):
                    self.app.get_evidence_image(access, generation, item.evidence_id, image.key)

    async def test_the_same_pdf_ingested_again_is_a_duplicate_with_the_same_keys(self):
        raw_archive = archive("![](images/one.png)\n\nbody", {"images/one.png": IMAGES["images/one.png"]})
        first, second = self.ingest(raw_archive), self.ingest(raw_archive)
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.revision_id, second.revision_id)

    async def test_a_local_parse_without_figures_still_reports_zero(self):
        parsed = ParsedPdf("# Plain\n\nText only.", 1, "local", .01, {"source_sha256": sha256(self.raw).hexdigest()})
        with patch("adaptive_rag.pdf.parse_pdf", return_value=parsed):
            result = self.app.ingest_pdf(self.pdf, tenant_id="demo", document_id="plain", version="v1")
        self.assertEqual(result.figures, {"referenced": 0, "stored": 0, "skipped": 0})


class EvidenceImageHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(data_dir=Path(directory.name), browser_tenant="company", admin_password=ADMIN_PASSWORD)
        self.api = create_api(self.settings)
        self.app = self.api.state.application
        self.admin = self.client()
        self.assertEqual(self.admin.post("/auth/login", json={"username": "ragadmin", "password": ADMIN_PASSWORD}).status_code, 200)
        self.generation = None
        self.red, self.blue = picture((200, 30, 30)), picture((30, 30, 200), "JPEG")
        self.red_key, self.blue_key = self.app.objects.put(self.red), self.app.objects.put(self.blue)

    def client(self):
        client = TestClient(self.api, raise_server_exceptions=False)
        self.addCleanup(client.close)
        return client

    def reader(self, name, domains):
        response = self.admin.post("/admin/users", json={"username": name, "password": "test-password", "data_domains": domains})
        self.assertEqual(response.status_code, 201, response.text)
        client = self.client()
        self.assertEqual(client.post("/auth/login", json={"username": name, "password": "test-password"}).status_code, 200)
        return client

    def publish(self, document_id, body, domains=()):
        response = self.admin.post("/documents", json={"document_id": document_id, "version": "v1", "title": document_id,
                                                       "content": body, "data_domains": list(domains)})
        self.assertEqual(response.status_code, 201, response.text)
        response = self.admin.post("/publish", json={"job_id": response.json()["job_id"], "expected_generation": self.generation})
        self.assertEqual(response.status_code, 200, response.text)
        self.generation = response.json()["generation"]

    def evidence(self, client, question):
        response = client.post("/query", json={"question": question, "top_k": 5})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["evidence"]

    def image_url(self, item, key):
        return f"/evidence/{self.generation}/{item['evidence_id']}/images/{key}"

    def publish_pdf(self, document_id, intro, image, caption, page, domains=()):
        path = self.settings.data_dir / "fixture.pdf"
        path.write_bytes(pdf_bytes())
        result = archive(intro + "\n\n![](images/chart.png)", {"images/chart.png": image}, [
            {"type": "image", "img_path": "images/chart.png", "image_caption": [caption], "page_idx": page - 1}])
        job = ingest_archive(self.app, path, result, tenant_id="company", document_id=document_id,
                             version="v1", title=document_id, data_domains=tuple(domains))
        self.generation = self.app.publish("company", job.job_id, self.generation).generation

    def test_the_figure_is_listed_on_the_evidence_and_served_with_its_own_media_type(self):
        self.publish_pdf("revenue", "# Revenue\n\nQuarterly revenue.", self.red, "Figure 1: Revenue by quarter", 7)
        self.publish_pdf("scores", "# Scores\n\nHead scores.", self.blue, "Figure 2: Scores by head", 9)
        item, = self.evidence(self.admin, "Revenue by quarter")
        self.assertEqual(item["images"], [{"key": self.red_key, "caption": "Figure 1: Revenue by quarter", "page": 7}])
        shown = self.admin.get(f"/evidence/{self.generation}/{item['evidence_id']}").json()
        self.assertEqual(shown["images"], item["images"])
        response = self.admin.get(self.image_url(item, self.red_key))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, self.red)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["content-disposition"], "inline")
        self.assertIn("no-store", response.headers["cache-control"])
        other, = self.evidence(self.admin, "Scores by head")
        jpeg = self.admin.get(self.image_url(other, self.blue_key))
        self.assertEqual((jpeg.status_code, jpeg.headers["content-type"], jpeg.content), (200, "image/jpeg", self.blue))

    def test_a_key_opens_nothing_unless_that_evidence_shows_it(self):
        self.publish_pdf("revenue", "# Revenue\n\nQuarterly revenue.", self.red, "Figure 1: Revenue by quarter", 7)
        self.publish_pdf("scores", "# Scores\n\nHead scores.", self.blue, "Figure 2: Scores by head", 9)
        item, = self.evidence(self.admin, "Revenue by quarter")
        self.assertEqual(self.admin.get(self.image_url(item, self.blue_key)).status_code, 404)
        library = self.app.objects.put(picture((1, 2, 3)))  # stored, but no document shows it
        self.assertEqual(self.admin.get(self.image_url(item, library)).status_code, 404)

    def test_malformed_keys_and_identifiers_are_refused(self):
        self.publish("revenue", f"# Revenue\n\nQuarterly revenue.\n\n{marker(self.red_key, 'Figure 1', 7)}")
        item, = self.evidence(self.admin, "Quarterly revenue")
        for key in ("A" * 64, "a" * 63, "a" * 65, "x" * 64, "not-a-key"):
            with self.subTest(key=key):
                self.assertEqual(self.admin.get(self.image_url(item, key)).status_code, 400)
        self.assertEqual(self.admin.get(f"/evidence/{self.generation}/{item['evidence_id']}/images/..%2F..%2Fx").status_code, 404)
        self.assertEqual(self.admin.get(f"/evidence/{self.generation}/not-an-evidence/images/{self.red_key}").status_code, 400)
        self.assertEqual(self.admin.get(f"/evidence/nothing/{item['evidence_id']}/images/{self.red_key}").status_code, 404)

    def test_signed_out_callers_and_other_data_domains_get_nothing(self):
        operator, finance = self.reader("operator", ["operations"]), self.reader("finance", ["finance"])
        self.publish_pdf("budget", "# Budget\n\nBudget chart.", self.blue, "Figure 3: Budget split", 2, ["finance"])
        item, = self.evidence(self.admin, "Budget split")
        url = self.image_url(item, self.blue_key)
        self.assertEqual(self.client().get(url).status_code, 401)
        self.assertEqual(operator.get(url).status_code, 404)
        self.assertEqual(finance.get(url).status_code, 200)
        self.assertEqual(self.admin.get(url).status_code, 200)
        self.assertEqual(self.evidence(operator, "Budget split"), [])
        # Revoking the grant closes the picture for an open session too.
        self.assertEqual(self.admin.patch("/admin/users/finance/domains", json={"data_domains": ["operations"]}).status_code, 200)
        self.assertEqual(finance.get(url).status_code, 404)

    def test_a_marker_pointing_at_nothing_or_at_a_non_image_is_not_served(self):
        html = self.app.objects.put(b"<html><script>alert(1)</script></html>")
        self.publish("gone", f"# Gone\n\nDangling reference.\n\n{marker('d' * 64, 'Figure 1: Missing', 1)}")
        self.publish("hostile", f"# Hostile\n\nWebpage payload.\n\n{marker(html, 'Figure 2: Web page', 1)}")
        missing, = self.evidence(self.admin, "Dangling reference")
        self.assertEqual(self.admin.get(self.image_url(missing, "d" * 64)).status_code, 404)
        hostile, = self.evidence(self.admin, "Webpage payload")
        response = self.admin.get(self.image_url(hostile, html))
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("text/html", response.headers["content-type"])
        self.assertNotIn(b"alert(1)", response.content)

    def test_only_reading_is_allowed(self):
        self.publish("revenue", f"# Revenue\n\nQuarterly revenue.\n\n{marker(self.red_key, 'Figure 1', 7)}")
        item, = self.evidence(self.admin, "Quarterly revenue")
        for method in ("post", "put", "delete", "patch"):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.admin, method)(self.image_url(item, self.red_key)).status_code, 405)

    def test_the_application_refuses_unknown_snapshots_and_malformed_keys(self):
        with self.assertRaises(NotFoundError):
            self.app.get_evidence_image(AccessContext("company", "reader"), "g1", "p.1.abcd", KEY)
        with self.assertRaises(ValidationError):
            self.app.get_evidence_image(AccessContext("company", "reader"), "g1", "p.1.abcd", "short")


if __name__ == "__main__":
    unittest.main()
