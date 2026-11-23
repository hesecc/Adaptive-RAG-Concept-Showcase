"""Transport fixtures plus opt-in contracts against explicitly selected test services."""

from dataclasses import replace
from hashlib import sha256
import io
import json
import os
import unittest
from uuid import uuid4

import httpx

from adaptive_rag.domain import CapabilityError, ConflictError, NotFoundError, ValidationError, stable_id
from adaptive_rag.remote import MilvusVectorIndex, S3ObjectStore
from adaptive_rag.vector_store import VectorRecord


class StorageError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}
        super().__init__("credentials=must-not-escape")


class S3Fixture:
    def __init__(self):
        self.objects = {}
        self.fail = False

    def put_object(self, *, Bucket, Key, Body, **metadata):
        if self.fail:
            raise StorageError("AccessDenied")
        self.objects[Bucket, Key] = Body

    def get_object(self, *, Bucket, Key):
        if self.fail:
            raise StorageError("AccessDenied")
        try:
            value = self.objects[Bucket, Key]
        except KeyError:
            raise StorageError("NoSuchKey")
        return {"Body": io.BytesIO(value), "ContentLength": len(value)}


class S3Tests(unittest.TestCase):
    def setUp(self):
        self.client = S3Fixture()
        self.store = S3ObjectStore("http://objects:9000", "test-access", "test-secret", "rag-assets", client=self.client)

    def test_content_addressing_integrity_and_missing_objects(self):
        raw = "中文图片说明".encode()
        key = self.store.put(raw)
        self.assertEqual(key, sha256(raw).hexdigest())
        self.assertEqual(self.store.put(raw), key)
        self.assertEqual(self.store.get(key), raw)
        self.assertEqual(len(self.client.objects), 1)
        self.client.objects["rag-assets", self.store.object_key(key)] = b"modified"
        with self.assertRaises(ConflictError):
            self.store.get(key)
        with self.assertRaises(NotFoundError):
            self.store.get("0" * 64)
        with self.assertRaises(ValidationError):
            self.store.get("../../secret")

    def test_failure_is_sanitized_and_inputs_are_bounded(self):
        self.client.fail = True
        for call in (lambda: self.store.put(b"test"), lambda: self.store.get("0" * 64)):
            with self.assertRaises(CapabilityError) as caught:
                call()
            self.assertNotIn("credentials", str(caught.exception))
        tiny = S3ObjectStore("http://objects:9000", "test", "test", "rag-assets", client=self.client, max_object_bytes=2)
        with self.assertRaises(ValidationError):
            tiny.put(b"123")
        for endpoint in ("http://user:secret@host", "http://host/other", "file:///tmp", "http://host?key=x"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValidationError):
                S3ObjectStore(endpoint, "test", "test", "rag-assets", client=self.client)


def described(dimension=2):
    # This shape follows the Milvus v2.6 REST describe response example.
    fields = [{"name": "pk", "type": "VarChar", "primaryKey": True,
               "params": [{"key": "max_length", "value": "64"}]}]
    fields.extend({"name": name, "type": "VarChar", "params": [{"key": "max_length", "value": "512"}]}
                  for name in MilvusVectorIndex._metadata)
    fields.append({"name": "vector", "type": "FloatVector", "params": [{"key": "dim", "value": str(dimension)}]})
    return {"autoId": False, "enableDynamicField": False, "fields": fields,
            "indexes": [{"fieldName": "vector", "metricType": "COSINE"}]}


class MilvusTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.exists, self.rows = False, {}
        self.description = described()
        self.bad_search = False
        self.record = VectorRecord("description-1", "公司甲", "图一", "v1", "text-v1", (1.0, 0.0))
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.addCleanup(self.client.close)
        self.index = MilvusVectorIndex("http://milvus:19530", "rag_images", "secret-token", client=self.client)

    def handle(self, request):
        payload = json.loads(request.content)
        path = request.url.path.rsplit("/", 1)[-1]
        self.requests.append((path, payload))
        self.assertEqual(request.headers["Authorization"], "Bearer secret-token")
        self.assertEqual(payload["collectionName"], "rag_images")
        if path == "has":
            data = {"has": self.exists}
        elif path == "create":
            self.exists = True
            self.assertFalse(payload["schema"]["autoID"])
            self.assertFalse(payload["schema"]["enableDynamicField"])
            self.assertEqual(payload["indexParams"][0]["metricType"], "COSINE")
            data = {}
        elif path == "describe":
            data = self.description
        elif path == "load":
            data = {}
        elif path == "upsert":
            for row in payload["data"]:
                self.rows[row["pk"]] = row
            data = {"upsertCount": len(payload["data"])}
        elif path == "search":
            self.assertEqual(payload["consistencyLevel"], "Strong")
            self.assertEqual(payload["searchParams"]["metricType"], "COSINE")
            self.assertIn("tenant_id ==", payload["filter"])
            self.assertIn("profile ==", payload["filter"])
            data = [{"pk": row["pk"], "distance": 1.0,
                     **{field: row[field] for field in MilvusVectorIndex._metadata}}
                    for row in self.rows.values()]
            if self.bad_search:
                data[0]["tenant_id"] = "wrong-tenant"
        elif path == "delete":
            self.assertIn("tenant_id ==", payload["filter"])
            self.assertIn("asset_id ==", payload["filter"])
            self.rows.clear()
            data = {}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"code": 0, "data": data})

    def test_collection_creation_idempotent_upsert_search_and_delete(self):
        self.assertEqual(self.index.search("公司甲", "text-v1", (1.0, 0.0), 3), ())
        self.assertNotIn("create", [path for path, _ in self.requests])
        self.index.upsert((self.record,))
        self.index.upsert((self.record,))
        self.assertEqual(len(self.rows), 1)
        self.assertIn(stable_id("公司甲", "text-v1", "description-1"), self.rows)
        match = self.index.search("公司甲", "text-v1", (1.0, 0.0), 3)[0]
        self.assertEqual(match.asset_id, "图一")
        self.assertEqual(match.revision_id, "v1")
        self.assertEqual([path for path, _ in self.requests].count("create"), 1)
        self.index.delete("公司甲", "图一")
        self.index.delete("公司甲", "图一")
        self.assertEqual(self.index.search("公司甲", "text-v1", (1.0, 0.0), 3), ())

    def test_wrong_schema_dimension_and_result_namespace_fail(self):
        self.exists = True
        self.description = described(3)
        with self.assertRaises(ConflictError):
            self.index.upsert((self.record,))
        self.assertFalse(self.rows)
        with self.assertRaises(ConflictError):
            index = MilvusVectorIndex("http://milvus:19530", "rag_images", "secret-token", dimension=2, client=self.client)
            index.delete("公司甲", "图一")
        self.assertNotIn("delete", [path for path, _ in self.requests])
        self.description = described()
        self.index.upsert((self.record,))
        with self.assertRaises(ConflictError):
            self.index.search("公司甲", "text-v1", (1.0,), 1)
        self.bad_search = True
        with self.assertRaises(CapabilityError):
            self.index.search("公司甲", "text-v1", (1.0, 0.0), 1)

    def test_errors_never_expose_credentials_or_provider_text(self):
        def broken(_request):
            return httpx.Response(200, json={"code": 123, "message": "secret-token", "data": {}})
        with httpx.Client(transport=httpx.MockTransport(broken)) as client:
            index = MilvusVectorIndex("http://milvus:19530", "rag_images", "secret-token", client=client)
            with self.assertRaises(CapabilityError) as caught:
                index.upsert((self.record,))
            self.assertNotIn("secret-token", str(caught.exception))
        self.assertEqual(self.index.upsert(()), None)
        self.assertEqual(self.requests, [])
        with self.assertRaises(ValidationError):
            self.index.upsert((replace(self.record, vector=(0.0, 0.0)),))


@unittest.skipUnless(os.environ.get("RAG_TEST_MILVUS_ENDPOINT"),
                     "set RAG_TEST_MILVUS_ENDPOINT for a dedicated real Milvus test service")
