from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import time
from typing import Any, List, Optional
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_layouts import CompLayout

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.layouts.create_with_uploads")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


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


def resolve_content_type(upload: UploadFile) -> str:
    ct = (getattr(upload, "content_type", None) or "").strip()
    if ct:
        return ct

    name = getattr(upload, "filename", "") or ""
    ext = os.path.splitext(name)[1].lower()
    if ext in _FALLBACK_MIME:
        return _FALLBACK_MIME[ext]

    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def normalize_tags(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(p).strip() for p in value]
    else:
        parts = []

    items: List[str] = []
    seen = set()
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            items.append(p)
    return items


def file_size_bytes(upload: UploadFile) -> Optional[int]:
    f = getattr(upload, "file", None)
    if f is None:
        return None
    try:
        cur = f.tell()
        f.seek(0, os.SEEK_END)
        end = f.tell()
        f.seek(cur, os.SEEK_SET)
        return int(end)
    except Exception:
        try:
            f.seek(0, os.SEEK_SET)
        except Exception:
            return None
        return None


def validate_upload(upload: UploadFile, *, max_mb: int, allowed_types: Optional[set[str]]) -> str:
    ct = resolve_content_type(upload)
    size = file_size_bytes(upload)

    if size is None:
        raise HTTPException(status_code=400, detail=f"Unable to determine file size for '{upload.filename}'")

    if size > max_mb * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"File '{upload.filename}' exceeds {max_mb}MB limit")

    if allowed_types is not None and ct not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported content type '{ct}' for '{upload.filename}'",
        )

    try:
        upload.file.seek(0)
    except Exception:
        pass

    return ct


class CreateCompLayoutsPayload(BaseModel):
    name: str = Field(description="Layout name")
    description: Optional[str] = Field(default=None, description="Layout description")
    template_id: Optional[UUID] = Field(default=None, description="Template id")

    group_id: Optional[UUID] = Field(default=None, description="Group id")
    sub_type: Optional[str] = Field(default=None, description="Layout subtype")

    tags: Optional[str] = Field(default=None, description="Comma-separated tags")
    metadata: Optional[Any] = Field(default=None, description="Metadata JSON object or JSON string")

    @field_validator("name")
    @classmethod
    def name_trim(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name is required")
        return v

    @field_validator("description", mode="before")
    @classmethod
    def strip_optional(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("sub_type", "tags", mode="before")
    @classmethod
    def strip_to_none(cls, v: Any) -> Any:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("template_id", "group_id", mode="before")
    @classmethod
    def uuid_empty_to_none(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("metadata", mode="before")
    @classmethod
    def metadata_in(cls, v: Any) -> Any:
        if v is None or isinstance(v, dict):
            return v

        if isinstance(v, str):
            raw = v.strip()
            if not raw:
                return None
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"metadata must be valid JSON if provided as string: {e}") from e

            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}

        try:
            return dict(v)
        except Exception as e:
            raise ValueError("metadata must be a JSON object or JSON string") from e


class CreateCompLayoutsWithUploadsCommand(BaseCommand):
    """Create a CompLayout with optional uploads (thumbnail, images, attachment)."""

    name = "components/layouts/create_with_uploads"
    schema = CreateCompLayoutsPayload
    require_auth = True
    method = "post"
    type = "file_upload"
    group = "Layout"

    file_fields = [
        ("thumbnail", False),
        ("images", True),
        ("attachment", False),
    ]

    base_folder = "uploads"
    max_image_mb = 50
    max_file_mb = 200
    image_types = {"image/jpeg", "image/png", "image/webp"}

    async def execute(
        self,
        payload: CreateCompLayoutsPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        attachment: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        start_t = time.monotonic()
        logger.info(
            "[layouts] start name=%s user_id=%s group_id=%s sub_type=%s",
            payload.name,
            user_id,
            payload.group_id,
            payload.sub_type,
        )

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        created_by: Optional[int] = None
        if user_id is not None:
            try:
                created_by = int(user_id)
            except Exception as e:
                raise HTTPException(status_code=400, detail="Invalid user context (user_id must be int-like).") from e

        gcs = get_gcs()
        layout_folder_id = str(uuid4())
        dest_prefix = f"{self.base_folder}/{layout_folder_id}"

        thumbnail_key: Optional[str] = None
        images_keys: List[str] = []
        attachment_key: Optional[str] = None

        try:
            if thumbnail:
                ct = validate_upload(thumbnail, max_mb=self.max_image_mb, allowed_types=self.image_types)
                fname = make_uuid_name(thumbnail.filename, "thumbnail")
                res = await asyncio.to_thread(
                    gcs.upload_fileobj,
                    thumbnail.file,
                    fname,
                    f"{dest_prefix}/thumbnail",
                    False,
                    ct,
                )
                thumbnail_key = res.get("key")

            if images:
                for idx, img in enumerate(images):
                    if not img:
                        continue
                    ct = validate_upload(img, max_mb=self.max_image_mb, allowed_types=self.image_types)
                    fname = make_uuid_name(img.filename, f"img{idx:03d}")
                    res = await asyncio.to_thread(
                        gcs.upload_fileobj,
                        img.file,
                        fname,
                        f"{dest_prefix}/images",
                        False,
                        ct,
                    )
                    k = res.get("key")
                    if k:
                        images_keys.append(k)

            if attachment:
                ct = validate_upload(attachment, max_mb=self.max_file_mb, allowed_types=None)
                fname = make_uuid_name(attachment.filename, "file")
                res = await asyncio.to_thread(
                    gcs.upload_fileobj,
                    attachment.file,
                    fname,
                    f"{dest_prefix}/files",
                    False,
                    ct,
                )
                attachment_key = res.get("key")

            db = SessionLocal()
            try:
                row = CompLayout(
                    name=payload.name,
                    description=payload.description,
                    thumbnail=thumbnail_key,
                    images=images_keys or None,
                    template_id=payload.template_id,
                    file_link=attachment_key,
                    created_by=created_by,
                    updated_by=created_by,
                    tags=normalize_tags(payload.tags),
                    metadata_json=payload.metadata or None,
                    group_id=payload.group_id,
                    sub_type=payload.sub_type,
                )

                db.add(row)
                db.commit()
                db.refresh(row)
            except Exception:
                db.rollback()
                logger.exception("[layouts] DB error")
                raise
            finally:
                db.close()

            signed_thumb: Optional[str] = None
            signed_imgs: List[str] = []
            signed_attach: Optional[str] = None

            if thumbnail_key:
                try:
                    signed_thumb = gcs.signed_get_url(thumbnail_key, expires_seconds=3600)
                except Exception:
                    signed_thumb = f"https://storage.googleapis.com/{gcs.bucket_name}/{thumbnail_key}"

            for k in images_keys:
                try:
                    signed_imgs.append(gcs.signed_get_url(k, expires_seconds=3600))
                except Exception:
                    signed_imgs.append(f"https://storage.googleapis.com/{gcs.bucket_name}/{k}")

            if attachment_key:
                try:
                    signed_attach = gcs.signed_get_url(attachment_key, expires_seconds=3600)
                except Exception:
                    signed_attach = f"https://storage.googleapis.com/{gcs.bucket_name}/{attachment_key}"

            logger.info(
                "[layouts] finished total_elapsed=%.3fs id=%s",
                time.monotonic() - start_t,
                getattr(row, "id", None),
            )

            return {
                "status": "ok",
                "data": comp_layout_to_dict(row),
                "gcs": {
                    "thumbnail": signed_thumb,
                    "images": signed_imgs,
                    "attachment": signed_attach,
                    "layout_id": layout_folder_id,
                },
            }
        finally:
            for uf in [thumbnail, *(images or []), attachment]:
                try:
                    if uf and getattr(uf, "file", None) and not uf.file.closed:
                        uf.file.close()
                except Exception:
                    pass


def comp_layout_to_dict(m: CompLayout) -> dict:
    return {
        "id": str(m.id) if getattr(m, "id", None) is not None else None,
        "name": m.name,
        "description": m.description,
        "group_id": str(getattr(m, "group_id", None)) if getattr(m, "group_id", None) else None,
        "sub_type": getattr(m, "sub_type", None),
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
