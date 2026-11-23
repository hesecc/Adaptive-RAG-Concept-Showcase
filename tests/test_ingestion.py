"""Parser, chunker and ingestion contract tests without model or network access."""

from dataclasses import replace
from hashlib import sha256
import re
import unittest

from adaptive_rag.domain import IngestRequest, IngestResult, Section, ValidationError, stable_id
from adaptive_rag.ingestion import IngestionService, ParentChildChunker, StructuredParser, TextCleaner


class MemoryObjects:
    def __init__(self):
        self.values = {}

    def put(self, content):
        key = sha256(content).hexdigest()
        self.values[key] = content
        return key


class StagingStore:
    def __init__(self):
        self.documents = {}

    def stage(self, document):
        duplicate = document.revision_id in self.documents
        self.documents[document.revision_id] = document
        return IngestResult(
            stable_id("job", document.revision_id), document.revision_id,
            document.request.document_id, document.request.version, "staged",
            len(document.parents), len(document.children), duplicate,
        )


def request(content="不可放入洗碗机。质保1年，500ml。", **kwargs):
    return IngestRequest(tenant_id="demo", document_id="manual", version="1", content=content, **kwargs)


class ParserTests(unittest.TestCase):
    def test_plain_preserves_unicode_negation_numbers(self):
        text = "Do not machine wash!\n不可机洗，质保1年。500ml 😀\n型号A01-500"
        section, = StructuredParser().parse(request(text, media_type="text/plain"))
        self.assertEqual(section.text, text)
        self.assertEqual(section.locator, "lines:1-3")

    def test_markdown_heading_tree_and_table(self):
        content = "前言\n# 商品\n## 清洁\n不可机洗。\n\n| 型号 | 容量 |\n| A01-500 | 500ml |\n## 售后\n质保1年，人为损坏除外。"
        sections = StructuredParser().parse(request(content))
        self.assertEqual([s.heading_path for s in sections], [(), ("商品", "清洁"), ("商品", "售后")])
        self.assertIn("| A01-500 | 500ml |", sections[1].text)
        self.assertEqual(sections[2].text, "质保1年，人为损坏除外。")

    def test_markdown_fenced_hash_is_not_heading(self):
        fence = chr(96) * 3
        content = f"# Actual\n{fence}python\n# Not a heading\nprint(1)\n{fence}\nStill same section"
        sections = StructuredParser().parse(request(content))
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].heading_path, ("Actual",))
        self.assertIn("# Not a heading", sections[0].text)

    def test_markdown_heading_hash_in_title_is_preserved(self):
        sections = StructuredParser().parse(request("# C#\nCode docs\n## Release ##\nVersion docs"))
        self.assertEqual([s.heading_path for s in sections], [("C#",), ("C#", "Release")])

    def test_html_is_inert_with_heading_lists_and_table(self):
        text = """<html><head><title>Do not index head</title></head><body>
        <h1>商品</h1><h2>保养</h2><p>不可机洗 &amp; 不可冷冻。</p>
        <script>fetch('https://invalid.example'); secret=1;</script>
        <style>.secret {display:block}</style><svg><text>bad svg</text></svg>
        <noscript>bad noscript</noscript><ul><li>500ml</li><li>质保1年😀</li></ul>
        <table><tr><th>型号</th><th>容量</th></tr><tr><td>A01-500</td><td>500ml</td></tr></table>
        <a href="https://invalid.example">可读链接文字</a></body></html>"""
        sections = StructuredParser().parse(request(text, media_type="text/html"))
        self.assertEqual(len(sections), 1)
        section = sections[0]
        self.assertEqual(section.heading_path, ("商品", "保养"))
        for expected in ("不可机洗 & 不可冷冻。", "500ml", "质保1年😀", "A01-500 | 500ml", "可读链接文字"):
            self.assertIn(expected, section.text)
        for blocked in ("fetch", "secret", "bad svg", "bad noscript", "Do not index head"):
            self.assertNotIn(blocked, section.text)

    def test_cleaner_conservative_and_drops_empty(self):
        source = (Section((" Clean  heading ",), "\ufeff 不可机洗。\r\n\r\n\r\n500ml\xa0😀  \r\n", "lines:1-5"), Section((), "  \n ", "lines:6-7"))
        cleaned = TextCleaner().clean(source)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0].text, "不可机洗。\n\n500ml 😀")
        self.assertEqual(cleaned[0].heading_path, ("Clean heading",))
        self.assertEqual(cleaned[0].locator, "lines:1-5")


