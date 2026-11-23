"""The page shells stay usable when one of their script or stylesheet requests fails."""

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest

STATIC = Path(__file__).resolve().parents[1] / "adaptive_rag" / "static"
PAGES = ("index.html", "images.html", "knowledge.html", "evaluation.html")


class Tags(HTMLParser):
    """Start tags in document order as (tag, attributes)."""

    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def with_id(self, identifier):
        found = [attrs for _, attrs in self.tags if attrs.get("id") == identifier]
        assert len(found) == 1, (identifier, found)
        return found[0]


def tags(page="index.html"):
    return Tags((STATIC / page).read_text(encoding="utf-8"))


class PageShellTests(unittest.TestCase):
    def test_the_login_form_stays_hidden_until_the_session_is_known(self):
        page = tags()
        # A signed-in user must not see the login form flash up, nor be left on it when one request fails.
        self.assertIn("hidden", page.with_id("login-page"))
        self.assertIn("hidden", page.with_id("app-shell"))
        self.assertNotIn("hidden", page.with_id("boot-page"))

    def test_a_script_that_never_loads_leaves_a_way_to_reload(self):
        page = tags()
        # A plain link works without JavaScript; app.css reveals it after a delay, and app.js right away on failure.
        self.assertEqual(page.with_id("boot-reload").get("href"), "/")
        self.assertNotIn("hidden", page.with_id("boot-actions"))

    def test_the_login_form_never_falls_back_to_a_get_request(self):
        # Submitted natively as GET, the form would put the password into the address bar and the history.
        self.assertEqual(tags().with_id("login-form").get("method"), "post")

    @unittest.skipUnless(shutil.which("node"), "Node.js required for conversation UI scenarios")
    def test_conversation_ui_scenarios(self):
        suite = Path(__file__).with_name("conversation_ui.test.cjs")
        run = subprocess.run([shutil.which("node"), "--test", str(suite)], capture_output=True, timeout=45)
        self.assertEqual(run.returncode, 0, (run.stdout + run.stderr).decode("utf-8", errors="replace"))

    def test_every_page_carries_the_same_four_navigation_tabs(self):
        # The image library once shipped with only two tabs, so the other two vanished until the user went back.
        expected = [("/", "检索工作台"), ("/images", "图片资料库"),
                    ("/knowledge", "文件中心"), ("/evaluation", "评测")]
        own = {"index.html": "/", "images.html": "/images", "knowledge.html": "/knowledge", "evaluation.html": "/evaluation"}
        for page in PAGES:
            text = (STATIC / page).read_text(encoding="utf-8")
            nav = re.search(r'<nav class="tabs"[^>]*>(.*?)</nav>', text, re.S)
            self.assertIsNotNone(nav, page)
            links = re.findall(r"<a\b([^>]*)>([^<]*)</a>", nav.group(1))
            self.assertEqual([(re.search(r'href="([^"]*)"', attrs).group(1), label.strip()) for attrs, label in links],
                             expected, page)
            current = [re.search(r'href="([^"]*)"', attrs).group(1) for attrs, _ in links if "aria-current" in attrs]
            self.assertEqual(current, [own[page]], page)

    def test_members_get_file_center_but_not_evaluation_navigation(self):
        for page, script in (("index.html", "app.js"), ("images.html", "images.js")):
            shell = tags(page)
            source = (STATIC / script).read_text(encoding="utf-8")
            for identifier in ("knowledge-open", "evaluation-open"):
                self.assertIn("hidden", shell.with_id(identifier), (page, identifier))
            self.assertIn('knowledge-open").hidden = false', source)
            self.assertIn('evaluation-open").hidden = !session.admin', source)
        for page in ("knowledge.html", "evaluation.html"):
            self.assertIn("hidden", tags(page).with_id("evaluation-open"))

    def test_brand_marks_have_a_size_without_the_stylesheet(self):
        # An inline SVG that only has a viewBox grows to the width of the page when app.css is missing.
        for page in PAGES:
            marks = [attrs for tag, attrs in tags(page).tags if tag == "svg" and "brand-mark" in attrs.get("class", "")]
            self.assertTrue(marks, page)
            for attrs in marks:
                self.assertTrue(attrs.get("width") and attrs.get("height"), (page, attrs))


if __name__ == "__main__":
    unittest.main()
