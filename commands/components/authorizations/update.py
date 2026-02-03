from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID as PyUUID
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_authentications import CompAuthentication

try:
    from logger import logger
except Exception:
    import logging as _logging

    logger = _logging.getLogger("authentications_update_with_uploads")
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(_logging.INFO)


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


class UpdateCompAuthenticationsPayload(BaseModel):
    """Multipart form payload for updating an authentication component."""

    id: PyUUID = Field(...)

    name: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    template_id: Optional[PyUUID] = Field(default=None)

    group_id: Optional[PyUUID] = Field(default=None)
    sub_type: Optional[str] = Field(default=None)

    tags: Optional[str] = Field(default=None, description="Comma-separated")

    metadata: Optional[Any] = Field(
        default=None,
        description="JSON object or JSON string; full replacement of metadata_json",
    )

    clear_thumbnail: Optional[str] = Field(default=None)
    clear_images: Optional[str] = Field(default=None)
    clear_file_link: Optional[str] = Field(default=None)

    remove_image_keys: Optional[List[str]] = Field(default=None)

    @field_validator("name")
    @classmethod
    def trim_name(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("description", mode="before")
    @classmethod
    def trim_description(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("tags", mode="before")
    @classmethod
    def trim_tags(cls, v: Any) -> Any:
        return str(v).strip() if v is not None else v

    @field_validator("sub_type", mode="before")
    @classmethod
    def trim_sub_type(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("metadata", mode="before")
    @classmethod
    def parse_metadata(cls, v: Any) -> Optional[Dict[str, Any]]:
        if v is None:
            return None
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            try:
                parsed = json.loads(s)
            except Exception as e:
                raise ValueError(f"metadata must be valid JSON: {e}")
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        try:
            return dict(v)
        except Exception as e:
            raise ValueError(f"metadata must be a JSON object or JSON string: {e}") from e


def make_uuid_name(filename: str, default_stem: str) -> str:
    name = filename or ""
    stem, ext = os.path.splitext(name)
    stem = (stem or default_stem).strip()
    stem = _SAFE_CHARS_RE.sub("_", stem) or default_stem
    ext = (ext or "").lower().lstrip(".") or "bin"
    return f"{stem}.{uuid4()}.{ext}"


def _resolve_content_type(upload: UploadFile) -> str:
    ct = getattr(upload, "content_type", None)
    if ct:
        return ct
    name = getattr(upload, "filename", "") or ""
    ext = os.path.splitext(name)[1].lower()
    if ext in _FALLBACK_MIME:
        return _FALLBACK_MIME[ext]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _is_truthy(v: Optional[str]) -> bool:
    if v is None:
        return False
    return str(v).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _normalize_tags(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(p).strip() for p in value]
    else:
        parts = []
    items: List[str] = []
    seen: set[str] = set()
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            items.append(p)
    return items


def _comp_auth_to_dict(m: CompAuthentication) -> Dict[str, Any]:
    return {
        "id": str(getattr(m, "id", None)) if getattr(m, "id", None) is not None else None,
        "name": getattr(m, "name", None),
        "description": getattr(m, "description", None),
        "thumbnail": getattr(m, "thumbnail", None),
        "images": getattr(m, "images", None),
        "template_id": str(getattr(m, "template_id", None)) if getattr(m, "template_id", None) else None,
        "group_id": str(getattr(m, "group_id", None)) if getattr(m, "group_id", None) else None,
        "sub_type": getattr(m, "sub_type", None),
        "file_link": getattr(m, "file_link", None),
        "metadata": getattr(m, "metadata_json", None) or {},
        "tags": getattr(m, "tags", []) or [],
        "created_by": getattr(m, "created_by", None),
        "updated_by": getattr(m, "updated_by", None),
        "created_at": getattr(m, "created_at", None).isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": getattr(m, "updated_at", None).isoformat() if getattr(m, "updated_at", None) else None,
    }


async def _read_limited(upload: UploadFile, max_bytes: int) -> bytes:
    data = await upload.read()
    try:
        upload.file.seek(0)
    except Exception:
        pass
    if len(data) > max_bytes:
        raise ValueError(f"File '{upload.filename}' exceeds {max_bytes // (1024 * 1024)}MB limit")
    return data


async def _validate_upload(upload: UploadFile, *, max_mb: int, allowed_types: Optional[set[str]] = None) -> str:
    await _read_limited(upload, max_mb * 1024 * 1024)
    ct = _resolve_content_type(upload)
    if allowed_types and ct not in allowed_types:
        raise ValueError(f"Unsupported content type '{ct}' for '{upload.filename}'")
    return ct


async def _upload_to_gcs(
    *,
    gcs: Any,
    upload: UploadFile,
    dest_prefix: str,
    subfolder: str,
    default_stem: str,
    content_type: str,
) -> str:
    fname = make_uuid_name(upload.filename or "", default_stem)
    res = await asyncio.to_thread(
        gcs.upload_fileobj,
        upload.file,
        fname,
        f"{dest_prefix}/{subfolder}",
        False,
        content_type,
    )
    return res["key"]


def _close_uploads(thumbnail: Optional[UploadFile], images: Optional[List[UploadFile]], file: Optional[UploadFile]) -> None:
    all_files: List[Optional[UploadFile]] = [thumbnail, file] + (images or [])
    for uf in all_files:
        try:
            if uf and getattr(uf, "file", None) and not uf.file.closed:
                uf.file.close()
        except Exception:
            pass


class UpdateCompAuthenticationsWithUploadsCommand(BaseCommand):
    """Update a CompAuthentication with optional uploads and clear/remove actions."""

    name = "components/authentications/update_with_uploads"
    schema = UpdateCompAuthenticationsPayload
    require_auth = True
    method = "put"
    type = "file_upload"
    group = "Authentication"

    file_fields = [
        ("thumbnail", False),
        ("images", True),
        ("file", False),
    ]

    base_folder = "uploads"

    async def execute(
        self,
        payload: UpdateCompAuthenticationsPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        file: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        t0 = time.monotonic()
        logger.info("[authentications:update] id=%s user_id=%s", str(payload.id), user_id)

        gcs = get_gcs()
        db = SessionLocal()

        try:
            row: Optional[CompAuthentication] = db.query(CompAuthentication).get(str(payload.id))
            if not row:
                raise HTTPException(status_code=404, detail="Authentication component not found")
            if getattr(row, "created_by", None) != user_id:
                raise HTTPException(status_code=403, detail="Forbidden")

            dest_prefix = f"{self.base_folder}/{str(row.id)}"

            if payload.name is not None:
                row.name = payload.name
            if payload.description is not None:
                row.description = payload.description
            if payload.template_id is not None:
                row.template_id = payload.template_id
            if payload.tags is not None:
                row.tags = _normalize_tags(payload.tags)

            if payload.group_id is not None:
                row.group_id = payload.group_id
            if payload.sub_type is not None:
                row.sub_type = payload.sub_type

            if payload.metadata is not None:
                row.metadata_json = payload.metadata or None

            if _is_truthy(payload.clear_thumbnail):
                row.thumbnail = None
            if thumbnail:
                ct = await _validate_upload(thumbnail, max_mb=MAX_IMAGE_MB, allowed_types=IMAGE_TYPES)
                key = await _upload_to_gcs(
                    gcs=gcs,
                    upload=thumbnail,
                    dest_prefix=dest_prefix,
                    subfolder="thumbnail",
                    default_stem="thumbnail",
                    content_type=ct,
                )
                row.thumbnail = key

            imgs = list(getattr(row, "images", None) or [])
            if _is_truthy(payload.clear_images):
                imgs = []
            if payload.remove_image_keys:
                rm = {k for k in payload.remove_image_keys if k}
                imgs = [k for k in imgs if k not in rm]
            if images:
                for idx, img in enumerate(images):
                    ct = await _validate_upload(img, max_mb=MAX_IMAGE_MB, allowed_types=IMAGE_TYPES)
                    key = await _upload_to_gcs(
                        gcs=gcs,
                        upload=img,
                        dest_prefix=dest_prefix,
                        subfolder="images",
                        default_stem=f"img{idx:03d}",
                        content_type=ct,
                    )
                    imgs.append(key)
            row.images = imgs or None

            if _is_truthy(payload.clear_file_link):
                row.file_link = None
            if file:
                ct = await _validate_upload(file, max_mb=MAX_FILE_MB)
                key = await _upload_to_gcs(
                    gcs=gcs,
                    upload=file,
                    dest_prefix=dest_prefix,
                    subfolder="file",
                    default_stem="file",
                    content_type=ct,
                )
                row.file_link = key

            row.updated_by = user_id

            db.add(row)
            db.commit()
            db.refresh(row)

            signed: Dict[str, Any] = {"thumbnail": None, "images": [], "file_link": None}

            if row.thumbnail:
                signed["thumbnail"] = _sign_best_effort(gcs, row.thumbnail)
            for k in row.images or []:
                signed["images"].append(_sign_best_effort(gcs, k))
            if row.file_link:
                signed["file_link"] = _sign_best_effort(gcs, row.file_link)

            return {
                "status": "ok",
                "data": _comp_auth_to_dict(row),
                "gcs": signed,
                "perf_ms": round((time.monotonic() - t0) * 1000, 2),
            }
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("[authentications:update] error")
            raise
        finally:
            db.close()
            _close_uploads(thumbnail, images, file)

    def run(
        self,
        payload: UpdateCompAuthenticationsPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        file: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return asyncio.run(self.execute(payload, thumbnail=thumbnail, images=images, file=file, user_id=user_id))


def _sign_best_effort(gcs: Any, key: str) -> str:
    try:
        return gcs.signed_get_url(key, expires_seconds=3600)
    except Exception:
        bucket = getattr(gcs, "bucket_name", None)
        if bucket:
            return f"https://storage.googleapis.com/{bucket}/{key}"
        return key
