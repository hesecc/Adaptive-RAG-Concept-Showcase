import asyncio
import io
import os
from pathlib import Path
import tempfile
from time import time
import unittest
from uuid import uuid4

try:
    from PIL import Image
except ImportError:
    Image = None

from adaptive_rag.assets import AssetService
from adaptive_rag.domain import AccessContext, CapabilityError, ConflictError, NotFoundError, ValidationError, stable_id
from adaptive_rag.storage import FileObjectStore
from adaptive_rag.vector_store import SQLiteVectorIndex


def picture(color="red"):
    content = io.BytesIO()
    Image.new("RGB", (64, 40), color).save(content, format="PNG")
    return content.getvalue()


class TextEmbedder:
    """Deterministic test fixture, never presented as a real model call."""
    def __init__(self):
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        return tuple((1.0, 0.1) if "流程" in text or "RAG" in text else (0.1, 1.0) for text in texts)


class FailingIndex:
    def __init__(self, index):
        self.inner, self.fail = index, True

    def upsert(self, records):
        self.inner.upsert(records)
        if self.fail:
            raise CapabilityError("secret-provider-error-key-must-not-leak")

    def search(self, *args):
        return self.inner.search(*args)

    def delete(self, *args):
        return self.inner.delete(*args)


@unittest.skipUnless(Image is not None, "image tests require the images optional dependency")
class AssetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.objects = FileObjectStore(self.root)
        self.index = SQLiteVectorIndex(self.root)
        self.access = AccessContext("team", "owner")
        self.service = AssetService(self.root, self.objects, self.index)

    def create(self, service=None, asset_id="diagram", **metadata):
        return (service or self.service).create(self.access, picture(), asset_id=asset_id,
                                               title="RAG 流程图", description="文档解析与混合检索流程", **metadata)

    def test_upload_reopen_bytes_thumbnail_and_idempotency(self):
        asset = self.create()
        self.assertEqual(asset["media_type"], "image/png")
        self.assertEqual((asset["width"], asset["height"]), (64, 40))
        self.assertNotIn("object_key", asset)
        self.assertEqual(self.create()["revision_id"], asset["revision_id"])
        self.assertEqual(len(self.service.list(self.access)), 1)
        reopened = AssetService(self.root, FileObjectStore(self.root), SQLiteVectorIndex(self.root))
        self.assertEqual(reopened.content(self.access, "diagram"), (picture(), "image/png"))
        thumbnail, mime = reopened.content(self.access, "diagram", thumbnail=True)
        self.assertEqual(mime, "image/jpeg")
        with Image.open(io.BytesIO(thumbnail)) as decoded:
            self.assertEqual(decoded.format, "JPEG")
        with self.assertRaises(ConflictError):
            reopened.create(self.access, picture("blue"), asset_id="diagram", title="different")

    def test_bad_inputs_never_create_asset(self):
        for content in (b"<svg><script>alert(1)</script></svg>", b"", b"x" * 10_000_001):
            with self.assertRaises(ValidationError):
                self.service.create(self.access, content, asset_id="bad", title="bad")
        with self.assertRaises(ValidationError):
            self.service.create(self.access, picture(), asset_id="bad", title="x", tags=["x"])
        with self.assertRaises(ValidationError):
            self.service.create(self.access, picture(), asset_id="bad", title="x", source_page=True)
        self.assertEqual(self.service.list(self.access), [])

    def test_animated_and_oversized_images_rejected(self):
        animated = io.BytesIO()
        Image.new("RGB", (10, 10), "red").save(animated, format="WEBP", save_all=True,
                append_images=[Image.new("RGB", (10, 10), "blue")])
        with self.assertRaises(ValidationError):
            self.service.create(self.access, animated.getvalue(), asset_id="animation", title="no")

    def test_lexical_search_zero_models_restart_and_trace(self):
        self.create()
        result = asyncio.run(self.service.search(self.access, "混合检索流程"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["search_mode"], "lexical")
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["hits"][0]["asset"]["asset_id"], "diagram")
        self.assertIn("混合检索", result["hits"][0]["matched_text"])
        reopened = AssetService(self.root, self.objects, self.index)
        self.assertEqual(reopened.get_trace(self.access, result["trace_id"])["hits"], result["hits"])
        empty = asyncio.run(self.service.search(self.access, "zzzznothing"))
        self.assertEqual((empty["status"], empty["hits"]), ("empty", []))

    def test_permission_boundary_for_content_search_and_trace(self):
        self.create(allowed_subjects=("owner",))
        result = asyncio.run(self.service.search(self.access, "RAG"))
        for access in (AccessContext("other", "owner"), AccessContext("team", "reader")):
            self.assertEqual(self.service.list(access), [])
            with self.assertRaises(NotFoundError):
                self.service.content(access, "diagram")
            self.assertEqual(asyncio.run(self.service.search(access, "RAG"))["hits"], [])
            with self.assertRaises(NotFoundError):
                self.service.get_trace(access, result["trace_id"])

    def test_metadata_update_cas_invalidates_previous_trace(self):
        asset = self.create()
        result = asyncio.run(self.service.search(self.access, "RAG"))
        values = dict(title="更新流程图", description="产品目录", ocr_text="SKU-A01", tags=("产品",), allowed_subjects=())
        updated = self.service.update(self.access, "diagram", expected_revision=asset["revision_id"], **values)
        self.assertNotEqual(updated["revision_id"], asset["revision_id"])
        self.assertEqual(self.service.content(self.access, "diagram")[0], picture())
        self.assertEqual(asyncio.run(self.service.search(self.access, "SKU-A01"))["hits"][0]["asset"]["asset_id"], "diagram")
        with self.assertRaises(ConflictError):
            self.service.update(self.access, "diagram", expected_revision=asset["revision_id"], **values)
        with self.assertRaises(NotFoundError):
            self.service.get_trace(self.access, result["trace_id"])

    def test_semantic_index_is_explicit_and_persistent(self):
        embedder = TextEmbedder()
        service = AssetService(self.root, self.objects, self.index, embedder=embedder)
        self.create(service)
        self.assertEqual(embedder.calls, 0, "upload must not call a paid model")
        before = asyncio.run(service.search(self.access, "流程"))
        self.assertEqual(before["hits"], [])
        processed = asyncio.run(service.process_pending("team"))
        self.assertEqual((processed["processed"], processed["ready"]), (1, 1))
        self.assertEqual(service.get(self.access, "diagram")["indexing_status"], "ready")
        self.assertEqual(asyncio.run(service.process_pending("team"))["processed"], 0)
        result = asyncio.run(service.search(self.access, "流程"))
        self.assertEqual(result["hits"][0]["asset"]["asset_id"], "diagram")
        self.assertIn("query_embedding", [stage["name"] for stage in result["stages"]])

    def test_external_index_partial_success_retries_cached_vectors(self):
        embedder = TextEmbedder()
        index = FailingIndex(self.index)
        service = AssetService(self.root, self.objects, index, embedder=embedder)
        self.create(service)
        first = asyncio.run(service.process_pending("team"))
        self.assertEqual(first["failed"], 1)
        self.assertNotIn("secret-provider", str(first))
        self.assertEqual(service.get(self.access, "diagram")["indexing_status"], "failed")
        self.assertEqual(asyncio.run(service.process_pending("team"))["processed"], 0)
        self.assertEqual(embedder.calls, 1)
        index.fail = False
        service.retry(self.access, "diagram")
        self.assertEqual(asyncio.run(service.process_pending("team"))["ready"], 1)
        self.assertEqual(embedder.calls, 1, "retry after vector write must reuse cached embedding")
        self.assertEqual(len(self.index.search("team", service.profile, (1.0, 0.1), 100)), 1)

    def test_successful_job_can_rebuild_lost_index_without_reembedding(self):
        embedder = TextEmbedder()
        service = AssetService(self.root, self.objects, self.index, embedder=embedder)
        self.create(service)
        asyncio.run(service.process_pending("team"))
        self.index.delete("team", "diagram")
        service.retry(self.access, "diagram")
        self.assertEqual(asyncio.run(service.process_pending("team"))["ready"], 1)
        self.assertEqual(embedder.calls, 1)
        self.assertTrue(self.index.search("team", service.profile, (1.0, 0.1), 100))

    def test_changed_profile_does_not_advertise_old_index_as_ready(self):
        old = AssetService(self.root, self.objects, self.index, embedder=TextEmbedder(), embedding_profile="old-model")
        self.create(old)
        asyncio.run(old.process_pending("team"))
        changed = AssetService(self.root, self.objects, self.index, embedder=TextEmbedder(), embedding_profile="new-model")
        self.assertEqual(changed.get(self.access, "diagram")["indexing_status"], "outdated")
        self.assertEqual(changed.list(self.access)[0]["indexing_status"], "outdated")
        self.assertEqual(asyncio.run(changed.search(self.access, "RAG"))["hits"], [])
        changed.retry(self.access, "diagram")
        self.assertEqual(asyncio.run(changed.process_pending("team"))["ready"], 1)
        self.assertEqual(changed.get(self.access, "diagram")["indexing_status"], "ready")

    def test_worker_never_reuses_pending_vectors_from_another_model_profile(self):
        old_index = FailingIndex(self.index)
        old = AssetService(self.root, self.objects, old_index, embedder=TextEmbedder(), embedding_profile="old-model")
        self.create(old)
        self.assertEqual(asyncio.run(old.process_pending("team"))["failed"], 1)
        old.retry(self.access, "diagram")
        new_embedder = TextEmbedder()
        changed = AssetService(self.root, self.objects, self.index, embedder=new_embedder, embedding_profile="new-model")
        self.assertEqual(asyncio.run(changed.process_pending("team"))["processed"], 0)
        changed.retry(self.access, "diagram")
        self.assertEqual(asyncio.run(changed.process_pending("team"))["ready"], 1)
        self.assertEqual(new_embedder.calls, 1)

    def test_superseded_profile_worker_does_not_call_model_or_replace_ready_state(self):
        old_embedder = TextEmbedder()
        old = AssetService(self.root, self.objects, self.index, embedder=old_embedder, embedding_profile="old-model")
        self.create(old)
        changed = AssetService(self.root, self.objects, self.index, embedder=TextEmbedder(), embedding_profile="new-model")
        changed.retry(self.access, "diagram")
        self.assertEqual(asyncio.run(changed.process_pending("team"))["ready"], 1)
        self.assertEqual(asyncio.run(old.process_pending("team"))["cancelled"], 1)
        self.assertEqual(old_embedder.calls, 0)
        self.assertEqual(changed.get(self.access, "diagram")["indexing_status"], "ready")

    def test_unknown_embedding_failure_requires_explicit_retry(self):
        class FailedEmbedder:
            async def embed(self, texts):
                raise TimeoutError("private-key-do-not-log")
        service = AssetService(self.root, self.objects, self.index, embedder=FailedEmbedder())
        self.create(service)
        failed = asyncio.run(service.process_pending("team"))
        self.assertEqual(failed["failed"], 1)
        self.assertNotIn("private-key", str(failed))
        self.assertEqual(asyncio.run(service.process_pending("team"))["processed"], 0)

    def test_concurrent_update_during_embedding_never_publishes_old_revision(self):
        service = None
        original = None
        class UpdatingEmbedder:
            calls = 0
            async def embed(inner, texts):
                inner.calls += 1
                if inner.calls == 1:
                    service.update(self.access, "diagram", title="新图", description="说明更新", ocr_text="", tags=(), allowed_subjects=(), expected_revision=original["revision_id"])
                return tuple((1.0, 0.1) for _ in texts)
        service = AssetService(self.root, self.objects, self.index, embedder=UpdatingEmbedder())
        original = self.create(service)
        first = asyncio.run(service.process_pending("team", limit=1))
        self.assertEqual(first["cancelled"], 1)
        self.assertEqual(service.get(self.access, "diagram")["indexing_status"], "pending")
        asyncio.run(service.process_pending("team"))
        result = asyncio.run(service.search(self.access, "流程"))
        self.assertEqual(result["hits"][0]["asset"]["title"], "新图")

    def test_reverting_metadata_requeues_existing_revision(self):
        service = AssetService(self.root, self.objects, self.index, embedder=TextEmbedder())
        original = self.create(service)
        asyncio.run(service.process_pending("team"))
        changed = service.update(self.access, "diagram", title="new", description="new description", ocr_text="", tags=(), allowed_subjects=(), expected_revision=original["revision_id"])
        asyncio.run(service.process_pending("team"))
        restored = service.update(self.access, "diagram", title=original["title"], description=original["description"], ocr_text="", tags=(), allowed_subjects=(), expected_revision=changed["revision_id"])
        self.assertEqual(restored["revision_id"], original["revision_id"])
        self.assertEqual(asyncio.run(service.process_pending("team"))["ready"], 1)
        self.assertEqual(service.get(self.access, "diagram")["indexing_status"], "ready")

    def test_delete_revokes_trace_content_and_semantic_hits_before_cleanup(self):
        service = AssetService(self.root, self.objects, self.index, embedder=TextEmbedder())
        asset = self.create(service)
        asyncio.run(service.process_pending("team"))
        result = asyncio.run(service.search(self.access, "流程"))
        service.delete(self.access, "diagram", asset["revision_id"])
        with self.assertRaises(NotFoundError):
            service.content(self.access, "diagram")
        with self.assertRaises(NotFoundError):
            service.get_trace(self.access, result["trace_id"])
        self.assertEqual(asyncio.run(service.search(self.access, "流程"))["hits"], [])
        asyncio.run(service.process_pending("team"))
        self.assertEqual(self.index.search("team", service.profile, (1.0, 0.1), 100), ())

    def test_disabled_embedding_preserves_pending_job(self):
        self.create()
        result = asyncio.run(self.service.process_pending("team"))
        self.assertFalse(result["embedding_enabled"])
        self.assertEqual(result["processed"], 0)
        service = AssetService(self.root, self.objects, self.index, embedder=TextEmbedder())
        self.assertEqual(asyncio.run(service.process_pending("team"))["ready"], 1)

    def test_search_failure_has_durable_safe_trace(self):
        class FailedEmbedder:
            async def embed(self, texts):
                raise TimeoutError("secret-key-value")
        service = AssetService(self.root, self.objects, self.index, embedder=FailedEmbedder())
        result = asyncio.run(service.search(self.access, "RAG"))
        self.assertEqual(result["status"], "error")
        self.assertNotIn("secret-key", str(result))
        self.assertEqual(service.get_trace(self.access, result["trace_id"])["status"], "error")

    def test_permission_revocation_during_object_read_blocks_content(self):
        asset = self.create()
        original = self.objects.get
        def interrupted(key):
            raw = original(key)
            if key == asset["sha256"]:
                self.service.delete(self.access, "diagram")
            return raw
        self.objects.get = interrupted
        with self.assertRaises(NotFoundError):
            self.service.content(self.access, "diagram")

    def test_revocation_during_trace_persistence_blocks_search_response(self):
        self.create()
        save = self.service._save_trace
        def revoke_after_save(trace):
            save(trace)
            self.service.delete(self.access, "diagram")
        self.service._save_trace = revoke_after_save
        with self.assertRaises(NotFoundError):
            asyncio.run(self.service.search(self.access, "RAG"))

    def test_delete_cleanup_requeued_after_late_vector_upsert(self):
        service = AssetService(self.root, self.objects, self.index, embedder=TextEmbedder())
        self.create(service)
        original = self.index.upsert
        def delayed(records):
            service.delete(self.access, "diagram")
            # Simulate cleanup finishing while the previous external upsert is
            # still in flight, then the old effect materializes afterwards.
            self.index.delete("team", "diagram")
            with service.db.transaction() as connection:
                connection.execute("UPDATE image_index_jobs SET state='done' WHERE tenant_id=? AND asset_id=? AND state='pending'", ("team", "diagram"))
            original(records)
        self.index.upsert = delayed
        self.assertEqual(asyncio.run(service.process_pending("team", limit=1))["cancelled"], 1)
        self.assertEqual(len(self.index.search("team", service.profile, (1.0, 0.1), 100)), 1)
        self.assertEqual(asyncio.run(service.process_pending("team"))["processed"], 1)
        self.assertEqual(self.index.search("team", service.profile, (1.0, 0.1), 100), ())

    def test_deleted_asset_id_can_be_created_again(self):
        first = self.create()
        self.service.delete(self.access, "diagram", first["revision_id"])
        second = self.service.create(self.access, picture("blue"), asset_id="diagram",
                                     title="新图", description="第二张图")
        self.assertNotEqual(second["revision_id"], first["revision_id"])
        self.assertEqual(self.service.get(self.access, "diagram")["title"], "新图")
        self.assertEqual(self.service.content(self.access, "diagram"), (picture("blue"), "image/png"))
        self.assertEqual([item["asset_id"] for item in self.service.list(self.access)], ["diagram"])
        # A live asset_id keeps conflicting, and the message names the supported way forward.
        with self.assertRaisesRegex(ConflictError, "expected_revision"):
            self.create()

    def test_reusing_asset_id_cancels_pending_cleanup_of_the_old_image(self):
        service = AssetService(self.root, self.objects, self.index, embedder=TextEmbedder())
        first = self.create(service)
        asyncio.run(service.process_pending("team"))
        service.delete(self.access, "diagram", first["revision_id"])  # the worker has not run, so cleanup is pending
        second = service.create(self.access, picture("blue"), asset_id="diagram",
                                title="RAG 新图", description="流程说明")
        result = asyncio.run(service.process_pending("team"))
        # Only the new image is indexed: a stale cleanup would delete every vector of the asset_id.
        self.assertEqual((result["ready"], result["deleted"]), (1, 0))
        hits = asyncio.run(service.search(self.access, "流程"))["hits"]
        self.assertEqual([hit["asset"]["revision_id"] for hit in hits], [second["revision_id"]])

    def test_reusing_asset_id_with_identical_content_is_indexed_again(self):
        embedder = TextEmbedder()
        service = AssetService(self.root, self.objects, self.index, embedder=embedder)
        first = self.create(service)
        asyncio.run(service.process_pending("team"))
        service.delete(self.access, "diagram")
        asyncio.run(service.process_pending("team"))
        self.assertEqual(self.index.search("team", service.profile, (1.0, 0.1), 100), ())
        again = self.create(service)
        self.assertNotEqual(again["revision_id"], first["revision_id"])
        with self.assertRaises(ConflictError):
            service.delete(self.access, "diagram", first["revision_id"])
        self.assertEqual(again["indexing_status"], "pending")
        self.assertEqual(asyncio.run(service.process_pending("team"))["ready"], 1)
        self.assertEqual(embedder.calls, 1, "cached vectors are reused, no second embedding request")
        self.assertEqual(len(asyncio.run(service.search(self.access, "流程"))["hits"]), 1)

    def test_deleting_a_reused_asset_id_schedules_cleanup_again(self):
        service = AssetService(self.root, self.objects, self.index, embedder=TextEmbedder())
        self.create(service)
        asyncio.run(service.process_pending("team"))
        service.delete(self.access, "diagram")
        asyncio.run(service.process_pending("team"))
        again = service.create(self.access, picture("blue"), asset_id="diagram",
                               title="RAG 新图", description="流程说明")
        asyncio.run(service.process_pending("team"))
        self.assertEqual(len(self.index.search("team", service.profile, (1.0, 0.1), 100)), 1)
        service.delete(self.access, "diagram", again["revision_id"])
        self.assertEqual(asyncio.run(service.process_pending("team"))["deleted"], 1)
        self.assertEqual(self.index.search("team", service.profile, (1.0, 0.1), 100), ())

    def test_reusing_asset_id_waits_while_its_cleanup_is_running(self):
        self.create()
        self.service.delete(self.access, "diagram")
        cleanup = stable_id("image-delete", "team", "diagram")
        with self.service.db.transaction() as connection:
            connection.execute("UPDATE image_index_jobs SET state='running',owner='worker',started_at=? WHERE tenant_id=? AND job_id=?",
                               (time(), "team", cleanup))
        with self.assertRaisesRegex(ConflictError, "cleaned up"):
            self.create()
        with self.assertRaises(NotFoundError):
            self.service.get(self.access, "diagram")
        with self.service.db.transaction() as connection:
            connection.execute("UPDATE image_index_jobs SET state='done',owner=NULL WHERE tenant_id=? AND job_id=?", ("team", cleanup))
        self.assertEqual(self.create()["asset_id"], "diagram")