class ChunkerTests(unittest.TestCase):
    def test_chunks_bounded_reproducible_and_citable_without_prefix(self):
        req = request("不可机洗。500ml 😀\n" * 70, title="保温杯", scope="sku", sku_ids=("A01-500",))
        sections = TextCleaner().clean(StructuredParser().parse(req))
        chunker = ParentChildChunker(parent_chars=100, child_chars=35, overlap_chars=8)
        parents, children = chunker.chunk(req, "rev", sections)
        self.assertEqual((parents, children), chunker.chunk(req, "rev", sections))
        self.assertTrue(parents and children)
        self.assertTrue(all(0 < len(p.text) <= 100 for p in parents))
        self.assertTrue(all(0 < len(c.text) <= 35 for c in children))
        self.assertEqual(len({c.chunk_id for c in children}), len(children))
        by_id = {p.parent_id: p for p in parents}
        for parent in parents:
            self.assertEqual(parent.content_hash, sha256(parent.text.encode()).hexdigest())
        for child in children:
            self.assertIn(child.text, by_id[child.parent_id].text)
            self.assertEqual(child.content_hash, sha256(child.text.encode()).hexdigest())
            self.assertNotIn("保温杯", child.text)
            self.assertTrue(child.retrieval_text.startswith("保温杯\n"))
            self.assertIn(";chars:", child.locator)
            self.assertEqual((child.scope, child.sku_ids), ("sku", ("A01-500",)))

    def test_extreme_overlap_always_advances_and_covers_nonspace(self):
        req = request("1234567890😀ABCDEF")
        section = Section(("Title",), req.content, "lines:1-1")
        _, children = ParentChildChunker(20, 5, 4).chunk(req, "revision", (section,))
        self.assertLessEqual(len(children), len(req.content))
        coverage = set()
        for child in children:
            start, end = map(int, re.search(r"chars:(\d+)-(\d+)$", child.locator).groups())
            coverage.update(range(start, end))
            self.assertEqual(child.text, req.content[start:end])
        self.assertEqual(coverage, set(range(len(req.content))))

    def test_table_rows_preserved_when_they_fit(self):
        req = request("# 表格\n| 型号 | 容量 |\n| A01 | 500ml |\n| A02 | 750ml |\n不可机洗。")
        sections = TextCleaner().clean(StructuredParser().parse(req))
        parents, children = ParentChildChunker(50, 25, 0).chunk(req, "rev", sections)
        self.assertTrue(any("| A01 | 500ml |" in child.text for child in children))
        self.assertTrue(any("| A02 | 750ml |" in child.text for child in children))
        self.assertTrue(any("不可机洗。" in parent.text for parent in parents))

    def test_configuration_validation(self):
        for values in ((0, 1, 0), (10, 20, 0), (10, 5, 5), (10, 5, -1), (True, 1, 0)):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ParentChildChunker(*values)

    def test_fitting_structures_are_kept_whole_even_across_preferred_breaks(self):
        structures = (
            "<table><tr><td>型号</td><td>A01</td></tr>\n<tr><td>容量</td><td>500ml</td></tr></table>",
            "$$\nAttention(Q,K,V) = softmax(QK^T / sqrt(d_k))V\n$$",
            "\\[\nE = mc^2\n\\]",
            "\\begin{align}\na &= b+c \\\\\nd &= e\n\\end{align}",
            "```python\nx = 1\nprint(x)\n```",
            "| 型号 | 容量 |\n| --- | --- |\n| A01 | 500ml |",
        )
        for structure in structures:
            with self.subTest(structure=structure):
                text = "前言。" * 12 + "\n" + structure + "\n" + "结尾。" * 10
                req = request(text)
                sections = TextCleaner().clean(StructuredParser().parse(req))
                parents, children = ParentChildChunker(180, 100, 15).chunk(req, "rev", sections)
                self.assertTrue(any(structure in parent.text for parent in parents))
                self.assertTrue(any(structure in child.text for child in children))
                for child in children:
                    self.assertLessEqual(len(child.text), 100)
                    if any(token in child.text for token in ("<table", "Attention(", "E =", "a &=", "x =", "| A01")):
                        self.assertIn(structure, child.text)
                        self.assertNotIn("structure_fragment", child.locator)

    def test_oversized_structures_split_bounded_citable_and_explicit_fragments(self):
        for structure in ("<table>" + "<tr><td>500ml</td></tr>\n" * 20 + "</table>",
                          "$$\n" + "x+y+z+" * 50 + "\n$$", "```\n" + "print(1)\n" * 50 + "```"):
            with self.subTest(structure=structure[:12]):
                req = request(structure)
                section = Section((), structure, "lines:1-10")
                parents, children = ParentChildChunker(120, 50, 5).chunk(req, "rev", (section,))
                self.assertTrue(all(len(item.text) <= 120 for item in parents))
                self.assertTrue(all(len(item.text) <= 50 for item in children))
                self.assertTrue(all("structure_fragment:" in item.locator for item in (*parents, *children)))
                coverage = set()
                for child in children:
                    start, end = map(int, re.search(r"chars:(\d+)-(\d+)$", child.locator).groups())
                    self.assertEqual(child.text, structure[start:end])
                    coverage.update(range(start, end))
                self.assertTrue({i for i, char in enumerate(structure) if not char.isspace()} <= coverage)

    def test_code_literal_markup_and_unclosed_elements_are_not_falsely_complete(self):
        content = "```html\n<table><tr><td>literal</td></tr></table>\n```\nOutside."
        req = request(content)
        parents, children = ParentChildChunker(120, 90, 0).chunk(req, "rev", (Section((), content, "lines:1-5"),))
        self.assertTrue(any("```html" in child.text and "```\nOutside" in child.text for child in children))
        for text in ("```\nunfinished", "<table><tr><td>unfinished"):
            _, children = ParentChildChunker(120, 90, 0).chunk(request(text), "rev", (Section((), text, "lines:1-2"),))
            self.assertIn("structure_fragment:", children[0].locator)


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.store = StagingStore()
        self.objects = MemoryObjects()

    def service(self, fingerprint="config-v1", **kwargs):
        return IngestionService(self.store, self.objects, kwargs.get("parser", StructuredParser()),
                                TextCleaner(), kwargs.get("chunker", ParentChildChunker()), fingerprint)

    def test_archive_hash_and_stage_without_publication(self):
        req = request("# 保养\n不可机洗。质保1年😀")
        result = self.service().ingest(req)
        prepared = self.store.documents[result.revision_id]
        self.assertEqual(prepared.content_hash, sha256(req.content.encode()).hexdigest())
        self.assertEqual(self.objects.values[prepared.raw_key], req.content.encode())
        self.assertEqual(result.state, "staged")
        self.assertTrue(result.child_count and result.parent_count)

    def test_repeated_ingestion_and_unordered_metadata_deduplicate(self):
        req = request(allowed_subjects=("user-b", "user-a"))
        first = self.service().ingest(req)
        second = self.service().ingest(replace(req, allowed_subjects=("user-a", "user-b")))
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(first.revision_id, second.revision_id)
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)

    def test_config_and_metadata_changes_same_source_version_rebuild(self):
        req = request()
        results = [self.service().ingest(req),
                   self.service("config-v2").ingest(req),
                   self.service(chunker=ParentChildChunker(1200, 200, 20)).ingest(req),
                   self.service().ingest(replace(req, title="Different title")),
                   self.service().ingest(replace(req, allowed_subjects=("private",)))]
        self.assertEqual(len({result.revision_id for result in results}), 5)

    def test_script_only_html_rejected_without_staging(self):
        with self.assertRaisesRegex(ValidationError, "no readable text"):
            self.service().ingest(request("<script>raise Exception()</script><style>hidden</style>", media_type="text/html"))
        self.assertFalse(self.store.documents)

    def test_parser_failure_never_stages(self):
        class BrokenParser:
            def parse(self, req):
                raise ValidationError("parser failed")
        with self.assertRaisesRegex(ValidationError, "parser failed"):
            self.service(parser=BrokenParser()).ingest(request())
        self.assertFalse(self.store.documents)

    def test_plugin_identity_violation_rejected(self):
        class BadChunker:
            def chunk(self, req, revision_id, sections):
                parents, children = ParentChildChunker().chunk(req, revision_id, sections)
                return (replace(parents[0], tenant_id="other"),), children
        with self.assertRaisesRegex(ValidationError, "identity"):
            self.service(chunker=BadChunker()).ingest(request())
        self.assertFalse(self.store.documents)

    def test_plugin_invented_parent_text_rejected(self):
        class BadChunker:
            def chunk(self, req, revision_id, sections):
                parents, children = ParentChildChunker().chunk(req, revision_id, sections)
                invented = "Invented content absent from the source."
                return (replace(parents[0], text=invented, content_hash=sha256(invented.encode()).hexdigest()),), children
        with self.assertRaisesRegex(ValidationError, "parsed section"):
            self.service(chunker=BadChunker()).ingest(request())
        self.assertFalse(self.store.documents)

    def test_plugin_invented_child_text_rejected(self):
        class BadChunker:
            def chunk(self, req, revision_id, sections):
                parents, children = ParentChildChunker().chunk(req, revision_id, sections)
                return parents, (replace(children[0], text="Invented content absent from the parent."),)
        with self.assertRaisesRegex(ValidationError, "referenced parent"):
            self.service(chunker=BadChunker()).ingest(request())
        self.assertFalse(self.store.documents)


if __name__ == "__main__":
    unittest.main()
