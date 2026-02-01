# commands/components/content/create_with_uploads.py
import os
import re
import json
import mimetypes
import time
import asyncio
from uuid import uuid4
from typing import Optional, List, Any
from uuid import UUID

from fastapi import UploadFile, HTTPException
from pydantic import BaseModel, field_validator

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs  # your singleton
from models.components.tbl_comp_contents import CompContent

# Prefer your app logger if available; fallback to stdlib
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("create_with_uploads")
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)


# ---------- small utilities copied from your content_create.py ----------
_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def make_uuid_name(filename: str, default_stem: str) -> str:
    name = filename or ""
    stem, ext = os.path.splitext(name)
    stem = (stem or default_stem).strip()
    stem = _SAFE_CHARS_RE.sub("_", stem) or default_stem
    ext = (ext or "").lower().lstrip(".") or "bin"
    return f"{stem}.{uuid4()}.{ext}"


_FALLBACK_MIME = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _resolve_content_type(upload: UploadFile) -> str:
    if getattr(upload, "content_type", None):
        return upload.content_type
    name = getattr(upload, "filename", "") or ""
    ext = os.path.splitext(name)[1].lower()
    if ext in _FALLBACK_MIME:
        return _FALLBACK_MIME[ext]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _normalize_tags(value) -> List[str]:
    """
    Accept a single comma-separated string like "a, b, c"
    (still tolerant of lists/tuples/sets just in case),
    return a trimmed, de-duplicated list (original casing).
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(p).strip() for p in value]
    else:
        parts = []

    items, seen = [], set()
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            items.append(p)
    return items


# ---------- Payload (reuse / extend your existing payload) ----------
class CreateCompContentsPayload(BaseModel):
    name: str
    description: Optional[str] = None
    template_id: Optional[UUID] = None

    # ✅ NEW
    group_id: Optional[UUID] = None
    sub_type: Optional[str] = None

    tags: Optional[str] = None
    metadata: Optional[Any] = None

    @field_validator("name")
    @classmethod
    def _name_trim(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name is required")
        return v

    @field_validator("description", mode="before")
    @classmethod
    def _strip_optional(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("tags", mode="before")
    @classmethod
    def _tags_in(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("sub_type", mode="before")
    @classmethod
    def _sub_type_in(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    # ✅ important for multipart/form-data: "" -> None
    @field_validator("template_id", "group_id", mode="before")
    @classmethod
    def _uuid_empty_to_none(cls, v):
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata_in(cls, v):
        """
        Accept dict, None, or JSON string and normalize to dict/None.

        Important for multipart/form-data (FormData sends strings).
        """
        if v is None or isinstance(v, dict):
            return v

        if isinstance(v, str):
            raw = v.strip()
            if not raw:
                return None
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"metadata must be valid JSON if provided as string: {e}")

            if isinstance(parsed, dict):
                return parsed

            return {"value": parsed}

        try:
            return dict(v)
        except Exception:
            raise ValueError("metadata must be a JSON object or JSON string")


# ---------- Command ----------
class CreateCompContentsWithUploadsCommand(BaseCommand):
    """
    Handles creation of a CompContent row with optional file uploads:
      - thumbnail: single image UploadFile
      - images: list of image UploadFile (multi-file)
      - attachment: single file UploadFile
    """

    name = "components/contents/create_with_uploads"
    schema = CreateCompContentsPayload
    require_auth = True
    method = "post"
    type = "file_upload"
    group = "Content"

    file_fields = [
        ("thumbnail", False),
        ("images", True),
        ("attachment", False),
    ]

    base_folder = "uploads"

    MAX_IMAGE_MB = 50
    MAX_FILE_MB = 200
    IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
    ANY_FILE_TYPES = None

    async def execute(
        self,
        payload: CreateCompContentsPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        attachment: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ):
        start_t = time.monotonic()
        logger.info(
            "[create_with_uploads] start - name=%s user_id=%s group_id=%s sub_type=%s",
            getattr(payload, "name", None),
            user_id,
            getattr(payload, "group_id", None),
            getattr(payload, "sub_type", None),
        )

        gcs = get_gcs()

        # NOTE: this folder id is separate from DB row id (fine; used for upload grouping)
        content_id = str(uuid4())
        dest_prefix = f"{self.base_folder}/{content_id}"
        logger.debug("[create_with_uploads] content_id=%s dest_prefix=%s", content_id, dest_prefix)

        thumbnail_key: Optional[str] = None
        images_keys: Optional[List[str]] = None
        attachment_key: Optional[str] = None

        async def _read_and_check(f: UploadFile, max_mb: int, allowed: Optional[set]):
            if f is None:
                return None, None
            t0 = time.monotonic()
            logger.debug("[create_with_uploads] _read_and_check start file=%s", getattr(f, "filename", None))
            data = await f.read()
            try:
                f.file.seek(0)
            except Exception:
                pass
            ct = _resolve_content_type(f)
            size = len(data)
            logger.debug(
                "[create_with_uploads] _read_and_check file=%s size=%d ct=%s elapsed=%.3fs",
                getattr(f, "filename", None),
                size,
                ct,
                time.monotonic() - t0,
            )
            if size > max_mb * 1024 * 1024:
                raise ValueError(f"File '{f.filename}' exceeds {max_mb}MB limit")
            if allowed is not None and ct not in allowed:
                raise ValueError(f"Unsupported content type '{ct}' for '{f.filename}'")
            return data, ct

        # Upload thumbnail
        if thumbnail:
            logger.info("[create_with_uploads] processing thumbnail=%s", getattr(thumbnail, "filename", None))
            _, thumb_ct = await _read_and_check(thumbnail, self.MAX_IMAGE_MB, self.IMAGE_TYPES)
            thumb_name = make_uuid_name(thumbnail.filename, "thumbnail")
            key_prefix = f"{dest_prefix}/thumbnail"
            t0 = time.monotonic()
            res = await asyncio.to_thread(
                gcs.upload_fileobj,
                thumbnail.file,
                thumb_name,
                key_prefix,
                False,
                thumb_ct,
            ) if hasattr(gcs, "upload_fileobj") else await asyncio.to_thread(
                gcs.upload_fileobj,
                fileobj=thumbnail.file,
                filename=thumb_name,
                dest_prefix=key_prefix,
                public=False,
                content_type=thumb_ct,
            )
            logger.info("[create_with_uploads] thumbnail uploaded elapsed=%.3fs ok=%s", time.monotonic() - t0, res.get("ok"))
            thumbnail_key = res["key"]

        # Upload images
        if images:
            logger.info("[create_with_uploads] processing %d images", len(images))
            for idx, img in enumerate(images):
                _, img_ct = await _read_and_check(img, self.MAX_IMAGE_MB, self.IMAGE_TYPES)
                img_name = make_uuid_name(img.filename, f"img{idx:03d}")
                key_prefix = f"{dest_prefix}/images"
                t0 = time.monotonic()
                res = await asyncio.to_thread(
                    gcs.upload_fileobj,
                    img.file,
                    img_name,
                    key_prefix,
                    False,
                    img_ct,
                ) if hasattr(gcs, "upload_fileobj") else await asyncio.to_thread(
                    gcs.upload_fileobj,
                    fileobj=img.file,
                    filename=img_name,
                    dest_prefix=key_prefix,
                    public=False,
                    content_type=img_ct,
                )
                if images_keys is None:
                    images_keys = []
                images_keys.append(res["key"])
                logger.info("[create_with_uploads] image idx=%d uploaded elapsed=%.3fs ok=%s", idx, time.monotonic() - t0, res.get("ok"))

        # Upload attachment
        if attachment:
            logger.info("[create_with_uploads] processing attachment=%s", getattr(attachment, "filename", None))
            _, att_ct = await _read_and_check(attachment, self.MAX_FILE_MB, self.ANY_FILE_TYPES)
            att_name = make_uuid_name(attachment.filename, "file")
            key_prefix = f"{dest_prefix}/files"
            t0 = time.monotonic()
            res = await asyncio.to_thread(
                gcs.upload_fileobj,
                attachment.file,
                att_name,
                key_prefix,
                False,
                att_ct,
            ) if hasattr(gcs, "upload_fileobj") else await asyncio.to_thread(
                gcs.upload_fileobj,
                fileobj=attachment.file,
                filename=att_name,
                dest_prefix=key_prefix,
                public=False,
                content_type=att_ct,
            )
            attachment_key = res["key"]
            logger.info("[create_with_uploads] attachment uploaded elapsed=%.3fs ok=%s", time.monotonic() - t0, res.get("ok"))

        # Persist DB row
        db = SessionLocal()
        try:
            if self.require_auth and user_id is None:
                logger.warning("[create_with_uploads] unauthorized call - no user context")
                raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

            # ✅ created_by/updated_by are Integer columns
            created_by = None
            updated_by = None
            if user_id is not None:
                try:
                    created_by = int(user_id)
                    updated_by = int(user_id)
                except Exception:
                    raise HTTPException(status_code=400, detail="Invalid user context (user_id must be int-like).")

            row = CompContent(
                name=payload.name,
                description=payload.description,
                thumbnail=thumbnail_key,
                images=images_keys,
                template_id=payload.template_id,
                file_link=attachment_key,
                created_by=created_by,
                updated_by=updated_by,
                tags=_normalize_tags(payload.tags),
                metadata_json=payload.metadata or None,

                # ✅ NEW
                group_id=payload.group_id,
                sub_type=payload.sub_type,
            )

            logger.debug(
                "[create_with_uploads] inserting DB row name=%s created_by=%s group_id=%s sub_type=%s",
                payload.name,
                created_by,
                payload.group_id,
                payload.sub_type,
            )

            t0 = time.monotonic()
            db.add(row)
            db.commit()
            db.refresh(row)
            logger.info("[create_with_uploads] DB commit elapsed=%.3fs id=%s", time.monotonic() - t0, getattr(row, "id", None))

            signed_thumb = None
            signed_imgs = []
            signed_attach = None

            try:
                if thumbnail_key:
                    signed_thumb = gcs.signed_get_url(thumbnail_key, expires_seconds=3600)
            except Exception:
                pass

            try:
                for k in images_keys or []:
                    try:
                        signed_imgs.append(gcs.signed_get_url(k, expires_seconds=3600))
                    except Exception:
                        signed_imgs.append(f"https://storage.googleapis.com/{gcs.bucket_name}/{k}")
            except Exception:
                pass

            try:
                if attachment_key:
                    signed_attach = gcs.signed_get_url(attachment_key, expires_seconds=3600)
            except Exception:
                pass

            return {
                "status": "ok",
                "data": _comp_contents_to_dict(row),
                "gcs": {
                    "thumbnail": signed_thumb,
                    "images": signed_imgs,
                    "attachment": signed_attach,
                    "content_id": content_id,
                },
            }

        except Exception:
            logger.exception("[create_with_uploads] DB error - rolling back")
            db.rollback()
            raise
        finally:
            db.close()
            try:
                if thumbnail and getattr(thumbnail, "file", None) and not thumbnail.file.closed:
                    thumbnail.file.close()
            except Exception:
                pass
            try:
                for uf in images or []:
                    if uf and getattr(uf, "file", None) and not uf.file.closed:
                        uf.file.close()
            except Exception:
                pass
            try:
                if attachment and getattr(attachment, "file", None) and not attachment.file.closed:
                    attachment.file.close()
            except Exception:
                pass

        logger.info("[create_with_uploads] finished total_elapsed=%.3fs", time.monotonic() - start_t)


def _comp_contents_to_dict(m: CompContent) -> dict:
    return {
        "id": str(m.id) if getattr(m, "id", None) is not None else None,
        "name": m.name,
        "description": m.description,
        "thumbnail": m.thumbnail,
        "images": m.images,
        "template_id": str(m.template_id) if getattr(m, "template_id", None) else None,
        "file_link": m.file_link,
        "tags": getattr(m, "tags", []) or [],
        "metadata": getattr(m, "metadata_json", None) or {},

        # ✅ NEW
        "group_id": str(getattr(m, "group_id", None)) if getattr(m, "group_id", None) else None,
        "sub_type": getattr(m, "sub_type", None),

        "created_by": m.created_by,
        "updated_by": m.updated_by,
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }
