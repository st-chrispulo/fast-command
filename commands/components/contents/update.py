from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import time
from typing import Any, List, Literal, Optional, Dict, Set
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_contents import CompContent

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.contents.update_with_uploads")
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


class UpdateCompContentsPayload(BaseModel):
    id: str = Field(description="Content id (UUID string)")

    name: Optional[str] = Field(default=None, description="Name")
    description: Optional[str] = Field(default=None, description="Description")

    group_id: Optional[UUID] = Field(default=None, description="Group id (UUID)")
    sub_type: Optional[str] = Field(default=None, description="Sub type")

    tags: Optional[str] = Field(default=None, description="Comma-separated tags")
    tags_mode: Literal["append", "replace", "remove"] = Field(default="replace")
    tags_clear: bool = Field(default=False)

    images_mode: Literal["append", "replace"] = Field(default="append")

    thumbnail_clear: bool = Field(default=False)
    images_clear: bool = Field(default=False)
    file_link_clear: bool = Field(default=False)

    metadata: Optional[Any] = Field(default=None, description="JSON object (or JSON string)")

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("id is required")
        import uuid

        uuid.UUID(s)
        return s

    @field_validator("name")
    @classmethod
    def normalize_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, v: Any) -> Any:
        if isinstance(v, str):
            s = v.strip()
            return s
        return v

    @field_validator("sub_type", mode="before")
    @classmethod
    def normalize_sub_type(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags_field(cls, v: Any) -> Optional[str]:
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
    def normalize_metadata(cls, v: Any) -> Any:
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


def _safe_stem(name: str, default_stem: str) -> str:
    stem, _ = os.path.splitext(name or "")
    stem = (stem or default_stem).strip()
    stem = _SAFE_CHARS_RE.sub("_", stem) or default_stem
    return stem


def _uuid_filename(filename: str, default_stem: str) -> str:
    stem = _safe_stem(filename, default_stem)
    ext = (os.path.splitext(filename or "")[1] or "").lower().lstrip(".") or "bin"
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
    seen: Set[str] = set()
    for p in parts:
        if not p:
            continue
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


async def _read_and_check_file(f: UploadFile, max_mb: int, allowed: Optional[Set[str]]) -> str:
    t0 = time.monotonic()
    data = await f.read()
    try:
        f.file.seek(0)
    except Exception:
        pass

    ct = _resolve_content_type(f)
    size = len(data)

    logger.debug(
        "[components.contents.update_with_uploads] read file=%s size=%d ct=%s elapsed=%.3fs",
        getattr(f, "filename", None),
        size,
        ct,
        time.monotonic() - t0,
    )

    if size == 0:
        raise ValueError(f"File '{f.filename}' is empty")
    if size > max_mb * 1024 * 1024:
        raise ValueError(f"File '{f.filename}' exceeds {max_mb}MB limit")
    if allowed is not None and ct not in allowed:
        raise ValueError(f"Unsupported content type '{ct}' for '{f.filename}'")
    return ct


async def _upload_fileobj(gcs, fileobj, filename: str, dest_prefix: str, content_type: str) -> str:
    try:
        res = await asyncio.to_thread(
            gcs.upload_fileobj,
            fileobj,
            filename,
            dest_prefix,
            False,
            content_type,
        )
    except TypeError:
        res = await asyncio.to_thread(
            gcs.upload_fileobj,
            fileobj=fileobj,
            filename=filename,
            dest_prefix=dest_prefix,
            public=False,
            content_type=content_type,
        )

    if not res or not res.get("ok"):
        raise RuntimeError("Upload failed")
    return res["key"]


def _fields_set(payload: UpdateCompContentsPayload) -> Set[str]:
    try:
        return set(getattr(payload, "model_fields_set", set()) or set())
    except Exception:
        return set(getattr(payload, "__pydantic_fields_set__", set()) or set())


def _close_upload(uf: Optional[UploadFile]) -> None:
    if not uf:
        return
    try:
        if getattr(uf, "file", None) and not uf.file.closed:
            uf.file.close()
    except Exception:
        return


def _close_uploads(files: Optional[List[UploadFile]]) -> None:
    for f in files or []:
        _close_upload(f)


def _comp_contents_to_dict(m: CompContent) -> Dict[str, Any]:
    gid = getattr(m, "group_id", None)
    return {
        "id": str(getattr(m, "id", None)) if getattr(m, "id", None) is not None else None,
        "name": getattr(m, "name", None),
        "description": getattr(m, "description", None),
        "thumbnail": getattr(m, "thumbnail", None),
        "images": getattr(m, "images", []) or [],
        "template_id": str(getattr(m, "template_id", None)) if getattr(m, "template_id", None) else None,
        "file_link": getattr(m, "file_link", None),
        "tags": getattr(m, "tags", []) or [],
        "metadata": getattr(m, "metadata_json", None) or {},
        "group_id": str(gid) if gid else None,
        "sub_type": getattr(m, "sub_type", None),
        "created_by": getattr(m, "created_by", None),
        "updated_by": getattr(m, "updated_by", None),
        "created_at": getattr(m, "created_at", None).isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": getattr(m, "updated_at", None).isoformat() if getattr(m, "updated_at", None) else None,
    }


class UpdateComponentWithUploadsCommand(BaseCommand):
    name = "components/contents/update_with_uploads"
    schema = UpdateCompContentsPayload
    require_auth = True
    method = "put"
    type = "file_upload"
    group = "Content"

    file_fields = [("thumbnail", False), ("images", True), ("attachment", False)]
    base_folder = "uploads"

    MAX_IMAGE_MB = 50
    MAX_FILE_MB = 200
    IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
    ANY_FILE_TYPES = None

    async def execute(
        self,
        payload: UpdateCompContentsPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        attachment: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        start_t = time.monotonic()
        logger.info(
            "[components.contents.update_with_uploads] start id=%s user_id=%s group_id=%s sub_type=%s",
            payload.id,
            user_id,
            getattr(payload, "group_id", None),
            getattr(payload, "sub_type", None),
        )

        db = SessionLocal()
        try:
            row: Optional[CompContent] = db.get(CompContent, payload.id)
            if not row:
                raise HTTPException(status_code=404, detail="Content not found")

            gcs = get_gcs()
            dest_prefix = f"{self.base_folder}/{row.id}"
            provided = _fields_set(payload)

            if payload.name is not None:
                if payload.name:
                    row.name = payload.name
            if payload.description is not None:
                row.description = payload.description

            if "group_id" in provided:
                row.group_id = payload.group_id
            if "sub_type" in provided:
                row.sub_type = payload.sub_type

            current_tags: List[str] = list(getattr(row, "tags", None) or [])
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

            if payload.tags_clear or payload.tags is not None or getattr(row, "tags", None) is None:
                row.tags = current_tags

            if "metadata" in provided:
                row.metadata_json = payload.metadata or None

            if payload.thumbnail_clear:
                row.thumbnail = None
            if payload.images_clear:
                row.images = []
            if payload.file_link_clear:
                row.file_link = None

            if thumbnail:
                thumb_ct = await _read_and_check_file(thumbnail, self.MAX_IMAGE_MB, self.IMAGE_TYPES)
                thumb_name = _uuid_filename(thumbnail.filename or "thumbnail", "thumbnail")
                thumb_key = await _upload_fileobj(gcs, thumbnail.file, thumb_name, f"{dest_prefix}/thumbnail", thumb_ct)
                row.thumbnail = thumb_key

            current_images: List[str] = list(getattr(row, "images", None) or [])
            if payload.images_clear:
                current_images = []

            new_image_keys: List[str] = []
            if images:
                logger.info("[components.contents.update_with_uploads] uploading_images=%d", len(images))
                for idx, img in enumerate(images):
                    img_ct = await _read_and_check_file(img, self.MAX_IMAGE_MB, self.IMAGE_TYPES)
                    img_name = _uuid_filename(img.filename or f"img{idx:03d}", f"img{idx:03d}")
                    img_key = await _upload_fileobj(gcs, img.file, img_name, f"{dest_prefix}/images", img_ct)
                    new_image_keys.append(img_key)

            if new_image_keys:
                if payload.images_mode == "replace":
                    current_images = new_image_keys
                else:
                    seen = set(current_images)
                    for k in new_image_keys:
                        if k not in seen:
                            current_images.append(k)
                            seen.add(k)

            if payload.images_clear or new_image_keys or getattr(row, "images", None) is None:
                row.images = current_images

            if attachment:
                att_ct = await _read_and_check_file(attachment, self.MAX_FILE_MB, self.ANY_FILE_TYPES)
                att_name = _uuid_filename(attachment.filename or "file", "file")
                att_key = await _upload_fileobj(gcs, attachment.file, att_name, f"{dest_prefix}/files", att_ct)
                row.file_link = att_key

            if user_id is not None:
                try:
                    row.updated_by = int(user_id)
                except Exception:
                    raise HTTPException(status_code=400, detail="Invalid user context (user_id must be int-like).")

            t0 = time.monotonic()
            db.add(row)
            db.commit()
            db.refresh(row)
            logger.info("[components.contents.update_with_uploads] commit_ok id=%s elapsed=%.3fs", row.id, time.monotonic() - t0)

            gcs_payload = _build_signed_gcs_payload(gcs, row)

            logger.info(
                "[components.contents.update_with_uploads] ok id=%s elapsed=%.3fs",
                row.id,
                time.monotonic() - start_t,
            )

            return {"status": "ok", "data": _comp_contents_to_dict(row), "gcs": gcs_payload}

        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            logger.exception("[components.contents.update_with_uploads] error=%s", e)
            raise HTTPException(status_code=500, detail="Failed to update content")
        finally:
            db.close()
            _close_upload(thumbnail)
            _close_uploads(images)
            _close_upload(attachment)


def _build_signed_gcs_payload(gcs, row: CompContent) -> Dict[str, Any]:
    def signed(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        try:
            return gcs.signed_get_url(url, expires_seconds=3600)
        except Exception:
            return url

    signed_imgs: List[str] = []
    for k in getattr(row, "images", None) or []:
        signed_imgs.append(signed(k) or k)

    return {
        "thumbnail": signed(getattr(row, "thumbnail", None)),
        "images": signed_imgs,
        "file_link": signed(getattr(row, "file_link", None)),
        "content_folder": str(getattr(row, "id", "")),
    }
