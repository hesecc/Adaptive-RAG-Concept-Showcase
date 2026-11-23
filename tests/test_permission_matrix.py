import asyncio
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adaptive_rag import permission_matrix
from adaptive_rag.cli import main
from adaptive_rag.evaluation_reports import ReportStore
from adaptive_rag.app import RagApplication
from adaptive_rag.config import Settings
from adaptive_rag.domain import ValidationError
from adaptive_rag.permission_matrix import DOCUMENTS, IDENTITIES, run_permission_matrix, seed_corpus
from adaptive_rag.storage import SQLiteKnowledgeStore


def documented_rule(document, identity) -> bool:
    """docs/data-permissions.md restated for the cross-check; deliberately not the engine's own function."""
    if document.revoked:
        return False
    if document.allowed_subjects and identity.subject_id not in document.allowed_subjects:
        return False
    if identity.data_domains is None:
        return True
    if not document.data_domains:
        return False
    return "public" in document.data_domains or bool(set(document.data_domains) & set(identity.data_domains))


@contextmanager
def only(names):
    """Run the matrix for these identities only. One identity costs about a second of queries, so a
    mutation test that needs two of them should not pay for all nine."""
    chosen = [item for item in IDENTITIES if item.identity in names]
    assert len(chosen) == len(names), names
    with patch.object(permission_matrix, "IDENTITIES", chosen):
        yield


def run_matrix(tenant="alpha", names=None):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work_dir:
        if names is None:
            return asyncio.run(run_permission_matrix(tenant, work_dir))
        with only(names):
            return asyncio.run(run_permission_matrix(tenant, work_dir))


def cell(report, identity, document_id):
    return next(item for item in report["cells"] if item["identity"] == identity and item["document_id"] == document_id)


class ExpectationTableTests(unittest.TestCase):
    def test_hand_written_table_agrees_with_the_documented_rules(self):
        ids = {document.document_id for document in DOCUMENTS}
        self.assertEqual(len(ids), len(DOCUMENTS))
        for identity in IDENTITIES:
            self.assertLessEqual(identity.sees, ids, identity.identity)
            for document in DOCUMENTS:
                self.assertEqual(document.document_id in identity.sees, documented_rule(document, identity),
                                 (identity.identity, document.document_id))

    def test_hand_picked_cells(self):
        sees = {identity.identity: identity.sees for identity in IDENTITIES}
        self.assertEqual(sees["public-only"], {"public-handbook"})
        self.assertNotIn("finance-budget", sees["operations"])
        self.assertIn("ops-finance-joint", sees["operations"])
        self.assertIn("ops-finance-joint", sees["finance"])
        self.assertIn("unclassified-memo", sees["admin"])
        self.assertTrue(all("unclassified-memo" not in value for name, value in sees.items() if name != "admin"))
        self.assertTrue(all("revoked-ops-notice" not in value for value in sees.values()))
        self.assertEqual([name for name, value in sees.items() if "acl-public-note" in value], ["reviewer"])

    def test_every_marker_is_unique_and_the_corpus_covers_each_case(self):
        markers = [document.marker for document in DOCUMENTS]
        self.assertEqual(len(markers), len(set(markers)))
        self.assertTrue(any(not document.data_domains for document in DOCUMENTS))
        self.assertTrue(any(document.revoked for document in DOCUMENTS))
        self.assertTrue(any(document.allowed_subjects for document in DOCUMENTS))
        self.assertTrue(any(len(document.data_domains) > 1 for document in DOCUMENTS))
        self.assertTrue(any(identity.data_domains is None for identity in IDENTITIES))


class MatrixRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_matrix()

    def test_the_real_engine_passes_with_no_model_calls(self):
        report = self.report
        self.assertEqual(report["summary"], {
            "cells": len(IDENTITIES) * len(DOCUMENTS), "ok": len(IDENTITIES) * len(DOCUMENTS),
            "leaks": 0, "missing": 0, "broad_checks": len(IDENTITIES), "broad_failures": 0, "passed": True,
        })
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["parameters"]["tenant_id"], "alpha")
        self.assertEqual(sum(item["expected"] for item in report["cells"]), sum(len(item.sees) for item in IDENTITIES))
        self.assertEqual([item["identity"] for item in report["identities"]], [item.identity for item in IDENTITIES])

    def test_cells_record_what_was_expected_and_what_was_seen(self):
        self.assertEqual(cell(self.report, "operations", "ops-finance-joint"),
                         {"identity": "operations", "document_id": "ops-finance-joint",
                          "expected": True, "visible": True, "result": "ok"})
        self.assertEqual(cell(self.report, "public-only", "finance-budget"),
                         {"identity": "public-only", "document_id": "finance-budget",
                          "expected": False, "visible": False, "result": "ok"})

    def test_matrix_catches_an_engine_that_ignores_data_classifications(self):
        with patch("adaptive_rag.storage.domains_allowed", lambda resource, access: True):
            report = run_matrix(names=("public-only", "operations"))
        self.assertFalse(report["summary"]["passed"])
        self.assertEqual(cell(report, "public-only", "finance-budget")["result"], "leak")
        self.assertEqual(cell(report, "operations", "hr-policy")["result"], "leak")
        self.assertEqual(cell(report, "operations", "unclassified-memo")["result"], "leak")
        self.assertGreater(report["summary"]["leaks"], 0)
        self.assertEqual(report["summary"]["missing"], 0)
        self.assertGreater(report["summary"]["broad_failures"], 0)

    def test_matrix_catches_an_engine_that_ignores_revocation_or_the_acl(self):
        original = SQLiteKnowledgeStore._allowed

        def without_revocation(row, access):
            return original({**dict(row), "revoked": 0}, access)

        def without_acl(row, access):
            return original({**dict(row), "allowed_subjects": "[]"}, access)

        with patch.object(SQLiteKnowledgeStore, "_allowed", staticmethod(without_revocation)):
            revoked = run_matrix(names=("operations", "admin"))
        self.assertEqual(cell(revoked, "operations", "revoked-ops-notice")["result"], "leak")
        self.assertEqual(cell(revoked, "admin", "revoked-ops-notice")["result"], "leak")
        self.assertEqual(revoked["summary"]["missing"], 0)
        with patch.object(SQLiteKnowledgeStore, "_allowed", staticmethod(without_acl)):
            acl = run_matrix(names=("public-only", "reviewer"))
        self.assertEqual(cell(acl, "public-only", "acl-public-note")["result"], "leak")
        self.assertEqual(cell(acl, "reviewer", "acl-public-note")["result"], "ok")
        self.assertFalse(acl["summary"]["passed"])

    def test_matrix_catches_an_engine_that_hides_what_it_should_show(self):
        with patch("adaptive_rag.storage.domains_allowed", lambda resource, access: access is None):
            report = run_matrix(names=("public-only", "admin"))
        self.assertEqual(cell(report, "public-only", "public-handbook")["result"], "missing")
        self.assertEqual(cell(report, "admin", "finance-budget")["result"], "ok")
        self.assertGreater(report["summary"]["missing"], 0)
        self.assertEqual(report["summary"]["leaks"], 0)
        self.assertFalse(report["summary"]["passed"])

    def test_seeding_is_verified_so_a_broken_setup_cannot_look_like_a_pass(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work_dir:
            application = RagApplication(Settings(data_dir=Path(work_dir)))
            with patch.object(RagApplication, "set_access", lambda *args, **kwargs: None):
                with self.assertRaisesRegex(ValidationError, "revoked-ops-notice"):
                    seed_corpus(application, "alpha")


class MatrixCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name) / "data"

    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--data-dir", str(self.data), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_command_saves_the_report_and_exits_zero_when_every_cell_matches(self):
        copy = Path(self.directory.name) / "copy.json"
        # The matrix must build its own model-free application, never the one the environment
        # configures, so building that one here is an error. The full nine-identity run is
        # covered above; this test is about saving, the exit code, and what is left on disk.
        with only(("public-only", "operations")), \
                patch("adaptive_rag.cli.RagApplication", side_effect=AssertionError("must not be built")):
            code, out, err = self.invoke("eval-permissions", "--tenant", "alpha", "--output", str(copy))
        self.assertEqual((code, err), (0, ""), err)
        result = json.loads(out)
        self.assertTrue(result["passed"])
        self.assertEqual(result["failures"], [])
        saved = ReportStore(self.data).get("alpha", result["report_id"])
        self.assertEqual(saved["kind"], "permissions")
        self.assertEqual(len(saved["cells"]), 2 * len(DOCUMENTS))
        self.assertEqual(json.loads(copy.read_text(encoding="utf-8"))["report_id"], result["report_id"])
        # The throwaway knowledge base is gone and the real data directory holds only reports.
        self.assertEqual(sorted(path.name for path in self.data.iterdir()), ["evaluations"])
        self.assertEqual([path.suffix for path in (self.data / "evaluations").iterdir()], [".json"])

    def test_command_exits_three_and_names_the_failing_cells_on_a_leak(self):
        with only(("public-only", "operations")), \
                patch("adaptive_rag.storage.domains_allowed", lambda resource, access: True):
            code, out, err = self.invoke("eval-permissions", "--tenant", "alpha")
        self.assertEqual((code, err), (3, ""), err)
        result = json.loads(out)
        self.assertFalse(result["passed"])
        self.assertTrue(result["failures"])
        self.assertTrue(all(item["result"] in ("leak", "broad_mismatch") for item in result["failures"]))
        # A failing run is still saved, so the page can show what went wrong.
        self.assertIsNotNone(ReportStore(self.data).get("alpha", result["report_id"]))


if __name__ == "__main__":
    unittest.main()
