from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import time
from typing import Any, Dict, List, Optional
from uuid import UUID as PyUUID
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_authentications import CompAuthentication

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.authentications.create_with_uploads")
except Exception:
    import logging

    logger = logging.getLogger(__name__)

_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FALLBACK_MIME = {
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


def make_uuid_name(filename: str, default_stem: str) -> str:
    name = filename or ""
    stem, ext = os.path.splitext(name)
    stem = (stem or default_stem).strip()
    stem = _SAFE_CHARS_RE.sub("_", stem) or default_stem
    ext = (ext or "").lower().lstrip(".") or "bin"
    return f"{stem}.{uuid4()}.{ext}"


def _resolve_content_type(upload: UploadFile) -> str:
    ct = (getattr(upload, "content_type", None) or "").strip()
    if ct:
        return ct

    name = getattr(upload, "filename", "") or ""
    ext = os.path.splitext(name)[1].lower()

    if ext in _FALLBACK_MIME:
        return _FALLBACK_MIME[ext]

    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


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
    seen = set()
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            items.append(p)
    return items


def _file_size(upload: UploadFile) -> Optional[int]:
    f = getattr(upload, "file", None)
    if not f:
        return None
    try:
        pos = f.tell()
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(pos)
        return int(size)
    except Exception:
        return None


def _validate_upload(upload: UploadFile, *, max_bytes: int, allowed_types: Optional[set[str]] = None) -> str:
    ct = _resolve_content_type(upload)
    if allowed_types is not None and ct not in allowed_types:
        raise ValueError(f"Unsupported content type '{ct}' for '{upload.filename}'")

    size = _file_size(upload)
    if size is not None and size > max_bytes:
        mb = round(max_bytes / (1024 * 1024))
        raise ValueError(f"File '{upload.filename}' exceeds {mb}MB limit")

    return ct


class CreateCompAuthenticationsPayload(BaseModel):
    name: str
    description: Optional[str] = None
    template_id: Optional[PyUUID] = None
    group_id: Optional[PyUUID] = None
    sub_type: Optional[str] = None
    tags: Optional[str] = None
    metadata: Optional[Any] = None

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

    @field_validator("tags", mode="before")
    @classmethod
    def tags_in(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    @field_validator("sub_type", mode="before")
    @classmethod
    def sub_type_in(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    @field_validator("metadata", mode="before")
    @classmethod
    def metadata_in(cls, v: Any) -> Optional[dict]:
        if v is None or isinstance(v, dict):
            return v

        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            try:
                parsed = json.loads(s)
            except Exception as e:
                raise ValueError(f"metadata must be valid JSON if provided as string: {e}") from e
            return parsed if isinstance(parsed, dict) else {"value": parsed}

        try:
            return dict(v)
        except Exception as e:
            raise ValueError("metadata must be a JSON object or JSON string") from e


class CreateCompAuthenticationsWithUploadsCommand(BaseCommand):
    """Creates a CompAuthentication with optional uploads."""

    name = "components/authentications/create_with_uploads"
    schema = CreateCompAuthenticationsPayload
    require_auth = True
    method = "post"
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
        payload: CreateCompAuthenticationsPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        file: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        row_id = str(uuid4())
        dest_prefix = f"{self.base_folder}/{row_id}"
        gcs = get_gcs()

        logger.info(
            "authentications.create_with_uploads.start name=%s user_id=%s row_id=%s",
            payload.name,
            user_id,
            row_id,
        )

        async def upload_one(
            upload: UploadFile,
            *,
            subfolder: str,
            default_stem: str,
            max_bytes: int,
            allowed_types: Optional[set[str]] = None,
        ) -> Dict[str, Any]:
            ct = _validate_upload(upload, max_bytes=max_bytes, allowed_types=allowed_types)
            fname = make_uuid_name(upload.filename, default_stem)
            res = await asyncio.to_thread(
                gcs.upload_fileobj,
                upload.file,
                fname,
                f"{dest_prefix}/{subfolder}",
                False,
                ct,
            )
            size = _file_size(upload)
            try:
                upload.file.seek(0)
            except Exception:
                pass

            return {
                "key": res["key"],
                "filename": upload.filename,
                "content_type": ct,
                "size": size,
            }

        thumbnail_key: Optional[str] = None
        images_keys: List[str] = []
        file_link_key: Optional[str] = None

        try:
            if thumbnail:
                meta = await upload_one(
                    thumbnail,
                    subfolder="thumbnail",
                    default_stem="thumbnail",
                    max_bytes=MAX_IMAGE_MB * 1024 * 1024,
                    allowed_types=IMAGE_TYPES,
                )
                thumbnail_key = meta["key"]

            if images:
                for idx, img in enumerate(images):
                    meta = await upload_one(
                        img,
                        subfolder="images",
                        default_stem=f"img{idx:03d}",
                        max_bytes=MAX_IMAGE_MB * 1024 * 1024,
                        allowed_types=IMAGE_TYPES,
                    )
                    images_keys.append(meta["key"])

            if file:
                meta = await upload_one(
                    file,
                    subfolder="file",
                    default_stem="file",
                    max_bytes=MAX_FILE_MB * 1024 * 1024,
                    allowed_types=None,
                )
                file_link_key = meta["key"]

        except ValueError as e:
            logger.exception(
                "authentications.create_with_uploads.upload_validation_failed name=%s user_id=%s row_id=%s",
                payload.name,
                user_id,
                row_id,
            )
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(
                "authentications.create_with_uploads.upload_failed name=%s user_id=%s row_id=%s",
                payload.name,
                user_id,
                row_id,
            )
            raise HTTPException(status_code=400, detail="Upload failed") from e

        db = SessionLocal()
        try:
            row = CompAuthentication(
                name=payload.name,
                description=payload.description,
                template_id=payload.template_id,
                group_id=payload.group_id,
                sub_type=payload.sub_type,
                thumbnail=thumbnail_key,
                images=images_keys or None,
                file_link=file_link_key,
                metadata_json=payload.metadata or None,
                created_by=user_id,
                updated_by=user_id,
                tags=_normalize_tags(payload.tags),
            )

            db.add(row)
            db.commit()
            db.refresh(row)

            signed = {
                "thumbnail": None,
                "images": [],
                "file_link": None,
                "row_id": row_id,
            }

            if thumbnail_key:
                try:
                    signed["thumbnail"] = gcs.signed_get_url(thumbnail_key, expires_seconds=3600)
                except Exception:
                    signed["thumbnail"] = f"https://storage.googleapis.com/{gcs.bucket_name}/{thumbnail_key}"

            for k in images_keys:
                try:
                    signed["images"].append(gcs.signed_get_url(k, expires_seconds=3600))
                except Exception:
                    signed["images"].append(f"https://storage.googleapis.com/{gcs.bucket_name}/{k}")

            if file_link_key:
                try:
                    signed["file_link"] = gcs.signed_get_url(file_link_key, expires_seconds=3600)
                except Exception:
                    signed["file_link"] = f"https://storage.googleapis.com/{gcs.bucket_name}/{file_link_key}"

            perf_ms = round((time.monotonic() - t0) * 1000, 2)
            logger.info(
                "authentications.create_with_uploads.success name=%s user_id=%s row_id=%s perf_ms=%s",
                payload.name,
                user_id,
                row_id,
                perf_ms,
            )

            return {
                "status": "ok",
                "data": _comp_auth_to_dict(row),
                "gcs": signed,
                "perf_ms": perf_ms,
            }

        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception(
                "authentications.create_with_uploads.db_error name=%s user_id=%s row_id=%s",
                payload.name,
                user_id,
                row_id,
            )
            raise
        finally:
            db.close()
            all_files: List[Optional[UploadFile]] = [thumbnail, file] + (images or [])
            for uf in all_files:
                try:
                    f = getattr(uf, "file", None)
                    if f and not getattr(f, "closed", True):
                        f.close()
                except Exception:
                    pass


def _comp_auth_to_dict(m: CompAuthentication) -> dict:
    return {
        "id": str(m.id) if getattr(m, "id", None) is not None else None,
        "name": m.name,
        "description": m.description,
        "thumbnail": getattr(m, "thumbnail", None),
        "images": getattr(m, "images", None),
        "template_id": str(m.template_id) if getattr(m, "template_id", None) else None,
        "group_id": str(m.group_id) if getattr(m, "group_id", None) else None,
        "sub_type": getattr(m, "sub_type", None),
        "file_link": getattr(m, "file_link", None),
        "metadata": getattr(m, "metadata_json", None) or {},
        "tags": getattr(m, "tags", []) or [],
        "created_by": m.created_by,
        "updated_by": m.updated_by,
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }
