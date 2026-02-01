import os
import re
import json
import mimetypes
import time
import asyncio
from uuid import uuid4
from typing import Optional, List, Any
from fastapi import UploadFile, HTTPException
from pydantic import BaseModel, field_validator
from uuid import UUID

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs
from models.tbl_projects import Project

# Prefer your app logger if available; fallback to stdlib
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("projects_update_with_uploads")
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


class UpdateProjectPayload(BaseModel):
    id: UUID
    name: Optional[str] = None
    description: Optional[str] = None
    template_id: Optional[UUID] = None
    # tags is a single comma-separated string (e.g., "a, b, c")
    tags: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name_trim(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = (v or "").strip()
        if not v:
            raise ValueError("name cannot be empty")
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


class UpdateProjectWithUploadsCommand(BaseCommand):
    """
    Updates an existing Project with optional uploads/changes:
      - thumbnail: UploadFile (single, optional)
      - images: List[UploadFile] (optional; appended to existing images)
      - attachment: UploadFile (optional; replaces file_link)
    """
    name = "projects/update_with_uploads"
    schema = UpdateProjectPayload
    require_auth = True
    method = "put"
    type = "file_upload"
    group = "Project"

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
        payload: UpdateProjectPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        attachment: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ):
        t0 = time.monotonic()
        logger.info(
            "[projects] update start id=%s user_id=%s",
            getattr(payload, "id", None),
            user_id,
        )
        gcs = get_gcs()

        db = SessionLocal()
        # we want to be sure we close files even on early returns
        upload_files: List[UploadFile] = [thumbnail] if thumbnail else []
        if images:
            upload_files.extend(images)
        if attachment:
            upload_files.append(attachment)

        try:
            if self.require_auth and not user_id:
                raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

            row: Optional[Project] = (
                db.query(Project)
                .filter(Project.id == payload.id)
                .first()
            )
            if not row:
                raise HTTPException(status_code=404, detail="Project not found")

            project_uuid = str(uuid4())
            dest_prefix = f"{self.base_folder}/{project_uuid}"

            thumbnail_key: Optional[str] = row.thumbnail
            images_keys: List[str] = list(row.images or [])
            attachment_key: Optional[str] = row.file_link

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

            # thumbnail (replace if new one provided)
            if thumbnail:
                _, ct = await _read_and_check(thumbnail, self.MAX_IMAGE_MB, self.IMAGE_TYPES)
                fname = make_uuid_name(thumbnail.filename, "thumbnail")
                res = await asyncio.to_thread(
                    gcs.upload_fileobj,
                    thumbnail.file,
                    fname,
                    f"{dest_prefix}/thumbnail",
                    False,
                    ct,
                )
                thumbnail_key = res["key"]

            # images (append new ones if provided)
            if images:
                for idx, img in enumerate(images):
                    _, ct = await _read_and_check(img, self.MAX_IMAGE_MB, self.IMAGE_TYPES)
                    fname = make_uuid_name(img.filename, f"img{idx:03d}")
                    res = await asyncio.to_thread(
                        gcs.upload_fileobj,
                        img.file,
                        fname,
                        f"{dest_prefix}/images",
                        False,
                        ct,
                    )
                    images_keys.append(res["key"])

            # attachment (replace if new one provided)
            if attachment:
                _, ct = await _read_and_check(attachment, self.MAX_FILE_MB, self.ANY_FILE_TYPES)
                fname = make_uuid_name(attachment.filename, "file")
                res = await asyncio.to_thread(
                    gcs.upload_fileobj,
                    attachment.file,
                    fname,
                    f"{dest_prefix}/files",
                    False,
                    ct,
                )
                attachment_key = res["key"]

            # apply scalar updates
            if payload.name is not None:
                row.name = payload.name
            if payload.description is not None:
                row.description = payload.description
            if payload.template_id is not None:
                row.template_id = payload.template_id
            if payload.tags is not None:
                row.tags = _normalize_tags(payload.tags)

            row.thumbnail = thumbnail_key
            row.images = images_keys or None
            row.file_link = attachment_key
            row.updated_by = user_id

            db.add(row)
            db.commit()
            db.refresh(row)

            # signed urls (best-effort)
            signed_thumb = None
            signed_imgs: List[str] = []
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
                        signed_imgs.append(
                            f"https://storage.googleapis.com/{gcs.bucket_name}/{k}"
                        )
            except Exception:
                pass

            try:
                if attachment_key:
                    signed_attach = gcs.signed_get_url(attachment_key, expires_seconds=3600)
            except Exception:
                pass

            logger.info(
                "[projects] update ok id=%s user_id=%s dt=%.3fs",
                row.id,
                user_id,
                time.monotonic() - t0,
            )

            return {
                "status": "ok",
                "data": _project_to_dict(row),
                "gcs": {
                    "thumbnail": signed_thumb,
                    "images": signed_imgs,
                    "attachment": signed_attach,
                    "project_id": project_uuid,
                },
            }

        except HTTPException:
            db.rollback()
            logger.exception("[projects] update HTTPException")
            raise
        except Exception:
            db.rollback()
            logger.exception("[projects] DB error during update")
            raise
        finally:
            db.close()
            for uf in upload_files:
                try:
                    if uf and getattr(uf, "file", None) and not uf.file.closed:
                        uf.file.close()
                except Exception:
                    pass


def _project_to_dict(m: Project) -> dict:
    return {
        "id": str(m.id) if getattr(m, "id", None) is not None else None,
        "name": m.name,
        "description": m.description,
        "thumbnail": m.thumbnail,
        "images": m.images,
        "template_id": str(m.template_id) if getattr(m, "template_id", None) else None,
        "file_link": m.file_link,
        "tags": getattr(m, "tags", []) or [],
        "created_by": m.created_by,
        "updated_by": m.updated_by,
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }
