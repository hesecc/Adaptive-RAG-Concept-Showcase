import base64
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from adaptive_rag.assets import AssetService
from adaptive_rag.config import Principal
from adaptive_rag.domain import ConflictError, ForbiddenError, NotFoundError
from adaptive_rag.storage import FileObjectStore
from adaptive_rag.vector_store import SQLiteVectorIndex
from test_assets import Image, picture


@unittest.skipUnless(Image, "requires Pillow")
class ImageLifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lib = AssetService(self.root, FileObjectStore(self.root), SQLiteVectorIndex(self.root))
        self.owner = Principal("team", "alice", data_domains=("finance",))
        self.other = Principal("team", "reader", data_domains=("finance",))
        self.admin = Principal("team", "root", admin=True, data_domains=("hr",))
        self.foreign = Principal("elsewhere", "root", admin=True)

    def create(self, principal=None, **kw):
        return self.lib.managed_create(principal or self.owner, **({"asset_id": "fig", "title": "original",
                                       "content": picture()} | kw))

    def test_manage_permission_is_not_read_permission(self):
        item = self.create()
        self.assertEqual(item["allowed_subjects"], ["alice"])
        self.assertEqual(self.lib.list(self.other.access), [])
        self.assertEqual(self.lib.list(self.admin.access), [])
        self.assertEqual(self.lib.managed_images(self.admin, "mine"), [])
        self.assertEqual(self.lib.managed_images(self.admin, "all")[0]["owner_id"], "alice")
        self.assertEqual(self.lib.managed_content(self.admin, "fig"), (picture(), "image/png"))
        self.assertEqual(self.lib.managed_reindex(self.admin, "fig")["status"], "pending")
        with self.assertRaises(ForbiddenError):
            self.lib.managed_reindex(self.owner, "fig")
        for actor in (self.other, self.foreign):
            with self.assertRaises(NotFoundError):
                self.lib.managed_content(actor, "fig")
            self.assertEqual(self.lib.managed_images(actor, "mine"), [])
        saved = self.lib.managed_update(self.admin, "fig", expected_revision=item["revision_id"],
                                        allowed_subjects=("reader",))
        self.assertEqual(saved["owner_id"], "alice")
        self.assertEqual(self.lib.get(self.other.access, "fig")["asset_id"], "fig")
        with self.assertRaises(NotFoundError):
            self.lib.managed_image(self.other, "fig")
        with self.assertRaises(NotFoundError):
            self.lib.trash_image(self.other, "fig", saved["revision_id"])
        with self.assertRaises(ForbiddenError):
            self.lib.managed_images(self.owner, "all")
        self.assertEqual(self.lib.managed_images(self.foreign, "all"), [])

    def test_cannot_widen_acl_or_impersonate_owner_by_create(self):
        item = self.create()
        self.assertEqual(self.create()["revision_id"], item["revision_id"])
        for acl in ({"allowed_subjects": ("reader",)}, {"data_domains": ("public",)}):
            with self.assertRaises(ForbiddenError):
                self.create(asset_id="other", **acl)
            with self.assertRaises(ForbiddenError):
                self.lib.managed_update(self.owner, "fig", expected_revision=item["revision_id"], **acl)
        with self.assertRaises(ConflictError):
            self.create(self.other)
        self.lib.trash_image(self.owner, "fig", item["revision_id"])
        with self.assertRaises(ConflictError):
            self.create(self.other)
        # Same tenant-local identifier does not reserve another tenant's name.
        self.assertEqual(self.create(self.foreign)["owner_id"], "root")

    def test_legacy_delete_admin_hold_and_exact_deadline(self):
        item = self.create()
        with patch("adaptive_rag.assets.time", return_value=1_700_000_000):
            self.lib.delete(self.admin, "fig", item["revision_id"])
        tomb, = self.lib.managed_images(self.owner, "trash")
        self.assertTrue(tomb["admin_hold"])
        self.assertFalse(tomb["can_restore"])
        end = datetime.fromisoformat(tomb["purge_after"]).timestamp()
        self.assertEqual(end - datetime.fromisoformat(tomb["deleted_at"]).timestamp(), 168 * 3600)
        with patch("adaptive_rag.assets.time", return_value=end - 1), self.assertRaises(ForbiddenError):
            self.create()
        with self.assertRaises(ForbiddenError):
            self.lib.restore_image(self.owner, "fig", tomb["trash_id"])
        with self.assertRaises(NotFoundError):
            self.lib.restore_image(self.foreign, "fig", tomb["trash_id"])
        with patch("adaptive_rag.assets.time", return_value=end):
            self.assertFalse(self.lib.managed_images(self.admin, "trash")[0]["can_restore"])
            with self.assertRaises(ConflictError):
                self.lib.restore_image(self.admin, "fig", tomb["trash_id"])
        with patch("adaptive_rag.assets.time", return_value=end - 0.001):
            self.assertEqual(self.lib.restore_image(self.admin, "fig", tomb["trash_id"])["owner_id"], "alice")

    def test_reuse_and_restore_select_the_original_incarnation(self):
        old = self.create()
        old_trash = self.lib.trash_image(self.owner, "fig", old["revision_id"])
        replacement = self.create()
        self.assertNotEqual(replacement["revision_id"], old["revision_id"])
        with self.assertRaises(ConflictError):
            self.lib.restore_image(self.owner, "fig", old_trash["trash_id"])
        with self.assertRaises(ConflictError):
            self.lib.managed_content(self.owner, "fig", old["revision_id"])
        with self.assertRaises(ConflictError):
            self.lib.trash_image(self.owner, "fig", old["revision_id"])
        new_trash = self.lib.trash_image(self.owner, "fig", replacement["revision_id"])
        restarted = AssetService(self.root, self.lib.objects, self.lib.index)
        restored = restarted.restore_image(self.owner, "fig", old_trash["trash_id"])
        self.assertEqual(restored["incarnation_id"], old["incarnation_id"])
        self.assertEqual(restarted.managed_images(self.admin, "trash")[0]["trash_id"], new_trash["trash_id"])
        detail = restarted.managed_image(self.owner, "fig")
        self.assertEqual({v["revision_id"] for v in detail["versions"]}, {old["revision_id"]})
        self.assertEqual([e["action"] for e in detail["events"]], ["restore", "trash", "create"])

    def test_preview_rechecks_after_object_read(self):
        item = self.create()
        def slow_read(key):
            self.lib.trash_image(self.owner, "fig", item["revision_id"])
            return picture()
        with patch.object(self.lib.objects, "get", slow_read), self.assertRaises(NotFoundError):
            self.lib.managed_content(self.admin, "fig", item["revision_id"])

    def test_owner_preview_admin_delete_and_restore_over_http(self):
        from fastapi.testclient import TestClient
        from adaptive_rag.api import create_api
        from adaptive_rag.config import Settings
        from support import ADMIN_PASSWORD

        api = create_api(Settings(data_dir=self.root, admin_password=ADMIN_PASSWORD))
        with TestClient(api) as admin, TestClient(api) as owner:
            self.assertEqual(admin.post("/auth/login", json={"username": "ragadmin", "password": ADMIN_PASSWORD}).status_code, 200)
            credentials = {"username": "alice", "password": "scratch-password"}
            self.assertEqual(admin.post("/admin/users", json=credentials).status_code, 201)
            self.assertEqual(owner.post("/auth/login", json=credentials).status_code, 200)
            body = {"asset_id": "fig", "title": "Test", "content_base64": base64.b64encode(picture()).decode()}
            result = owner.post("/files/images", json=body)
            self.assertEqual(result.status_code, 201, result.text)
            rev = result.json()["revision_id"]
            self.assertEqual(admin.get("/assets/fig/content").status_code, 404)
            self.assertEqual(admin.get("/files/image/fig/content", params={"revision": rev}).content, picture())
            self.assertEqual(admin.post("/assets/fig/reindex").status_code, 200)
            self.assertEqual(admin.delete("/assets/fig", params={"expected_revision": rev}).status_code, 204)
            self.assertEqual(owner.get("/files/image/fig/content").status_code, 404)
            tomb, = owner.get("/files", params={"view": "trash"}).json()["files"]
            self.assertFalse(tomb["can_restore"])
            restore = {"trash_id": tomb["trash_id"]}
            self.assertEqual(owner.post("/files/image/fig/restore", json=restore).status_code, 403)
            self.assertEqual(admin.post("/files/image/fig/restore", json=restore).status_code, 200)
            self.assertEqual(owner.get("/files/image/fig/content").content, picture())
