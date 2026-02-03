from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import time
from typing import Any, List, Literal, Optional
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_layouts import CompLayout

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.layouts.update_with_uploads")
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
    out: List[str] = []
    seen = set()
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


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
    if size == 0:
        raise HTTPException(status_code=400, detail=f"File '{upload.filename}' is empty")
    if size > max_mb * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"File '{upload.filename}' exceeds {max_mb}MB limit")
    if allowed_types is not None and ct not in allowed_types:
        raise HTTPException(status_code=400, detail=f"Unsupported content type '{ct}' for '{upload.filename}'")
    try:
        upload.file.seek(0)
    except Exception:
        pass
    return ct


def should_update_field(payload: BaseModel, field: str) -> bool:
    fields_set = getattr(payload, "__pydantic_fields_set__", set())
    return field in fields_set


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


class UpdateCompLayoutsPayload(BaseModel):
    """Update a layout and optionally upload thumbnail/images/file_link."""

    id: str = Field(description="Layout id (UUID)")
    name: Optional[str] = Field(default=None, description="Layout name")
    description: Optional[str] = Field(default=None, description="Layout description")

    group_id: Optional[UUID] = Field(default=None, description="Group id")
    sub_type: Optional[str] = Field(default=None, description="Layout subtype")

    tags: Optional[str] = Field(default=None, description="Comma-separated tags")
    tags_mode: Literal["append", "replace", "remove"] = Field(default="replace", description="Tag update mode")
    tags_clear: bool = Field(default=False, description="Clear all tags")

    images_mode: Literal["append", "replace"] = Field(default="append", description="Image update mode")

    thumbnail_clear: bool = Field(default=False, description="Clear thumbnail")
    images_clear: bool = Field(default=False, description="Clear images")
    file_link_clear: bool = Field(default=False, description="Clear file link")

    metadata: Optional[Any] = Field(default=None, description="Metadata JSON object or JSON string")

    @field_validator("id")
    @classmethod
    def id_trim(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("id is required")
        return s

    @field_validator("name")
    @classmethod
    def name_trim(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("description", mode="before")
    @classmethod
    def desc_trim(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("sub_type", mode="before")
    @classmethod
    def sub_type_in(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("tags", mode="before")
    @classmethod
    def tags_in(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("group_id", mode="before")
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


class UpdateLayoutWithUploadsCommand(BaseCommand):
    """Update a CompLayout and optionally upload thumbnail/images/file_link (store GCS keys)."""

    name = "components/layouts/update_with_uploads"
    schema = UpdateCompLayoutsPayload
    require_auth = True
    method = "put"
    type = "file_upload"
    group = "Layout"

    file_fields = [
        ("thumbnail", False),
        ("images", True),
        ("file_link", False),
    ]

    base_folder = "uploads"
    max_image_mb = 50
    max_file_mb = 200
    image_types = {"image/jpeg", "image/png", "image/webp"}

    async def execute(
        self,
        payload: UpdateCompLayoutsPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        file_link: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        start_t = time.monotonic()
        logger.info(
            "[layouts.update] start id=%s user_id=%s group_id=%s sub_type=%s",
            payload.id,
            user_id,
            payload.group_id,
            payload.sub_type,
        )

        db = SessionLocal()
        try:
            row: Optional[CompLayout] = db.get(CompLayout, payload.id)
            if not row:
                raise HTTPException(status_code=404, detail="Layout not found")

            gcs = get_gcs()
            dest_prefix = f"{self.base_folder}/{row.id}"

            if payload.name is not None and payload.name.strip():
                row.name = payload.name.strip()
            if payload.description is not None:
                row.description = payload.description

            if should_update_field(payload, "group_id"):
                row.group_id = payload.group_id
            if should_update_field(payload, "sub_type"):
                row.sub_type = payload.sub_type

            current_tags: List[str] = list(getattr(row, "tags", None) or [])
            if payload.tags_clear:
                current_tags = []

            incoming = normalize_tags(payload.tags)
            if incoming:
                if payload.tags_mode == "replace":
                    current_tags = incoming
                elif payload.tags_mode == "append":
                    seen = set(current_tags)
                    for t in incoming:
                        if t not in seen:
                            current_tags.append(t)
                            seen.add(t)
                else:
                    remove_set = set(incoming)
                    current_tags = [t for t in current_tags if t not in remove_set]

            if payload.tags_clear or payload.tags is not None or getattr(row, "tags", None) is None:
                row.tags = current_tags

            if should_update_field(payload, "metadata") and payload.metadata is not None:
                row.metadata_json = payload.metadata or None

            if payload.thumbnail_clear:
                row.thumbnail = None
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
                row.thumbnail = res.get("key")

            current_images: List[str] = list(getattr(row, "images", None) or [])
            if payload.images_clear:
                current_images = []

            new_keys: List[str] = []
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
                        new_keys.append(k)

            if new_keys:
                if payload.images_mode == "replace":
                    current_images = new_keys
                else:
                    seen = set(current_images)
                    for k in new_keys:
                        if k not in seen:
                            current_images.append(k)
                            seen.add(k)

            if payload.images_clear or new_keys or getattr(row, "images", None) is None:
                row.images = current_images

            if payload.file_link_clear:
                row.file_link = None
            if file_link:
                ct = validate_upload(file_link, max_mb=self.max_file_mb, allowed_types=None)
                fname = make_uuid_name(file_link.filename, "file")
                res = await asyncio.to_thread(
                    gcs.upload_fileobj,
                    file_link.file,
                    fname,
                    f"{dest_prefix}/files",
                    False,
                    ct,
                )
                row.file_link = res.get("key")

            if user_id is not None:
                try:
                    row.updated_by = int(user_id)
                except Exception as e:
                    raise HTTPException(status_code=400, detail="Invalid user context (user_id must be int-like).") from e

            db.add(row)
            db.commit()
            db.refresh(row)

            signed_thumb: Optional[str] = None
            signed_imgs: List[str] = []
            signed_file: Optional[str] = None

            if row.thumbnail:
                try:
                    signed_thumb = gcs.signed_get_url(row.thumbnail, expires_seconds=3600)
                except Exception:
                    signed_thumb = f"https://storage.googleapis.com/{gcs.bucket_name}/{row.thumbnail}"

            for k in row.images or []:
                try:
                    signed_imgs.append(gcs.signed_get_url(k, expires_seconds=3600))
                except Exception:
                    signed_imgs.append(f"https://storage.googleapis.com/{gcs.bucket_name}/{k}")

            if row.file_link:
                try:
                    signed_file = gcs.signed_get_url(row.file_link, expires_seconds=3600)
                except Exception:
                    signed_file = f"https://storage.googleapis.com/{gcs.bucket_name}/{row.file_link}"

            logger.info("[layouts.update] finished elapsed=%.3fs id=%s", time.monotonic() - start_t, row.id)

            return {
                "status": "ok",
                "data": comp_layout_to_dict(row),
                "gcs": {
                    "thumbnail": signed_thumb,
                    "images": signed_imgs,
                    "file_link": signed_file,
                    "layout_folder": str(row.id),
                },
            }
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("[layouts.update] error")
            raise HTTPException(status_code=500, detail="Failed to update layout")
        finally:
            db.close()
            for uf in [thumbnail, *(images or []), file_link]:
                try:
                    if uf and getattr(uf, "file", None) and not uf.file.closed:
                        uf.file.close()
                except Exception:
                    pass
