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
from models.components.tbl_comp_contents import CompContent

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.contents.create_with_uploads")
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


def _make_uuid_name(filename: str, default_stem: str) -> str:
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

    out: List[str] = []
    seen = set()
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _file_size_bytes(upload: UploadFile) -> int:
    f = getattr(upload, "file", None)
    if f is None:
        return 0
    try:
        cur = f.tell()
        f.seek(0, os.SEEK_END)
        end = f.tell()
        f.seek(cur, os.SEEK_SET)
        return int(end)
    except Exception:
        return 0


def _safe_close(upload: Optional[UploadFile]) -> None:
    if not upload:
        return
    try:
        f = getattr(upload, "file", None)
        if f is not None and not getattr(f, "closed", False):
            f.close()
    except Exception:
        return


def _safe_close_many(uploads: Optional[List[UploadFile]]) -> None:
    for u in uploads or []:
        _safe_close(u)


async def _upload_fileobj(
    gcs: Any,
    upload: UploadFile,
    *,
    dest_prefix: str,
    default_stem: str,
    public: bool,
    content_type: str,
) -> str:
    filename = _make_uuid_name(getattr(upload, "filename", "") or "", default_stem)
    fileobj = upload.file
    try:
        fileobj.seek(0)
    except Exception:
        pass

    def _call() -> dict:
        try:
            return gcs.upload_fileobj(
                fileobj=fileobj,
                filename=filename,
                dest_prefix=dest_prefix,
                public=public,
                content_type=content_type,
            )
        except TypeError:
            return gcs.upload_fileobj(fileobj, filename, dest_prefix, public, content_type)

    res = await asyncio.to_thread(_call)
    key = (res or {}).get("key")
    if not key:
        raise HTTPException(status_code=500, detail="Upload failed.")
    return key


def _signed_url(gcs: Any, key: Optional[str], expires_seconds: int = 3600) -> Optional[str]:
    if not key:
        return None
    try:
        return gcs.signed_get_url(key, expires_seconds=expires_seconds)
    except Exception:
        bucket = getattr(gcs, "bucket_name", None) or ""
        if bucket:
            return f"https://storage.googleapis.com/{bucket}/{key}"
        return None


class CreateCompContentsPayload(BaseModel):
    name: str = Field(..., description="Content name")
    description: Optional[str] = Field(default=None, description="Content description")
    template_id: Optional[UUID] = Field(default=None, description="Template id")

    group_id: Optional[UUID] = Field(default=None, description="Group id")
    sub_type: Optional[str] = Field(default=None, description="Subtype")

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
    def metadata_in(cls, v: Any) -> Optional[dict]:
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


class CreateCompContentsWithUploadsCommand(BaseCommand):
    """Create a content record with optional thumbnail, images, and attachment uploads."""

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

    max_image_mb = 50
    max_file_mb = 200
    image_types = {"image/jpeg", "image/png", "image/webp"}

    def _validate_upload(
        self,
        upload: UploadFile,
        *,
        max_mb: int,
        allowed_types: Optional[set[str]],
    ) -> str:
        ct = _resolve_content_type(upload)
        size = _file_size_bytes(upload)
        if size and size > max_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"File '{upload.filename}' exceeds {max_mb}MB limit")
        if allowed_types is not None and ct not in allowed_types:
            raise HTTPException(status_code=415, detail=f"Unsupported content type '{ct}' for '{upload.filename}'")
        return ct

    async def execute(
        self,
        payload: CreateCompContentsPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        attachment: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        t_start = time.monotonic()
        if self.require_auth and user_id is None:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        created_by: Optional[int] = None
        if user_id is not None:
            try:
                created_by = int(user_id)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid user context (user_id must be int-like).")

        gcs = get_gcs()
        content_id = str(uuid4())
        dest_prefix = f"{self.base_folder}/{content_id}"

        logger.info(
            "start name=%s user_id=%s group_id=%s sub_type=%s content_id=%s",
            payload.name,
            user_id,
            payload.group_id,
            payload.sub_type,
            content_id,
        )

        thumbnail_key: Optional[str] = None
        images_keys: List[str] = []
        attachment_key: Optional[str] = None

        db = SessionLocal()
        try:
            if thumbnail:
                ct = self._validate_upload(thumbnail, max_mb=self.max_image_mb, allowed_types=self.image_types)
                thumbnail_key = await _upload_fileobj(
                    gcs,
                    thumbnail,
                    dest_prefix=f"{dest_prefix}/thumbnail",
                    default_stem="thumbnail",
                    public=False,
                    content_type=ct,
                )

            if images:
                for idx, img in enumerate(images):
                    if not img:
                        continue
                    ct = self._validate_upload(img, max_mb=self.max_image_mb, allowed_types=self.image_types)
                    key = await _upload_fileobj(
                        gcs,
                        img,
                        dest_prefix=f"{dest_prefix}/images",
                        default_stem=f"img{idx:03d}",
                        public=False,
                        content_type=ct,
                    )
                    images_keys.append(key)

            if attachment:
                ct = self._validate_upload(attachment, max_mb=self.max_file_mb, allowed_types=None)
                attachment_key = await _upload_fileobj(
                    gcs,
                    attachment,
                    dest_prefix=f"{dest_prefix}/files",
                    default_stem="file",
                    public=False,
                    content_type=ct,
                )

            row = CompContent(
                name=payload.name,
                description=payload.description,
                thumbnail=thumbnail_key,
                images=images_keys or None,
                template_id=payload.template_id,
                file_link=attachment_key,
                created_by=created_by,
                updated_by=created_by,
                tags=_normalize_tags(payload.tags),
                metadata_json=payload.metadata or None,
                group_id=payload.group_id,
                sub_type=payload.sub_type,
            )

            db.add(row)
            db.commit()
            db.refresh(row)

            out = {
                "status": "ok",
                "data": _comp_contents_to_dict(row),
                "gcs": {
                    "thumbnail": _signed_url(gcs, thumbnail_key),
                    "images": [_signed_url(gcs, k) for k in (images_keys or []) if k],
                    "attachment": _signed_url(gcs, attachment_key),
                    "content_id": content_id,
                },
            }

            logger.info("ok id=%s elapsed=%.3fs", getattr(row, "id", None), time.monotonic() - t_start)
            return out
        except HTTPException:
            db.rollback()
            logger.exception("failed elapsed=%.3fs", time.monotonic() - t_start)
            raise
        except Exception as e:
            db.rollback()
            logger.exception("failed elapsed=%.3fs", time.monotonic() - t_start)
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            db.close()
            _safe_close(thumbnail)
            _safe_close_many(images)
            _safe_close(attachment)


def _comp_contents_to_dict(m: CompContent) -> dict:
    return {
        "id": str(getattr(m, "id", None)) if getattr(m, "id", None) is not None else None,
        "name": getattr(m, "name", None),
        "description": getattr(m, "description", None),
        "thumbnail": getattr(m, "thumbnail", None),
        "images": getattr(m, "images", None),
        "template_id": str(getattr(m, "template_id", None)) if getattr(m, "template_id", None) else None,
        "file_link": getattr(m, "file_link", None),
        "tags": getattr(m, "tags", None) or [],
        "metadata": getattr(m, "metadata_json", None) or {},
        "group_id": str(getattr(m, "group_id", None)) if getattr(m, "group_id", None) else None,
        "sub_type": getattr(m, "sub_type", None),
        "created_by": getattr(m, "created_by", None),
        "updated_by": getattr(m, "updated_by", None),
        "created_at": getattr(m, "created_at", None).isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": getattr(m, "updated_at", None).isoformat() if getattr(m, "updated_at", None) else None,
    }
