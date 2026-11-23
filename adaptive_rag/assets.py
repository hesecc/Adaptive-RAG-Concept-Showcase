"""Image assets indexed by their text descriptions, never by image embeddings.

Asset metadata is authoritative. The vector index is derived and every hit is
hydrated against current metadata and ACLs. Uploads do not run paid models;
explicit indexing consumes the durable metadata outbox outside DB transactions.
"""

import asyncio
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import math
from pathlib import Path
from time import monotonic, time
from uuid import uuid4
import warnings

from .assets_persistence import AssetDatabase, encode
from .config import Principal
from .domain import (AccessContext, CapabilityError, ConflictError, ForbiddenError, NotFoundError,
                     ValidationError, identifier, nonempty, safe_error, stable_id)
from .retrieval import tokenize
from .data_access import domains_allowed, normalize_data_domains


MAX_IMAGE_BYTES = 10_000_000
TRASH_SECONDS = 168 * 3600


def _revision(content_hash, metadata, source_document_id, source_page, incarnation):
    fields = (content_hash, metadata, source_document_id, source_page)
    if incarnation is None:  # revisions persisted before lifecycle support
        return stable_id("image-revision-v1", *fields)
    return stable_id("image-revision-v2", incarnation, *fields)


def _manages(principal, asset):
    return principal.tenant_id == asset["tenant_id"] and (
        principal.admin or principal.subject_id == asset.get("created_by"))


def _text(value, name, limit):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValidationError(f"{name} must contain at most {limit} characters without NUL")
    return value.strip()


def _metadata(title, description, ocr_text, tags, allowed_subjects, data_domains):
    title = nonempty(title, "title", 300).strip()
    description = _text(description, "description", 16_000)
    ocr_text = _text(ocr_text, "ocr_text", 100_000)
    if not isinstance(tags, tuple) or len(tags) > 100:
        raise ValidationError("tags must be a tuple of at most 100 strings")
    tags = tuple(dict.fromkeys(nonempty(tag, "tag", 100).strip() for tag in tags))
    if not isinstance(allowed_subjects, tuple) or len(allowed_subjects) > 1000:
        raise ValidationError("allowed_subjects must be a tuple of at most 1000 identifiers")
    subjects = tuple(dict.fromkeys(identifier(subject, "allowed_subject") for subject in allowed_subjects))
    return {"title": title, "description": description, "ocr_text": ocr_text,
            "tags": list(tags), "allowed_subjects": list(subjects),
            "data_domains": list(normalize_data_domains(data_domains))}


def _image(content):
    if not isinstance(content, bytes) or not 1 <= len(content) <= MAX_IMAGE_BYTES:
        raise ValidationError("image content must contain 1-10000000 bytes")
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        raise CapabilityError("image uploads require the Pillow dependency") from None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as probe:
                fmt = probe.format
                width, height = probe.size
                if fmt not in ("PNG", "JPEG", "WEBP") or width * height > 25_000_000 or max(width, height) > 16_384:
                    raise ValidationError("image must be PNG, JPEG or WEBP with at most 25 million pixels")
                if getattr(probe, "n_frames", 1) != 1:
                    raise ValidationError("animated images are not supported")
                probe.verify()
            with Image.open(io.BytesIO(content)) as decoded:
                decoded.load()
                decoded.thumbnail((480, 480))
                preview = io.BytesIO()
                decoded.convert("RGB").save(preview, format="JPEG", quality=85)
        return {"media_type": {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[fmt],
                "width": width, "height": height, "size_bytes": len(content)}, preview.getvalue()
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValidationError("image cannot be decoded as a supported static image") from None


def image_info(content):
    """Media type and size of a static PNG, JPEG or WEBP; raises ValidationError for anything else.

    The same checks as an image upload, shared with document figures.
    """
    return _image(content)[0]


def _allowed(asset, access):
    subjects = asset["allowed_subjects"]
    return (asset["tenant_id"] == access.tenant_id
            and domains_allowed(asset.get("data_domains", ()), access.data_domains)
            and (not subjects or bool(set(subjects) & access.subjects)))


def _representations(asset):
    # OCR is split independently so an excessively long transcript does not
    # drown the short description. Each immutable representation owns an ID.
    summary = "\n".join(filter(None, (asset["title"], asset["description"], " ".join(asset["tags"]))))
    pieces = [summary[i:i + 3000] for i in range(0, len(summary), 3000)]
    ocr = asset["ocr_text"]
    pieces.extend(ocr[i:i + 3000] for i in range(0, len(ocr), 3000))
    return [(stable_id("image-text-v1", asset["tenant_id"], asset["asset_id"], asset["revision_id"], i, text), text)
            for i, text in enumerate(pieces) if text.strip()]


class AssetService:
    def __init__(self, data_dir: Path, objects, index, embedder=None,
                 embedding_profile="image-descriptions-v1", postgres_dsn=None):
        identifier(embedding_profile, "embedding_profile")
        self.db = AssetDatabase(data_dir, postgres_dsn)
        self.objects, self.index, self.embedder = objects, index, embedder
        self.profile = embedding_profile

    def _public(self, asset):
        value = dict(asset)
        value.setdefault("data_domains", [])
        indexed_profile = value.get("index_profile") or value.get("index_target_profile")
        if indexed_profile is not None and indexed_profile != self.profile:
            value["indexing_status"] = "outdated"
        value["content_url"] = f"/assets/{asset['asset_id']}/content"
        value["thumbnail_url"] = f"/assets/{asset['asset_id']}/thumbnail"
        value.pop("object_key", None)
        value.pop("thumbnail_key", None)
        return value

    def _current(self, connection, access, asset_id):
        identifier(asset_id, "asset_id")
        result = self.db.current(connection, access.tenant_id, asset_id)
        if not result or result[1] or not _allowed(result[0], access):
            raise NotFoundError("image asset not found or no longer accessible")
        return result[0]

    def create(self, access: AccessContext, content: bytes, *, asset_id: str,
               title: str, description="", ocr_text="", tags=(), allowed_subjects=(),
               source_document_id=None, source_page=None, data_domains=(), _principal=None):
        identifier(asset_id, "asset_id")
        metadata = _metadata(title, description, ocr_text, tags, allowed_subjects, data_domains)
        if source_document_id is not None:
            identifier(source_document_id, "source_document_id")
        if source_page is not None and (type(source_page) is not int or not 1 <= source_page <= 100_000):
            raise ValidationError("source_page must be a positive page number")
        image, thumbnail = _image(content)
        content_hash = sha256(content).hexdigest()
        incarnation = uuid4().hex
        revision = _revision(content_hash, metadata, source_document_id, source_page, incarnation)
        # Validation precedes object writes. A failure after either put can
        # leave an unreferenced content-addressed object, never a broken asset.
        object_key = self.objects.put(content)
        thumbnail_key = self.objects.put(thumbnail)
        asset = {"tenant_id": access.tenant_id, "asset_id": asset_id,
                 "revision_id": revision, "incarnation_id": incarnation, "object_key": object_key,
                 "thumbnail_key": thumbnail_key, "sha256": content_hash,
                 "source_document_id": source_document_id, "source_page": source_page,
                 "source": "document" if source_document_id else "upload",
                 "created_by": access.subject_id, "created_at": time(), "updated_by": access.subject_id,
                 "indexing_status": "pending", "index_profile": None,
                 "index_target_profile": self.profile,
                 "index_error": None, "index_stages": [],
                 **image, **metadata}
        with self.db.transaction(access.tenant_id + ":" + asset_id) as connection:
            current = self.db.current(connection, access.tenant_id, asset_id)
            deleted = bool(current and current[1])
            if current and not deleted:
                existing = current[0]
                desired = _revision(content_hash, metadata, source_document_id, source_page, existing.get("incarnation_id"))
                if (existing["revision_id"] == desired and
                        (existing.get("created_by") == access.subject_id or (_principal and _principal.admin))):
                    return self._public(current[0])
                raise ConflictError("asset_id already exists; update metadata using expected_revision")
            if not (_principal and _principal.admin):
                self._check_reuse(connection, access, asset_id, current[0] if current else None)
            self._free_asset_id(connection, access.tenant_id, asset_id)
            self.db.write_asset(connection, asset)
            self._event(connection, asset, "create", access.subject_id)
            job_id = self.db.enqueue(connection, asset, self.profile)
            if deleted and current[0]["revision_id"] == _revision(content_hash, metadata, source_document_id,
                    source_page, current[0].get("incarnation_id")):
                self.db.copy_vectors(connection, asset, self.profile, current[0]["revision_id"], job_id)
        return self._public(asset)

    @staticmethod
    def _check_reuse(connection, access, asset_id, previous):
        if previous and previous.get("created_by") != access.subject_id:
            raise ConflictError("asset_id is unavailable")
        if connection.execute("SELECT 1 FROM image_trash WHERE tenant_id=? AND asset_id=? AND admin_hold=1 AND expires_at>?",
                              (access.tenant_id, asset_id, time())).fetchone():
            raise ForbiddenError("image is held by an administrator")

    def managed_create(self, principal, *, content, asset_id, title, description="", ocr_text="", tags=(),
                       allowed_subjects=(), source_document_id=None, source_page=None, data_domains=()):
        if not principal.admin:
            if allowed_subjects not in ((), (principal.subject_id,)) or not set(data_domains) <= set(principal.data_domains or ()):
                raise ForbiddenError("cannot grant image access outside your permissions")
            allowed_subjects = (principal.subject_id,)
            if not data_domains:
                data_domains = principal.data_domains or ()
        asset = self.create(principal.access, content, asset_id=asset_id, title=title, description=description,
                            ocr_text=ocr_text, tags=tags, allowed_subjects=allowed_subjects, data_domains=data_domains,
                            source_page=source_page, source_document_id=source_document_id, _principal=principal)
        return self._managed_row(principal, asset)

    @staticmethod
    def _free_asset_id(connection, tenant_id, asset_id):
        """Make a deleted asset_id reusable.

        Delete cleanup removes every vector stored under the asset_id. A cleanup that has not
        run yet must therefore be cancelled, or it would later erase the new image's vectors.
        Vectors of older revisions that stay behind are ignored when hits are hydrated and are
        removed by the cleanup scheduled when the new image is deleted.
        """
        job_id = stable_id("image-delete", tenant_id, asset_id)
        connection.execute("UPDATE image_index_jobs SET state='cancelled',owner=NULL WHERE tenant_id=? AND job_id=? AND state IN ('pending','failed')", (tenant_id, job_id))
        row = connection.execute("SELECT state FROM image_index_jobs WHERE tenant_id=? AND job_id=?", (tenant_id, job_id)).fetchone()
        if row and row["state"] == "running":
            raise ConflictError("the previous image with this asset_id is still being cleaned up; retry in a moment")

    def list(self, access):
        with self.db.transaction() as connection:
            rows = connection.execute("SELECT body FROM image_assets WHERE tenant_id=? AND deleted=0 ORDER BY asset_id", (access.tenant_id,)).fetchall()
        return [self._public(asset) for asset in (json.loads(row["body"]) for row in rows) if _allowed(asset, access)]

    def get(self, access, asset_id):
        with self.db.transaction() as connection:
            return self._public(self._current(connection, access, asset_id))

    def _managed_current(self, connection, principal, asset_id):
        current = self.db.current(connection, principal.tenant_id, identifier(asset_id, "asset_id"))
        if not current or current[1] or not _manages(principal, current[0]):
            raise NotFoundError("image asset not found")
        return current[0]

    @staticmethod
    def _event(conn, asset, action, actor):
        conn.execute("INSERT INTO image_events(tenant_id,asset_id,incarnation,event_id,created_at,action,actor_id,revision_id) "
                     "VALUES(?,?,?,?,?,?,?,?)", (asset["tenant_id"], asset["asset_id"], asset.get("incarnation_id", ""),
                     uuid4().hex, time(), action, actor, asset["revision_id"]))

    def _detail(self, conn, principal, asset, trash=None):
        key = asset["tenant_id"], asset["asset_id"]
        item = self._managed_row(principal, asset, trash)
        revisions = conn.execute("SELECT body FROM image_asset_revisions WHERE tenant_id=? AND asset_id=?", key)
        versions = [{"revision_id": v["revision_id"], "title": v["title"], "updated_by": v.get("updated_by", v["created_by"]),
                     "updated_at": v.get("updated_at", v["created_at"])} for v in (json.loads(r["body"]) for r in revisions)
                    if v.get("incarnation_id") == asset.get("incarnation_id") and v["created_at"] == asset["created_at"]]
        item["versions"] = sorted(versions, key=lambda v: v["updated_at"], reverse=True)
        item["events"] = [dict(e) for e in conn.execute("SELECT action,actor_id,created_at,revision_id FROM image_events "
                          "WHERE tenant_id=? AND asset_id=? AND incarnation=? ORDER BY created_at DESC,event_id",
                          (*key, asset.get("incarnation_id", "")))]
        return item

    def _managed_row(self, principal, asset, trash=None, *, occupied=False):
        public = self._public(asset)
        utc = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat()
        return public | {
            "kind": "image", "id": asset["asset_id"], "version": asset["revision_id"], "owner_id": asset.get("created_by"),
            "updated_by": asset.get("updated_by", asset.get("created_by")),
            "updated_at": utc(asset.get("updated_at", asset["created_at"])),
            "state": "trash" if trash else "active", "status": "trashed" if trash else public["indexing_status"],
            "trash_id": trash["trash_id"] if trash else None, "deleted_by": trash["deleted_by"] if trash else None,
            "deleted_at": utc(trash["deleted_at"]) if trash else None,
            "purge_after": utc(trash["expires_at"]) if trash else None,
            "admin_hold": bool(trash and trash["admin_hold"]),
            "can_restore": bool(trash and not occupied and time() < trash["expires_at"] and
                                (principal.admin or (not trash["admin_hold"] and trash["deleted_by"] == principal.subject_id))),
        }

    def managed_images(self, principal, view="mine"):
        if view not in ("mine", "all", "trash"):
            raise ValidationError("view must be mine, all or trash")
        if view == "all" and not principal.admin:
            raise ForbiddenError("administrator required")
        with self.db.transaction() as conn:
            if view == "trash":
                rows = conn.execute("SELECT t.*,a.deleted FROM image_trash t LEFT JOIN image_assets a "
                                    "USING(tenant_id,asset_id) WHERE t.tenant_id=? ORDER BY t.deleted_at DESC,t.trash_id",
                                    (principal.tenant_id,))
                return [self._managed_row(principal, asset, row, occupied=row["deleted"] == 0)
                        for row in rows if _manages(principal, asset := json.loads(row["body"]))]
            rows = conn.execute("SELECT body FROM image_assets WHERE tenant_id=? AND deleted=0 ORDER BY asset_id", (principal.tenant_id,))
            return [self._managed_row(principal, asset) for asset in (json.loads(row["body"]) for row in rows)
                    if view == "all" or asset.get("created_by") == principal.subject_id]

    def managed_image(self, principal, asset_id):
        with self.db.transaction() as conn:
            current = self.db.current(conn, principal.tenant_id, identifier(asset_id, "asset_id"))
            if current and not current[1]:
                if not _manages(principal, current[0]):
                    raise NotFoundError("image asset not found")
                return self._detail(conn, principal, current[0])
            rows = conn.execute("SELECT * FROM image_trash WHERE tenant_id=? AND asset_id=? ORDER BY deleted_at DESC,trash_id",
                                (principal.tenant_id, asset_id))
            for row in rows:
                asset = json.loads(row["body"])
                if _manages(principal, asset):
                    return self._detail(conn, principal, asset, row)
            raise NotFoundError("image asset not found")

    def managed_content(self, principal, asset_id, expected_revision=None):
        return self._content(principal, asset_id, False, self._managed_current, expected_revision)

    def update(self, access, asset_id, *, title, description, ocr_text, tags,
               allowed_subjects, expected_revision, data_domains=None, _principal=None):
        identifier(expected_revision, "expected_revision")
        with self.db.transaction(access.tenant_id + ":" + asset_id) as connection:
            asset = (self._managed_current(connection, _principal, asset_id) if _principal else
                     self._current(connection, access, asset_id))
            if _principal:
                title = asset["title"] if title is None else title
                description = asset["description"] if description is None else description
                ocr_text = asset["ocr_text"] if ocr_text is None else ocr_text
                tags = tuple(asset["tags"]) if tags is None else tags
                if allowed_subjects is None:
                    allowed_subjects = tuple(asset["allowed_subjects"])
            if _principal and not _principal.admin:
                # Empty form ACL means preserve. Only administrators edit grants/classification.
                if ((allowed_subjects and allowed_subjects != tuple(asset["allowed_subjects"])) or
                        (data_domains is not None and set(data_domains) != set(asset.get("data_domains", ())))):
                    raise ForbiddenError("only administrators can change image access")
                allowed_subjects = tuple(asset["allowed_subjects"])
            metadata = _metadata(title, description, ocr_text, tags, allowed_subjects,
                tuple(asset.get("data_domains", ())) if data_domains is None else data_domains)
            if asset["revision_id"] != expected_revision:
                raise ConflictError("image asset changed; reload its current revision")
            revision = _revision(asset["sha256"], metadata, asset["source_document_id"], asset["source_page"],
                                 asset.get("incarnation_id"))
            if revision == expected_revision:
                return self._public(asset)
            asset = asset | metadata | {"revision_id": revision, "updated_by": access.subject_id,
                                        "updated_at": time(), "indexing_status": "pending",
                                        "index_profile": None, "index_target_profile": self.profile,
                                        "index_error": None, "index_stages": []}
            self.db.write_asset(connection, asset)
            self._event(connection, asset, "update", access.subject_id)
            self.db.enqueue(connection, asset, self.profile, requeue=True)
        return self._public(asset)

    def managed_update(self, principal, asset_id, *, expected_revision, title=None, description=None, ocr_text=None,
                       tags=None, allowed_subjects=None, data_domains=None):
        asset = self.update(principal.access, asset_id, title=title, description=description, ocr_text=ocr_text,
                            tags=tags, allowed_subjects=allowed_subjects, data_domains=data_domains,
                            expected_revision=expected_revision, _principal=principal)
        return self._managed_row(principal, asset)

    def delete(self, access, asset_id, expected_revision=None):
        # AccessContext has no role: legacy in-process calls may delete their own asset only.
        principal = access if isinstance(access, Principal) else Principal(access.tenant_id, access.subject_id)
        self._trash(principal, asset_id, expected_revision)

    def trash_image(self, principal, asset_id, expected_revision):
        identifier(expected_revision, "expected_revision")
        return self._trash(principal, asset_id, expected_revision)

    def _trash(self, access, asset_id, expected_revision):
        with self.db.transaction(access.tenant_id + ":" + asset_id) as connection:
            asset = self._managed_current(connection, access, asset_id)
            if expected_revision is not None and expected_revision != asset["revision_id"]:
                raise ConflictError("image asset changed; reload its current revision")
            now = time()
            self._event(connection, asset, "trash", access.subject_id)
            trash = dict(trash_id=uuid4().hex, deleted_at=now, expires_at=now + TRASH_SECONDS,
                         deleted_by=access.subject_id, admin_hold=int(access.admin))
            connection.execute("INSERT INTO image_trash(tenant_id,asset_id,trash_id,deleted_at,expires_at,deleted_by,admin_hold,body) "
                               "VALUES(?,?,?,?,?,?,?,?)", (access.tenant_id, asset_id, trash["trash_id"], now,
                               trash["expires_at"], access.subject_id, int(access.admin), encode(asset)))
            asset = asset | {"indexing_status": "deleted", "trash_id": trash["trash_id"]}
            connection.execute("UPDATE image_assets SET deleted=1,body=? WHERE tenant_id=? AND asset_id=?", (encode(asset), access.tenant_id, asset_id))
            # Cleanup remains an outbox effect; deleting metadata succeeds even
            # when Milvus is temporarily unavailable. Files retain historical
            # references until a future object retention policy removes them.
            connection.execute("UPDATE image_index_jobs SET state='cancelled',owner=NULL WHERE tenant_id=? AND asset_id=? AND state IN ('pending','failed','running')", (access.tenant_id, asset_id))
            cleanup = {"kind": "delete", "asset_id": asset_id, "revision_id": asset["revision_id"], "profile": self.profile, "vectors": None, "stages": []}
            # The job id is fixed per asset_id. A row left by an earlier incarnation of a reused
            # asset_id (done, failed or cancelled) must be reset, or this delete would never clean up.
            connection.execute("INSERT INTO image_index_jobs(tenant_id,job_id,asset_id,revision_id,state,body) VALUES(?,?,?,?,?,?) ON CONFLICT(tenant_id,job_id) DO UPDATE SET state='pending',owner=NULL,revision_id=excluded.revision_id,body=excluded.body", (access.tenant_id, stable_id("image-delete", access.tenant_id, asset_id), asset_id, asset["revision_id"], "pending", encode(cleanup)))
            return self._managed_row(access, asset, trash)

    def restore_image(self, principal, asset_id, trash_id):
        tenant = principal.tenant_id
        identifier(asset_id, "asset_id")
        identifier(trash_id, "trash_id")
        with self.db.transaction(tenant + ":" + asset_id) as conn:
            row = conn.execute("SELECT * FROM image_trash WHERE tenant_id=? AND asset_id=? AND trash_id=?",
                               (tenant, asset_id, trash_id)).fetchone()
            if not row or not _manages(principal, asset := json.loads(row["body"])):
                raise NotFoundError("trashed image not found")
            if not principal.admin and (row["admin_hold"] or row["deleted_by"] != principal.subject_id):
                raise ForbiddenError("image requires an administrator to restore")
            now = time()
            if now >= row["expires_at"]:
                raise ConflictError("image restore period expired")
            current = self.db.current(conn, tenant, asset_id)
            if current and not current[1]:
                raise ConflictError("asset_id is already in use")
            if current and not principal.admin:
                self._check_reuse(conn, principal.access, asset_id, current[0])
            self._free_asset_id(conn, tenant, asset_id)
            asset.update(indexing_status="pending", index_profile=None, index_target_profile=self.profile,
                         index_error=None, index_stages=[], updated_at=now, updated_by=principal.subject_id)
            self.db.write_asset(conn, asset)
            self._event(conn, asset, "restore", principal.subject_id)
            self.db.enqueue(conn, asset, self.profile, requeue=True)
            conn.execute("DELETE FROM image_trash WHERE tenant_id=? AND trash_id=?", (tenant, trash_id))
            return self._managed_row(principal, asset)

    def content(self, access, asset_id, *, thumbnail=False):
        return self._content(access, asset_id, thumbnail, self._current)

    def _content(self, access, asset_id, thumbnail, lookup, expected_revision=None):
        with self.db.transaction() as connection:
            asset = lookup(connection, access, asset_id)
            if expected_revision is not None and asset["revision_id"] != expected_revision:
                raise ConflictError("image changed before loading")
        key = asset["thumbnail_key"] if thumbnail else asset["object_key"]
        content = self.objects.get(key)
        # Object storage can be remote and slow. Recheck access and version
        # after that effect so an intervening delete/revocation is respected.
        with self.db.transaction() as connection:
            current = lookup(connection, access, asset_id)
            if current["revision_id"] != asset["revision_id"]:
                raise ConflictError("image changed while loading; retry with the current revision")
        return content, "image/jpeg" if thumbnail else asset["media_type"]

    def managed_reindex(self, principal, asset_id):
        if not principal.admin:
            raise ForbiddenError("administrator required")
        return self._managed_row(principal, self.retry(principal.access, asset_id, _principal=principal))

    def retry(self, access, asset_id, *, _principal=None):
        """Explicit retry: unknown paid calls are never retried by a poll loop."""
        with self.db.transaction(access.tenant_id + ":" + asset_id) as connection:
            asset = (self._managed_current(connection, _principal, asset_id) if _principal else
                     self._current(connection, access, asset_id))
            job_id = self.db.enqueue(connection, asset, self.profile)
            row = connection.execute("SELECT state,body FROM image_index_jobs WHERE tenant_id=? AND job_id=?", (access.tenant_id, job_id)).fetchone()
            if row["state"] == "running":
                raise ConflictError("indexing is running; wait before retrying")
            # Explicit reindex must also rebuild a lost derived index after a
            # previously successful job. Keep cached vectors to avoid another
            # embedding request when only the external index needs recovery.
            connection.execute("UPDATE image_index_jobs SET state='pending',owner=NULL WHERE tenant_id=? AND job_id=?", (access.tenant_id, job_id))
            asset["indexing_status"] = "pending"
            asset["index_profile"] = None
            asset["index_target_profile"] = self.profile
            asset["index_error"] = None
            self.db.write_current(connection, asset)
        return self._public(asset)

    @staticmethod
    def _stage(stages, name, started, output, status="ok"):
        stages.append({"name": name, "status": status,
                       "duration_ms": round((monotonic() - started) * 1000, 3), "output": output})

    async def process_pending(self, tenant_id, limit=10):
        identifier(tenant_id, "tenant_id")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValidationError("index batch limit must be between 1 and 100")
        result = {"processed": 0, "ready": 0, "deleted": 0, "failed": 0, "cancelled": 0,
                  "model_calls": 0, "embedding_requests": 0, "jobs": [],
                  "embedding_enabled": self.embedder is not None}
        for _ in range(limit):
            claimed = await asyncio.to_thread(self._claim, tenant_id, self.embedder is not None)
            if claimed is None:
                break
            job_id, owner, job, asset = claimed
            if job.get("kind") != "delete" and self.embedder is None:
                # Keep outbox durable while cloud models are disabled.
                await asyncio.to_thread(self._release, tenant_id, job_id, owner)
                break
            stages = []
            state = "done"
            try:
                if job.get("kind") == "delete":
                    started = monotonic()
                    await asyncio.to_thread(self._delete_vectors, tenant_id, job["asset_id"], job_id, owner)
                    self._stage(stages, "vector_delete", started, {"asset_id": job["asset_id"]})
                elif asset is None:
                    state = "cancelled"
                else:
                    representations = _representations(asset)
                    if not representations:
                        state = "awaiting_description"
                    else:
                        if job.get("vectors") is None:
                            started = monotonic()
                            result["embedding_requests"] += 1
                            usage = {}
                            try:
                                vectors = await self._embed(tuple(text for _, text in representations), usage)
                            except BaseException:
                                self._stage(stages, "description_embedding", started,
                                            {"usage": usage}, "error")
                                raise
                            finally:
                                result["model_calls"] += usage.get("model_calls", 0)
                                job["usage"] = usage
                            if len(vectors) != len(representations):
                                raise CapabilityError("image description embedding returned the wrong count")
                            from .vector_store import validate_vector
                            job["vectors"] = [list(validate_vector(vector)) for vector in vectors]
                            if len({len(vector) for vector in job["vectors"]}) != 1:
                                raise CapabilityError("image description embeddings have inconsistent dimensions")
                            self._stage(stages, "description_embedding", started, {"representations": len(vectors), "usage": usage})
                            await asyncio.to_thread(self._save_job, tenant_id, job_id, owner, job)
                        from .vector_store import VectorRecord
                        records = tuple(VectorRecord(record_id, tenant_id, asset["asset_id"], asset["revision_id"], self.profile, tuple(vector))
                                        for (record_id, _), vector in zip(representations, job["vectors"]))
                        started = monotonic()
                        await asyncio.to_thread(self.index.upsert, records)
                        self._stage(stages, "vector_upsert", started, {"representations": len(records)})
            except asyncio.CancelledError:
                job["error"] = {"code": "index_interrupted", "message": "Indexing was interrupted; inspect the task before explicit retry."}
                await asyncio.shield(asyncio.to_thread(self._finish, tenant_id, job_id, owner, job, "failed", stages))
                raise
            except Exception as exc:
                state = "failed"
                job["error"] = safe_error(exc)
                self._stage(stages, "index_error", monotonic(), job["error"], "error")
            state = await asyncio.to_thread(self._finish, tenant_id, job_id, owner, job, state, stages)
            result["processed"] += 1
            counter = ("deleted" if job.get("kind") == "delete" else "ready") if state == "done" else "failed" if state == "failed" else "cancelled"
            result[counter] += 1
            result["jobs"].append({"job_id": job_id, "asset_id": job["asset_id"], "state": state,
                                   "stages": stages, "error": job.get("error")})
        return result

    def _claim(self, tenant_id, indexing_enabled=True):
        with self.db.transaction(tenant_id + ":image-worker") as connection:
            # A process crash may leave running jobs. Do not infer that a paid
            # request was never accepted; surface explicit retry after a lease.
            stale = connection.execute("SELECT job_id,asset_id,revision_id,body FROM image_index_jobs WHERE tenant_id=? AND state='running' AND started_at<?", (tenant_id, time() - 900)).fetchall()
            for row in stale:
                job = json.loads(row["body"])
                if job.get("kind") != "delete" and job["profile"] != self.profile:
                    continue
                job["error"] = {"code": "index_interrupted", "message": "Indexing was interrupted; inspect provider usage before explicit retry."}
                connection.execute("UPDATE image_index_jobs SET state='failed',owner=NULL,body=? WHERE tenant_id=? AND job_id=?", (encode(job), tenant_id, row["job_id"]))
                current = self.db.current(connection, tenant_id, row["asset_id"])
                if (current and not current[1] and current[0]["revision_id"] == row["revision_id"]
                        and current[0].get("index_target_profile", self.profile) == self.profile):
                    asset = current[0] | {"indexing_status": "failed", "index_error": job["error"]}
                    self.db.write_current(connection, asset)
            candidates = connection.execute("SELECT job_id,body FROM image_index_jobs WHERE tenant_id=? AND state='pending' ORDER BY job_id", (tenant_id,)).fetchall()
            row = next((candidate for candidate in candidates
                        if json.loads(candidate["body"]).get("kind") == "delete"
                        or (indexing_enabled and json.loads(candidate["body"])["profile"] == self.profile)), None)
            if row is None:
                return None
            owner = uuid4().hex
            job = json.loads(row["body"])
            # Claim only a job that is still pending: a delete or an asset_id reuse may have
            # cancelled it since the candidates were read (row locks, not this transaction, order them on PostgreSQL).
            claimed = connection.execute("UPDATE image_index_jobs SET state='running',owner=?,started_at=? WHERE tenant_id=? AND job_id=? AND state='pending'", (owner, time(), tenant_id, row["job_id"]))
            if claimed.rowcount != 1:
                return None
            current = self.db.current(connection, tenant_id, job["asset_id"])
            asset = current[0] if (current and not current[1]
                                  and current[0]["revision_id"] == job["revision_id"]
                                  and current[0].get("index_target_profile", self.profile) == self.profile) else None
            return row["job_id"], owner, job, asset

    async def _embed(self, texts, usage):
        # CloudTransport owns actual provider attempts, including batch and
        # retry calls. A deterministic test adapter must never be counted as a
        # real provider request merely because its embed method was invoked.
        from .cloud import reset_usage, start_usage, usage_snapshot
        token = start_usage()
        try:
            return await self.embedder.embed(texts)
        finally:
            usage.update(usage_snapshot())
            reset_usage(token)

    def _release(self, tenant_id, job_id, owner):
        with self.db.transaction() as connection:
            connection.execute("UPDATE image_index_jobs SET state='pending',owner=NULL WHERE tenant_id=? AND job_id=? AND owner=?", (tenant_id, job_id, owner))

    def _delete_vectors(self, tenant, asset_id, job_id, owner):
        # The external delete is asset-wide. Serialize it with restore/reuse, even
        # when a worker outlives its lease and another process recovers the job.
        with self.db.transaction(tenant + ":" + asset_id) as conn:
            job = conn.execute("SELECT owner,state FROM image_index_jobs WHERE tenant_id=? AND job_id=?",
                               (tenant, job_id)).fetchone()
            current = self.db.current(conn, tenant, asset_id)
            if job and job["owner"] == owner and job["state"] == "running" and (not current or current[1]):
                self.index.delete(tenant, asset_id)

    def _save_job(self, tenant_id, job_id, owner, job):
        with self.db.transaction() as connection:
            cursor = connection.execute("UPDATE image_index_jobs SET body=? WHERE tenant_id=? AND job_id=? AND state='running' AND owner=?", (encode(job), tenant_id, job_id, owner))
            if cursor.rowcount != 1:
                raise ConflictError("index task was superseded")

    def _finish(self, tenant_id, job_id, owner, job, state, stages):
        with self.db.transaction(tenant_id + ":" + job["asset_id"]) as connection:
            current = self.db.current(connection, tenant_id, job["asset_id"])
            active = (current and not current[1] and current[0]["revision_id"] == job["revision_id"]
                      and current[0].get("index_target_profile", self.profile) == self.profile)
            row = connection.execute("SELECT state,owner FROM image_index_jobs WHERE tenant_id=? AND job_id=?", (tenant_id, job_id)).fetchone()
            if not row or row["owner"] != owner or row["state"] != "running":
                if current and current[1] and job.get("kind") != "delete":
                    # An old external upsert may finish after deletion cleanup.
                    # Requeue cleanup after this observed late completion.
                    connection.execute("UPDATE image_index_jobs SET state='pending',owner=NULL WHERE tenant_id=? AND job_id=? AND state<>'running'", (tenant_id, stable_id("image-delete", tenant_id, job["asset_id"])))
                return "cancelled"
            if not active and job.get("kind") != "delete":
                state = "cancelled"
            job["stages"] = stages
            connection.execute("UPDATE image_index_jobs SET state=?,owner=NULL,body=? WHERE tenant_id=? AND job_id=?", (state, encode(job), tenant_id, job_id))
            if active and job.get("kind") != "delete":
                asset = current[0] | {"indexing_status": "ready" if state == "done" else state,
                                      "index_profile": self.profile if state == "done" else None,
                                      "index_error": job.get("error") if state == "failed" else None,
                                      "index_stages": stages}
                self.db.write_current(connection, asset)
            return state

    async def search(self, access, question, top_k=5):
        nonempty(question, "question", 4000)
        if type(top_k) is not int or not 1 <= top_k <= 20:
            raise ValidationError("top_k must be between 1 and 20")
        trace = {"trace_id": uuid4().hex, "tenant_id": access.tenant_id,
                 "subject_id": access.subject_id, "question": question,
                 "search_mode": "semantic" if self.embedder else "lexical",
                 "stages": [], "hits": [], "model_calls": 0,
                 "embedding_requests": 0, "status": "running"}
        started = monotonic()
        try:
            if self.embedder:
                tick = monotonic()
                trace["embedding_requests"] += 1
                usage = {}
                try:
                    vectors = await self._embed((question,), usage)
                except BaseException:
                    self._stage(trace["stages"], "query_embedding", tick, {"usage": usage}, "error")
                    raise
                finally:
                    trace["model_calls"] += usage.get("model_calls", 0)
                    trace["usage"] = usage
                if len(vectors) != 1:
                    raise CapabilityError("query embedding returned the wrong count")
                self._stage(trace["stages"], "query_embedding", tick, {"dimensions": len(vectors[0])})
                tick = monotonic()
                matches = await asyncio.to_thread(self.index.search, access.tenant_id, self.profile, vectors[0], 1000)
                self._stage(trace["stages"], "vector_search", tick, {"candidate_count": len(matches)})
                tick = monotonic()
                hits = await asyncio.to_thread(self._hydrate, access, matches, top_k)
                self._stage(trace["stages"], "asset_hydrate", tick,
                            {"authorized_image_count": len(hits), "requested_count": top_k,
                             "candidate_limit_reached": len(matches) == 1000})
            else:
                tick = monotonic()
                hits = await asyncio.to_thread(self._lexical, access, question, top_k)
                self._stage(trace["stages"], "description_lexical_search", tick, {"image_count": len(hits)})
            trace["hits"] = hits
            trace["status"] = "ok" if hits else "empty"
        except asyncio.CancelledError:
            trace["status"] = "cancelled"
            trace["error"] = safe_error(asyncio.CancelledError())
            raise
        except Exception as exc:
            trace["status"] = "error"
            trace["error"] = safe_error(exc)
            self._stage(trace["stages"], "search_error", monotonic(), trace["error"], "error")
        finally:
            trace["duration_ms"] = round((monotonic() - started) * 1000, 3)
            # If persistence fails, callers receive an explicit storage error,
            # never a trace ID that cannot be replayed.
            await asyncio.shield(asyncio.to_thread(self._save_trace, trace))
        # Persistence may be remote. Re-read using the same current-permission
        # gate as historical replay before delivering any saved image metadata.
        return await asyncio.to_thread(self.get_trace, access, trace["trace_id"])

    def _hydrate(self, access, matches, limit):
        hits, seen = [], set()
        with self.db.transaction() as connection:
            for match in matches:
                if match.tenant_id != access.tenant_id or match.profile != self.profile or match.asset_id in seen:
                    continue
                current = self.db.current(connection, access.tenant_id, match.asset_id)
                if not current or current[1]:
                    continue
                asset = current[0]
                if not _allowed(asset, access) or asset["revision_id"] != match.revision_id or asset["indexing_status"] != "ready" or asset["index_profile"] != self.profile:
                    continue
                representations = dict(_representations(asset))
                if match.record_id not in representations:
                    continue
                if not isinstance(match.score, (int, float)) or not math.isfinite(match.score):
                    raise CapabilityError("vector index returned an invalid score")
                seen.add(match.asset_id)
                hits.append({"asset": self._public(asset), "score": float(match.score),
                             "matched_text": representations[match.record_id], "representation_id": match.record_id})
                if len(hits) >= limit:
                    break
        return hits

    def _lexical(self, access, question, limit):
        query = set(tokenize(question))
        scored = []
        for asset in self.list(access):
            best = None
            for record_id, text in _representations(asset):
                counts = Counter(tokenize(text))
                overlap = sum(min(counts[word], 3) for word in query)
                if overlap:
                    score = overlap / (1 + math.log1p(sum(counts.values())))
                    if best is None or score > best[0]:
                        best = score, record_id, text
            if best:
                scored.append({"asset": asset, "score": best[0], "representation_id": best[1], "matched_text": best[2]})
        scored.sort(key=lambda hit: (-hit["score"], hit["asset"]["asset_id"]))
        return scored[:limit]

    def _save_trace(self, trace):
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO image_traces(tenant_id,trace_id,body) VALUES(?,?,?)", (trace["tenant_id"], trace["trace_id"], encode(trace)))

    def get_trace(self, access, trace_id):
        identifier(trace_id, "trace_id")
        with self.db.transaction() as connection:
            row = connection.execute("SELECT body FROM image_traces WHERE tenant_id=? AND trace_id=?", (access.tenant_id, trace_id)).fetchone()
            if not row:
                raise NotFoundError("image trace not found")
            trace = json.loads(row["body"])
            if trace["subject_id"] != access.subject_id:
                raise NotFoundError("image trace not found")
            for hit in trace["hits"]:
                current = self._current(connection, access, hit["asset"]["asset_id"])
                if current["revision_id"] != hit["asset"]["revision_id"]:
                    raise NotFoundError("image trace sources changed or are no longer accessible")
        return {key: value for key, value in trace.items() if key not in ("tenant_id", "subject_id")}
