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
from models.components.tbl_comp_pages import CompPage

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.pages.update_with_uploads")
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


def safe_stem(name: str, default_stem: str) -> str:
    stem, _ = os.path.splitext(name or "")
    stem = (stem or default_stem).strip()
    stem = _SAFE_CHARS_RE.sub("_", stem) or default_stem
    return stem


def make_uuid_name(filename: str, default_stem: str) -> str:
    stem = safe_stem(filename, default_stem)
    ext = (os.path.splitext(filename or "")[1] or "").lower().lstrip(".") or "bin"
    return f"{stem}.{uuid4()}.{ext}"


def resolve_content_type(upload: UploadFile) -> str:
    if getattr(upload, "content_type", None):
        return upload.content_type
    name = getattr(upload, "filename", "") or ""
    ext = os.path.splitext(name)[1].lower()
    if ext in _FALLBACK_MIME:
        return _FALLBACK_MIME[ext]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def normalize_tags(value: Any) -> List[str]:
    if value is None:
        return []

    parts: List[str]
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


def file_size_bytes(upload: UploadFile, max_bytes: int) -> int:
    f = getattr(upload, "file", None)
    if f is None:
        return 0

    try:
        cur = f.tell()
        f.seek(0, os.SEEK_END)
        size = int(f.tell())
        f.seek(cur, os.SEEK_SET)
        return size
    except Exception:
        pass

    try:
        cur = f.tell()
    except Exception:
        cur = None

    total = 0
    try:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return total
    finally:
        try:
            if cur is not None:
                f.seek(cur)
        except Exception:
            pass

    return total


def sign_url_maybe(gcs, key: Optional[str]) -> Optional[str]:
    if not key:
        return key
    try:
        return gcs.signed_get_url(key, expires_seconds=3600)
    except Exception:
        try:
            return f"https://storage.googleapis.com/{gcs.bucket_name}/{key}"
        except Exception:
            return key


