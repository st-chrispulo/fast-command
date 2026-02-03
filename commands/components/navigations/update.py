from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import time
from contextlib import suppress
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_navigations import CompNavigation

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.navigations.update_with_uploads")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FALLBACK_MIME: Dict[str, str] = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".html": "text/html",
    ".txt": "text/plain",
}
MAX_IMAGE_MB = 50
MAX_FILE_MB = 200
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _safe_stem(name: str, default_stem: str) -> str:
    stem, _ = os.path.splitext(name or "")
    stem = (stem or default_stem).strip()
    stem = _SAFE_CHARS_RE.sub("_", stem) or default_stem
    return stem


def make_uuid_name(filename: str, default_stem: str) -> str:
    stem = _safe_stem(filename, default_stem)
    ext = (os.path.splitext(filename or "")[1] or "").lower().lstrip(".") or "bin"
    return f"{stem}.{uuid4()}.{ext}"


def _resolve_content_type(upload: UploadFile) -> str:
    ct = (getattr(upload, "content_type", None) or "").strip()
    if ct:
        return ct
    name = (getattr(upload, "filename", "") or "").strip()
    ext = os.path.splitext(name)[1].lower()
    if ext in _FALLBACK_MIME:
        return _FALLBACK_MIME[ext]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _file_size(upload: UploadFile) -> Optional[int]:
    f = getattr(upload, "file", None)
    if f is None:
        return None
    with suppress(Exception):
        pos = f.tell()
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(pos)
        return size
    return None


def _ensure_size(upload: UploadFile, *, limit_mb: int) -> None:
    size = _file_size(upload)
    if size is None:
        return
    if size <= 0:
        raise ValueError(f"File '{upload.filename}' is empty")
    if size > limit_mb * 1024 * 1024:
        raise ValueError(f"File '{upload.filename}' exceeds {limit_mb}MB limit")


def _normalize_tags(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(p).strip() for p in value]
    else:
        parts = []
    seen: set[str] = set()
    out: List[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


class UpdateCompNavigationsPayload(BaseModel):
    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    template_id: Optional[UUID] = None
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
    metadata_clear: bool = False

    @field_validator("id", mode="before")
    @classmethod
    def id_trim(cls, v: Any) -> str:
        s = str(v or "").strip()
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

    @field_validator("tags", mode="before")
    @classmethod
    def tags_in(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("sub_type", mode="before")
    @classmethod
    def sub_type_in(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

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
            return parsed if isinstance(parsed, dict) else {"value": parsed}

        try:
            return dict(v)
        except Exception as e:
            raise ValueError("metadata must be a JSON object or JSON string") from e


class UpdateNavigationsWithUploadsCommand(BaseCommand):
    """Partially update a CompNavigation with optional uploads."""

    name = "components/navigations/update_with_uploads"
    schema = UpdateCompNavigationsPayload
    require_auth = True
    method = "put"
    type = "file_upload"
    group = "Navigation"

    file_fields = [
        ("thumbnail", False),
        ("images", True),
        ("file", False),
    ]

    base_folder = "uploads"

    async def execute(
        self,
        payload: UpdateCompNavigationsPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        file: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        start_t = time.monotonic()
        logger.info("[navigations.update] start id=%s user_id=%s", payload.id, user_id)

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        gcs = get_gcs()
        db = SessionLocal()
        try:
            row: Optional[CompNavigation] = db.query(CompNavigation).get(payload.id)
            if not row:
                raise HTTPException(status_code=404, detail="Navigation not found")

            dest_prefix = f"{self.base_folder}/{row.id}"

            async def upload_one(
                f: UploadFile,
                *,
                subfolder: str,
                default_stem: str,
                image: bool,
            ) -> Dict[str, Any]:
                ct = _resolve_content_type(f)
                if image and ct not in IMAGE_TYPES:
                    raise ValueError(f"Unsupported content type '{ct}' for '{f.filename}'")

                _ensure_size(f, limit_mb=MAX_IMAGE_MB if image else MAX_FILE_MB)

                with suppress(Exception):
                    f.file.seek(0)

                fname = make_uuid_name(f.filename, default_stem)
                res = await asyncio.to_thread(
                    gcs.upload_fileobj,
                    f.file,
                    fname,
                    f"{dest_prefix}/{subfolder}",
                    False,
                    ct,
                )

                size = _file_size(f)
                with suppress(Exception):
                    f.file.seek(0)

                return {"key": res["key"], "filename": f.filename, "content_type": ct, "size": size}

            if payload.name is not None and payload.name.strip():
                row.name = payload.name.strip()
            if payload.description is not None:
                row.description = payload.description
            if payload.template_id is not None:
                row.template_id = payload.template_id
            if payload.group_id is not None:
                row.group_id = payload.group_id
            if payload.sub_type is not None:
                row.sub_type = payload.sub_type

            current_tags: List[str] = list(row.tags or [])
            if payload.tags_clear:
                current_tags = []
            incoming = _normalize_tags(payload.tags)

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

            if payload.tags_clear or payload.tags is not None or row.tags is None:
                row.tags = current_tags

            if payload.metadata_clear:
                row.metadata_json = None
            elif payload.metadata is not None:
                row.metadata_json = payload.metadata or None

            if payload.thumbnail_clear:
                row.thumbnail = None
            if thumbnail:
                meta = await upload_one(thumbnail, subfolder="thumbnail", default_stem="thumbnail", image=True)
                row.thumbnail = meta["key"]

            current_images: List[str] = list(row.images or [])
            if payload.images_clear:
                current_images = []

            new_image_keys: List[str] = []
            if images:
                for idx, img in enumerate(images):
                    meta = await upload_one(img, subfolder="images", default_stem=f"img{idx:03d}", image=True)
                    new_image_keys.append(meta["key"])

            if new_image_keys:
                if payload.images_mode == "replace":
                    current_images = new_image_keys
                else:
                    current_images.extend(new_image_keys)

            if payload.images_clear or new_image_keys or row.images is None:
                row.images = current_images

            if payload.file_link_clear:
                row.file_link = None
            if file:
                try:
                    meta = await upload_one(file, subfolder="nav", default_stem="nav", image=False)
                    row.file_link = meta["key"]
                except Exception as e:
                    logger.exception("[navigations.update] upload failed for file")
                    raise HTTPException(status_code=400, detail=str(e)) from e

            row.updated_by = user_id

            db.add(row)
            db.commit()
            db.refresh(row)

            signed: Dict[str, Any] = {"thumbnail": None, "images": [], "file_link": None, "row_id": str(row.id)}

            if row.thumbnail:
                url = None
                with suppress(Exception):
                    url = gcs.signed_get_url(row.thumbnail, expires_seconds=3600)
                signed["thumbnail"] = url or f"https://storage.googleapis.com/{gcs.bucket_name}/{row.thumbnail}"

            for key in row.images or []:
                url = None
                with suppress(Exception):
                    url = gcs.signed_get_url(key, expires_seconds=3600)
                signed["images"].append(url or f"https://storage.googleapis.com/{gcs.bucket_name}/{key}")

            if row.file_link:
                url = None
                with suppress(Exception):
                    url = gcs.signed_get_url(row.file_link, expires_seconds=3600)
                signed["file_link"] = url or f"https://storage.googleapis.com/{gcs.bucket_name}/{row.file_link}"

            return {
                "status": "ok",
                "data": _comp_nav_to_dict(row),
                "gcs": signed,
                "perf_ms": round((time.monotonic() - start_t) * 1000, 2),
            }
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            logger.exception("[navigations.update] error - rolling back: %s", e)
            db.rollback()
            raise HTTPException(status_code=500, detail="Failed to update navigation") from e
        finally:
            db.close()
            for uf in [thumbnail, *(images or []), file]:
                with suppress(Exception):
                    if uf and getattr(uf, "file", None) and not uf.file.closed:
                        uf.file.close()


def _comp_nav_to_dict(m: CompNavigation) -> Dict[str, Any]:
    return {
        "id": str(m.id) if getattr(m, "id", None) is not None else None,
        "name": m.name,
        "description": m.description,
        "thumbnail": getattr(m, "thumbnail", None),
        "images": getattr(m, "images", None),
        "template_id": str(m.template_id) if getattr(m, "template_id", None) else None,
        "file_link": getattr(m, "file_link", None),
        "group_id": str(getattr(m, "group_id", None)) if getattr(m, "group_id", None) else None,
        "sub_type": getattr(m, "sub_type", None),
        "tags": getattr(m, "tags", []) or [],
        "metadata": getattr(m, "metadata_json", None) or {},
        "created_by": m.created_by,
        "updated_by": m.updated_by,
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }
