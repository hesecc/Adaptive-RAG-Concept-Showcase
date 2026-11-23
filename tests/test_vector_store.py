"""Vector-index contract exercised against persistent SQLite, without models."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from adaptive_rag.domain import ConflictError, ValidationError
from adaptive_rag.vector_store import SQLiteVectorIndex, VectorMatch, VectorRecord


class VectorIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.index = SQLiteVectorIndex(self.root)
        self.record = VectorRecord("caption-1", "公司甲", "图像一", "v1", "text-model-v1", (1.0, 0.0))

    def test_upsert_search_restart_namespace_and_delete(self):
        other = replace(self.record, record_id="caption-2", asset_id="图像二", vector=(0.0, 1.0))
        tenant = replace(self.record, tenant_id="公司乙")
        profile = replace(self.record, profile="text-model-v2", vector=(-1.0, 0.0))
        self.index.upsert((self.record, other, tenant, profile))
        self.index.upsert((self.record,))
        restarted = SQLiteVectorIndex(self.root)
        matches = restarted.search("公司甲", "text-model-v1", (1.0, 0.0), 10)
        self.assertEqual([match.asset_id for match in matches], ["图像一", "图像二"])
        self.assertAlmostEqual(matches[0].score, 1.0)
        self.assertAlmostEqual(matches[1].score, 0.0)
        self.assertEqual(restarted.search("公司甲", "text-model-v2", (1.0, 0.0), 1)[0].score, -1.0)
        restarted.delete("公司甲", "图像一")
        restarted.delete("公司甲", "图像一")
        self.assertEqual([match.asset_id for match in restarted.search("公司甲", "text-model-v1", (1.0, 0.0), 10)], ["图像二"])
        self.assertEqual(len(restarted.search("公司乙", "text-model-v1", (1.0, 0.0), 10)), 1)
        self.assertEqual(restarted.search("公司甲", "text-model-v2", (1.0, 0.0), 10), ())

    def test_dimensions_and_identity_rejected_atomically(self):
        self.index.upsert((self.record,))
        with self.assertRaises(ConflictError):
            self.index.upsert((replace(self.record, record_id="valid-new"), replace(self.record, record_id="bad", vector=(1.0, 0.0, 0.0))))
        self.assertEqual(len(self.index.search("公司甲", "text-model-v1", (1.0, 0.0), 10)), 1)
        with self.assertRaises(ConflictError):
            self.index.upsert((replace(self.record, asset_id="another"),))
        with self.assertRaises(ConflictError):
            self.index.search("公司甲", "text-model-v1", (1.0,), 10)
        self.index.upsert((replace(self.record, profile="different-dim", vector=(1.0,)),))
        self.assertEqual(len(self.index.search("公司甲", "different-dim", (1.0,), 1)), 1)

    def test_invalid_values_are_rejected_before_storage(self):
        for vector in ((), (0.0, 0.0), (float("nan"), 1.0), (float("inf"),), (True, 1), ("1", 2), (10**1000, 1)):
            with self.subTest(vector=vector), self.assertRaises(ValidationError):
                replace(self.record, vector=vector)
        for limit in (0, True, 1001, "1"):
            with self.subTest(limit=limit), self.assertRaises(ValidationError):
                self.index.search("公司甲", "text-model-v1", (1.0, 0.0), limit)
        with self.assertRaises(ValidationError):
            self.index.search("invalid tenant", "text-model-v1", (1.0,), 1)
        with self.assertRaises(ValidationError):
            VectorMatch("r", "t", "a", "v", "p", float("nan"))


if __name__ == "__main__":
    unittest.main()
