import os
import re
import json
import mimetypes
import time
import asyncio
from uuid import uuid4
from typing import Optional, List, Any, Dict
from fastapi import UploadFile, HTTPException
from pydantic import BaseModel, field_validator
from uuid import UUID

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_pages import CompPage

# Prefer your app logger if available; fallback to stdlib
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("pages_create_with_uploads")
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)

_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FALLBACK_MIME = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def make_uuid_name(filename: str, default_stem: str) -> str:
    name = filename or ""
    stem, ext = os.path.splitext(name)
    stem = (stem or default_stem).strip()
    stem = _SAFE_CHARS_RE.sub("_", stem) or default_stem
    ext = (ext or "").lower().lstrip(".") or "bin"
    return f"{stem}.{uuid4()}.{ext}"


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


class CreateCompPagesPayload(BaseModel):
    name: str
    description: Optional[str] = None
    template_id: Optional[UUID] = None
    # tags is a single comma-separated string (e.g., "a, b, c")
    tags: Optional[str] = None
    # metadata for metadata_json JSONB (dict in code, JSON string allowed over the wire)
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
        return str(v).strip()

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata_in(cls, v):
        """
        Accept dict, None, or JSON string and normalize to dict/None.

        This matches contents/layouts/auth semantics so multipart FormData can send:

            metadata = '{"layoutType":"map","outputs":[...]}'
        """
        # Already correct
        if v is None or isinstance(v, dict):
            return v

        # JSON string from FormData
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

            # valid JSON but not an object → wrap to keep column shape consistent
            return {"value": parsed}

        # Fallback: best-effort cast to dict
        try:
            return dict(v)
        except Exception:
            raise ValueError("metadata must be a JSON object or JSON string")


class CreateCompPagesWithUploadsCommand(BaseCommand):
    """
    Creates a CompPage with optional uploads:
      - thumbnail: UploadFile (single)
      - images: List[UploadFile]
      - attachment: UploadFile -> stored as file_link
    """
    name = "components/pages/create_with_uploads"
    schema = CreateCompPagesPayload
    require_auth = True
    method = "post"
    type = "file_upload"
    group = "Page"

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
        payload: CreateCompPagesPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        attachment: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ):
        t0 = time.monotonic()
        logger.info("[pages] start name=%s user_id=%s", getattr(payload, "name", None), user_id)
        gcs = get_gcs()

        page_id = str(uuid4())
        dest_prefix = f"{self.base_folder}/{page_id}"

        thumbnail_key: Optional[str] = None
        images_keys: Optional[List[str]] = None
        attachment_key: Optional[str] = None

        async def _read_and_check(f: UploadFile, max_mb: int, allowed: Optional[set]):
            if f is None:
                return None, None
            data = await f.read()
            try:
                f.file.seek(0)
            except Exception:
                pass
            ct = _resolve_content_type(f)
            size = len(data)
            if size > max_mb * 1024 * 1024:
                raise ValueError(f"File '{f.filename}' exceeds {max_mb}MB limit")
            if allowed is not None and ct not in allowed:
                raise ValueError(f"Unsupported content type '{ct}' for '{f.filename}'")
            return data, ct

        # thumbnail
        if thumbnail:
            _, ct = await _read_and_check(thumbnail, self.MAX_IMAGE_MB, self.IMAGE_TYPES)
            fname = make_uuid_name(thumbnail.filename, "thumbnail")
            res = await asyncio.to_thread(
                gcs.upload_fileobj, thumbnail.file, fname, f"{dest_prefix}/thumbnail", False, ct
            )
            thumbnail_key = res["key"]

        # images
        if images:
            images_keys = []
            for idx, img in enumerate(images):
                _, ct = await _read_and_check(img, self.MAX_IMAGE_MB, self.IMAGE_TYPES)
                fname = make_uuid_name(img.filename, f"img{idx:03d}")
                res = await asyncio.to_thread(
                    gcs.upload_fileobj, img.file, fname, f"{dest_prefix}/images", False, ct
                )
                images_keys.append(res["key"])

        # attachment
        if attachment:
            _, ct = await _read_and_check(attachment, self.MAX_FILE_MB, self.ANY_FILE_TYPES)
            fname = make_uuid_name(attachment.filename, "file")
            res = await asyncio.to_thread(
                gcs.upload_fileobj, attachment.file, fname, f"{dest_prefix}/files", False, ct
            )
            attachment_key = res["key"]

        db = SessionLocal()
        try:
            if self.require_auth and not user_id:
                raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

            row = CompPage(
                name=payload.name,
                description=payload.description,
                thumbnail=thumbnail_key,
                images=images_keys,
                template_id=payload.template_id,
                file_link=attachment_key,
                created_by=user_id,
                updated_by=user_id,
                tags=_normalize_tags(payload.tags),
                # store normalized dict into JSONB column
                metadata_json=payload.metadata or None,
            )

            db.add(row)
            db.commit()
            db.refresh(row)

            # signed urls (best-effort)
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
                "data": _comp_pages_to_dict(row),
                "gcs": {
                    "thumbnail": signed_thumb,
                    "images": signed_imgs,
                    "attachment": signed_attach,
                    "page_id": page_id,
                },
            }
        except Exception:
            db.rollback()
            logger.exception("[pages] DB error")
            raise
        finally:
            db.close()
            for uf in [thumbnail] + (images or []) + [attachment]:
                try:
                    if uf and getattr(uf, "file", None) and not uf.file.closed:
                        uf.file.close()
                except Exception:
                    pass


def _comp_pages_to_dict(m: CompPage) -> dict:
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
        "created_by": m.created_by,
        "updated_by": m.updated_by,
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }
