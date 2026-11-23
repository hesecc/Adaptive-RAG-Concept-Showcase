import json
from pathlib import Path
import tempfile
import unittest

from adaptive_rag.domain import ValidationError
from adaptive_rag.evaluation import latency_summary
from adaptive_rag.evaluation_reports import ReportStore, summarize


def retrieval_report(tenant="alpha", **overrides):
    return {
        "created_at": "2026-11-17T08:00:00+00:00",
        "parameters": {"tenant_id": tenant, "top_k": 5, "data_domains": None, "fixture_path": "C:\\data\\eval.jsonl"},
        "model_calls": 0, "case_count": 3, "positive_case_count": 2,
        "metrics": {"hit_at_k": 1.0, "recall_at_k": 0.5, "mrr_at_k": 1.0, "ndcg_at_k": 0.9},
        "latency_ms": {"count": 3, "p50": 4.0, "p95": 9.0, "max": 9.0},
        "cases": [{"id": "问题一", "question": "保修期限是多久？"}],
        **overrides,
    }


def permission_report(tenant="alpha", **overrides):
    return {
        "created_at": "2026-11-17T08:05:00+00:00", "parameters": {"tenant_id": tenant}, "model_calls": 0,
        "summary": {"cells": 90, "ok": 88, "leaks": 1, "missing": 1, "broad_checks": 9, "broad_failures": 1, "passed": False},
        "cells": [], **overrides,
    }


class ReportStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.store = ReportStore(self.data)

    def plant(self, name, content, binary=False):
        path = self.data / "evaluations" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content) if binary else path.write_text(content, encoding="utf-8")

    def test_save_assigns_an_id_and_round_trips_unicode_without_leftovers(self):
        saved = self.store.save(retrieval_report(), "retrieval")
        self.assertRegex(saved["report_id"], r"^[0-9]{8}T[0-9]{6}Z-retrieval-[0-9a-f]{8}$")
        self.assertEqual(saved["kind"], "retrieval")
        self.assertEqual(self.store.get("alpha", saved["report_id"]), saved)
        raw = (self.data / "evaluations" / f"{saved['report_id']}.json").read_text(encoding="utf-8")
        self.assertIn("保修期限是多久？", raw)
        self.assertEqual([path.name for path in (self.data / "evaluations").iterdir()], [f"{saved['report_id']}.json"])

    def test_save_does_not_change_the_report_it_was_given_and_ids_are_unique(self):
        report = retrieval_report()
        first = self.store.save(report, "retrieval")
        second = self.store.save(report, "retrieval")
        self.assertNotIn("report_id", report)
        self.assertNotEqual(first["report_id"], second["report_id"])

    def test_save_rejects_unknown_kinds_and_reports_without_a_tenant(self):
        with self.assertRaises(ValidationError):
            self.store.save(retrieval_report(), "other")
        for parameters in ({}, {"tenant_id": ""}, {"tenant_id": 3}):
            with self.assertRaises(ValidationError):
                self.store.save({"parameters": parameters}, "retrieval")
        with self.assertRaises(ValidationError):
            self.store.save({}, "retrieval")
        self.assertFalse((self.data / "evaluations").exists())

    def test_a_report_that_cannot_be_encoded_leaves_no_file_behind(self):
        with self.assertRaises(TypeError):
            self.store.save(retrieval_report(extra=object()), "retrieval")
        self.assertEqual(list((self.data / "evaluations").iterdir()), [])

    def test_list_is_newest_first_and_limited(self):
        ids = ["20260101T000000Z-retrieval-aaaaaaaa", "20260301T000000Z-permissions-bbbbbbbb",
               "20260201T000000Z-retrieval-cccccccc", "20260301T000000Z-retrieval-00000000"]
        for report_id in ids:
            kind = report_id.split("-")[1]
            report = retrieval_report() if kind == "retrieval" else permission_report()
            self.plant(f"{report_id}.json", json.dumps({**report, "report_id": report_id, "kind": kind}))
        newest_first = sorted(ids, reverse=True)
        self.assertEqual([item["report_id"] for item in self.store.list("alpha")], newest_first)
        self.assertEqual([item["report_id"] for item in self.store.list("alpha", limit=2)], newest_first[:2])
        self.assertEqual(self.store.list("alpha", limit=0), [])

    def test_list_summaries_use_the_stored_id_and_kind(self):
        first = self.store.save(retrieval_report(), "retrieval")
        second = self.store.save(permission_report(), "permissions")
        listed = self.store.list("alpha")
        self.assertEqual({item["report_id"] for item in listed}, {first["report_id"], second["report_id"]})
        by_kind = {item["kind"]: item for item in listed}
        self.assertEqual(by_kind["retrieval"]["fixture"], "eval.jsonl")
        self.assertEqual(by_kind["retrieval"]["metrics"]["hit_at_k"], 1.0)
        self.assertEqual(by_kind["retrieval"]["latency_ms"]["p95"], 9.0)
        self.assertEqual((by_kind["permissions"]["leaks"], by_kind["permissions"]["passed"]), (1, False))
        self.assertNotIn("cases", by_kind["retrieval"])
        # "cells" is the count; the per-cell detail stays out of the list.
        self.assertEqual(by_kind["permissions"]["cells"], 90)
        self.assertNotIn("broad", by_kind["permissions"])

    def test_reports_belong_to_their_tenant(self):
        mine = self.store.save(retrieval_report("alpha"), "retrieval")
        theirs = self.store.save(retrieval_report("beta"), "retrieval")
        self.assertEqual([item["report_id"] for item in self.store.list("alpha")], [mine["report_id"]])
        self.assertEqual([item["report_id"] for item in self.store.list("beta")], [theirs["report_id"]])
        self.assertIsNone(self.store.get("alpha", theirs["report_id"]))
        self.assertIsNone(self.store.get("gamma", mine["report_id"]))
        self.assertEqual(self.store.list("gamma"), [])

    def test_get_accepts_only_the_exact_id_shape(self):
        saved = self.store.save(retrieval_report(), "retrieval")
        outside = self.data / "secret.json"
        outside.write_text(json.dumps(retrieval_report()), encoding="utf-8")
        for bad in ("../secret", "..\\secret", "../../secret", "secret", "", saved["report_id"] + "\n",
                    saved["report_id"].upper(), saved["report_id"] + ".json", " " + saved["report_id"],
                    "٢٠٢٦١١١٧T082027Z-retrieval-aaaaaaaa", saved["report_id"].replace("retrieval", "other"), None, 7):
            self.assertIsNone(self.store.get("alpha", bad), repr(bad))
        self.assertIsNone(self.store.get("alpha", "20260101T000000Z-retrieval-aaaaaaaa"))

    def test_the_file_name_wins_over_an_id_written_inside_the_file(self):
        original = self.store.save(retrieval_report(), "retrieval")
        copy_id = "20990201T000000Z-retrieval-cccccccc"
        self.plant(f"{copy_id}.json", json.dumps(original))  # a renamed copy still says the old ID inside
        self.assertEqual(self.store.get("alpha", copy_id)["report_id"], copy_id)
        self.assertEqual(self.store.get("alpha", original["report_id"]), original)
        self.assertEqual([item["report_id"] for item in self.store.list("alpha")], [copy_id, original["report_id"]])

    def test_damaged_or_foreign_files_are_skipped(self):
        good = self.store.save(retrieval_report(), "retrieval")
        self.plant("20990101T000000Z-retrieval-dddddddd.json", "{not json")
        self.plant("20990102T000000Z-retrieval-eeeeeeee.json", b"\xff\xfe\x00", binary=True)
        self.plant("20990103T000000Z-retrieval-ffffffff.json", "[1, 2]")
        self.plant("20990104T000000Z-retrieval-11111111.json", json.dumps({"parameters": "oops"}))
        self.plant("notes.json", json.dumps(retrieval_report()))
        self.plant(".saving-x.tmp", "partial")
        self.assertEqual([item["report_id"] for item in self.store.list("alpha")], [good["report_id"]])
        self.assertIsNone(self.store.get("alpha", "20990101T000000Z-retrieval-dddddddd"))
        self.assertIsNone(self.store.get("alpha", "20990103T000000Z-retrieval-ffffffff"))
        self.assertIsNone(self.store.get("alpha", "20990104T000000Z-retrieval-11111111"))

    def test_an_empty_or_missing_directory_lists_nothing(self):
        self.assertEqual(self.store.list("alpha"), [])
        self.assertIsNone(self.store.get("alpha", "20260101T000000Z-retrieval-aaaaaaaa"))

    def test_scratch_directory_is_removed_and_ignored_by_the_listing(self):
        with self.store.scratch() as work_dir:
            (Path(work_dir) / "data.sqlite3").write_bytes(b"x")
            self.assertEqual(Path(work_dir).parent, self.data / "evaluations")
            self.assertEqual(self.store.list("alpha"), [])
        self.assertEqual(list((self.data / "evaluations").iterdir()), [])

    def test_summarize_tolerates_a_sparse_report(self):
        self.assertEqual(summarize({"kind": "retrieval"})["fixture"], None)
        self.assertEqual(summarize({"kind": "permissions"})["passed"], None)
        self.assertEqual(summarize({})["model_calls"], 0)


class LatencySummaryTests(unittest.TestCase):
    def test_nearest_rank_percentiles(self):
        self.assertIsNone(latency_summary([]))
        self.assertEqual(latency_summary([7.5]), {"count": 1, "p50": 7.5, "p95": 7.5, "max": 7.5})
        self.assertEqual(latency_summary([5, 1, 3, 2, 4]), {"count": 5, "p50": 3, "p95": 5, "max": 5})
        hundred = latency_summary(list(range(100, 0, -1)))
        self.assertEqual((hundred["p50"], hundred["p95"], hundred["max"]), (50, 95, 100))
        twenty = latency_summary(list(range(1, 21)))
        self.assertEqual((twenty["p50"], twenty["p95"]), (10, 19))
        self.assertEqual(latency_summary([1, 2])["p50"], 1)


if __name__ == "__main__":
    unittest.main()