class UpdateCompPagesPayload(BaseModel):
    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    group_id: Optional[UUID] = None
    sub_type: Optional[str] = None
    tags: Optional[str] = None
    tags_mode: Literal["append", "replace", "remove"] = Field(default="replace")
    tags_clear: bool = False
    images_mode: Literal["append", "replace"] = Field(default="append")
    thumbnail_clear: bool = False
    images_clear: bool = False
    file_link_clear: bool = False
    metadata: Optional[Any] = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("id is required")
        UUID(s)
        return s

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("sub_type", mode="before")
    @classmethod
    def normalize_sub_type(cls, v: Any) -> Optional[str]:
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

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags_str(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("metadata", mode="before")
    @classmethod
    def metadata_to_dict(cls, v: Any) -> Any:
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


class UpdatePageWithUploadsCommand(BaseCommand):
    name = "components/pages/update_with_uploads"
    schema = UpdateCompPagesPayload
    require_auth = True
    method = "put"
    type = "file_upload"
    group = "Page"

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
        payload: UpdateCompPagesPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        file_link: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        start_t = time.monotonic()
        logger.info(
            "[pages.update] start id=%s user_id=%s group_id=%s sub_type=%s",
            payload.id,
            user_id,
            payload.group_id,
            payload.sub_type,
        )

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        try:
            updated_by = int(user_id) if user_id is not None else None
        except Exception as e:
            raise HTTPException(status_code=400, detail="Invalid user context (user_id must be int-like).") from e

        db = SessionLocal()
        try:
            row: Optional[CompPage] = db.query(CompPage).get(payload.id)
            if not row:
                raise HTTPException(status_code=404, detail="Page not found")

            gcs = get_gcs()
            dest_prefix = f"{self.base_folder}/{row.id}"

            def validate_upload(f: UploadFile, max_mb: int, allowed_types: Optional[set[str]]) -> str:
                ct = resolve_content_type(f)
                max_bytes = max_mb * 1024 * 1024
                size = file_size_bytes(f, max_bytes)
                if size == 0:
                    raise HTTPException(status_code=400, detail=f"File '{f.filename}' is empty")
                if size > max_bytes:
                    raise HTTPException(status_code=413, detail=f"File '{f.filename}' exceeds {max_mb}MB limit")
                if allowed_types is not None and ct not in allowed_types:
                    raise HTTPException(status_code=415, detail=f"Unsupported content type '{ct}' for '{f.filename}'")
                try:
                    f.file.seek(0)
                except Exception:
                    pass
                return ct

            async def upload_and_get_key(fileobj, filename: str, key_prefix: str, content_type: str) -> str:
                try:
                    res = await asyncio.to_thread(
                        gcs.upload_fileobj,
                        fileobj,
                        filename,
                        key_prefix,
                        False,
                        content_type,
                    )
                except TypeError:
                    res = await asyncio.to_thread(
                        gcs.upload_fileobj,
                        fileobj=fileobj,
                        filename=filename,
                        dest_prefix=key_prefix,
                        public=False,
                        content_type=content_type,
                    )

                if not res:
                    raise RuntimeError("Upload failed")

                key = res.get("key") or ""
                if not key:
                    raise RuntimeError("Upload failed")
                return key

            if payload.name is not None and payload.name.strip():
                row.name = payload.name.strip()
            if payload.description is not None:
                row.description = payload.description

            fields_set = getattr(payload, "__pydantic_fields_set__", set())

            if "group_id" in fields_set:
                row.group_id = payload.group_id
            if "sub_type" in fields_set:
                row.sub_type = payload.sub_type

            current_tags: List[str] = list(row.tags or [])
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
                elif payload.tags_mode == "remove":
                    remove_set = set(incoming)
                    current_tags = [t for t in current_tags if t not in remove_set]

            if payload.tags_clear or payload.tags is not None or row.tags is None:
                row.tags = current_tags

            if payload.metadata is not None:
                row.metadata_json = payload.metadata or None

            if payload.thumbnail_clear:
                row.thumbnail = None

            if thumbnail:
                ct = validate_upload(thumbnail, self.max_image_mb, self.image_types)
                fname = make_uuid_name(thumbnail.filename, "thumbnail")
                key = await upload_and_get_key(thumbnail.file, fname, f"{dest_prefix}/thumbnail", ct)
                row.thumbnail = key

            current_images: List[str] = list(row.images or [])
            if payload.images_clear:
                current_images = []

            new_image_keys: List[str] = []
            if images:
                for idx, img in enumerate(images):
                    ct = validate_upload(img, self.max_image_mb, self.image_types)
                    fname = make_uuid_name(img.filename, f"img{idx:03d}")
                    key = await upload_and_get_key(img.file, fname, f"{dest_prefix}/images", ct)
                    new_image_keys.append(key)

            if new_image_keys:
                if payload.images_mode == "replace":
                    current_images = new_image_keys
                else:
                    seen = set(current_images)
                    for k in new_image_keys:
                        if k not in seen:
                            current_images.append(k)
                            seen.add(k)

            if payload.images_clear or new_image_keys or (row.images is None):
                row.images = current_images

            if payload.file_link_clear:
                row.file_link = None

            if file_link:
                ct = validate_upload(file_link, self.max_file_mb, None)
                fname = make_uuid_name(file_link.filename, "file")
                key = await upload_and_get_key(file_link.file, fname, f"{dest_prefix}/files", ct)
                row.file_link = key

            row.updated_by = updated_by

            db.add(row)
            db.commit()
            db.refresh(row)

            logger.info("[pages.update] finished total_elapsed=%.3fs id=%s", time.monotonic() - start_t, row.id)

            return {
                "status": "ok",
                "data": comp_pages_to_dict(row),
                "gcs": {
                    "thumbnail": sign_url_maybe(gcs, getattr(row, "thumbnail", None)),
                    "images": [sign_url_maybe(gcs, k) for k in (row.images or [])],
                    "file_link": sign_url_maybe(gcs, getattr(row, "file_link", None)),
                    "page_folder": str(row.id),
                },
            }

        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            logger.exception("[pages.update] error - rolling back: %s", e)
            db.rollback()
            raise HTTPException(status_code=500, detail="Failed to update page") from e
        finally:
            db.close()
            all_files: List[Optional[UploadFile]] = [thumbnail, file_link]
            if images:
                all_files.extend(images)
            for uf in all_files:
                try:
                    if uf and getattr(uf, "file", None) and not uf.file.closed:
                        uf.file.close()
                except Exception:
                    pass


def comp_pages_to_dict(m: CompPage) -> dict:
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