class MilvusLiveTests(unittest.TestCase):
    def test_real_schema_upsert_namespace_and_idempotent_delete(self):
        endpoint = os.environ["RAG_TEST_MILVUS_ENDPOINT"]
        token = os.environ.get("RAG_TEST_MILVUS_TOKEN", "")
        collection = "an_rag_test_" + uuid4().hex
        index = MilvusVectorIndex(endpoint, collection, token=token, dimension=2)
        record = VectorRecord("caption-1", "tenant-a", "asset-a", "r1", "text-v1", (1.0, 0.0))
        try:
            self.assertEqual(index.search("tenant-a", "text-v1", (1.0, 0.0), 10), ())
            index.upsert((record, replace(record, record_id="caption-2", asset_id="asset-b", vector=(0.0, 1.0)),
                          replace(record, tenant_id="tenant-b"), replace(record, profile="text-v2", vector=(-1.0, 0.0))))
            index.upsert((record,))
            reopened = MilvusVectorIndex(endpoint, collection, token=token, dimension=2)
            hits = reopened.search("tenant-a", "text-v1", (1.0, 0.0), 10)
            self.assertEqual([hit.asset_id for hit in hits], ["asset-a", "asset-b"])
            self.assertAlmostEqual(hits[0].score, 1.0)
            self.assertAlmostEqual(hits[1].score, 0.0)
            self.assertEqual(len(reopened.search("tenant-b", "text-v1", (1.0, 0.0), 10)), 1)
            self.assertAlmostEqual(reopened.search("tenant-a", "text-v2", (1.0, 0.0), 10)[0].score, -1.0)
            wrong = MilvusVectorIndex(endpoint, collection, token=token, dimension=3)
            with self.assertRaises(ConflictError):
                wrong.delete("tenant-a", "asset-a")
            reopened.delete("tenant-a", "asset-a")
            reopened.delete("tenant-a", "asset-a")
            self.assertEqual([hit.asset_id for hit in reopened.search("tenant-a", "text-v1", (1.0, 0.0), 10)], ["asset-b"])
            self.assertEqual(reopened.search("tenant-a", "text-v2", (1.0, 0.0), 10), ())
            self.assertEqual(len(reopened.search("tenant-b", "text-v1", (1.0, 0.0), 10)), 1)
        finally:
            # This random collection belongs only to this test run.
            index._post("collections/drop", {})


@unittest.skipUnless(all(os.environ.get(name) for name in (
    "RAG_TEST_S3_ENDPOINT", "RAG_TEST_S3_ACCESS_KEY", "RAG_TEST_S3_SECRET_KEY", "RAG_TEST_S3_BUCKET")),
    "set RAG_TEST_S3_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET for a provisioned test bucket")
class S3LiveTests(unittest.TestCase):
    def test_real_content_addressing_integrity_missing_and_reopen(self):
        store = S3ObjectStore(os.environ["RAG_TEST_S3_ENDPOINT"], os.environ["RAG_TEST_S3_ACCESS_KEY"],
                              os.environ["RAG_TEST_S3_SECRET_KEY"], os.environ["RAG_TEST_S3_BUCKET"],
                              prefix="an-rag-tests/" + uuid4().hex)
        raw = "真实 S3 图片字节合同 🔎".encode()
        key = sha256(raw).hexdigest()
        object_key = store.object_key(key)
        try:
            self.assertEqual(store.put(raw), key)
            self.assertEqual(store.put(raw), key)
            self.assertEqual(store.get(key), raw)
            reopened = S3ObjectStore(os.environ["RAG_TEST_S3_ENDPOINT"], os.environ["RAG_TEST_S3_ACCESS_KEY"],
                                     os.environ["RAG_TEST_S3_SECRET_KEY"], store.bucket, prefix=store.prefix)
            self.assertEqual(reopened.get(key), raw)
            with self.assertRaises(NotFoundError):
                store.get("0" * 64)
            store.client.put_object(Bucket=store.bucket, Key=object_key, Body=b"tampered")
            with self.assertRaises(ConflictError):
                store.get(key)
        finally:
            # Delete only the content-addressed object under this test's random prefix.
            store.client.delete_object(Bucket=store.bucket, Key=object_key)


if __name__ == "__main__":
    unittest.main()