@unittest.skipUnless(os.environ.get("RAG_TEST_POSTGRES_DSN") and Image is not None,
                     "set RAG_TEST_POSTGRES_DSN to run isolated PostgreSQL asset integration")
class PostgresAssetTests(unittest.TestCase):
    def test_postgres_asset_outbox_search_update_and_delete(self):
        import psycopg
        from psycopg.conninfo import make_conninfo
        from psycopg import sql
        dsn = os.environ["RAG_TEST_POSTGRES_DSN"]
        schema = "test_assets_" + uuid4().hex
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped = make_conninfo(dsn, options="-c search_path=" + schema)
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                objects, index = FileObjectStore(root), SQLiteVectorIndex(root)
                access = AccessContext("pg-team", "owner")
                service = AssetService(root, objects, index, TextEmbedder(), postgres_dsn=scoped)
                asset = service.create(access, picture(), asset_id="diagram", title="RAG流程图", description="混合检索")
                self.assertEqual(asyncio.run(service.process_pending("pg-team"))["ready"], 1)
                restarted = AssetService(root, objects, index, TextEmbedder(), postgres_dsn=scoped)
                result = asyncio.run(restarted.search(access, "流程"))
                self.assertEqual(result["hits"][0]["asset"]["revision_id"], asset["revision_id"])
                self.assertEqual(restarted.get_trace(access, result["trace_id"])["status"], "ok")
                changed = restarted.update(access, "diagram", title="新流程图", description="文档解析", ocr_text="", tags=(), allowed_subjects=(), expected_revision=asset["revision_id"])
                self.assertEqual(asyncio.run(restarted.process_pending("pg-team"))["ready"], 1)
                restarted.delete(access, "diagram", changed["revision_id"])
                with self.assertRaises(NotFoundError):
                    restarted.content(access, "diagram")
                self.assertEqual(asyncio.run(restarted.search(access, "流程"))["hits"], [])
        finally:
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    unittest.main()
