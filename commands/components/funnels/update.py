from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.gcs.gcs import get_gcs
from models.tbl_funnels import Funnel

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.funnels.update_with_uploads")
except Exception:
    import logging

    logger = logging.getLogger(__name__)

_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FALLBACK_MIME: Dict[str, str] = {
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
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _funnel_to_dict(m: Funnel) -> Dict[str, Any]:
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


class UpdateFunnelPayload(BaseModel):
    id: UUID = Field(..., description="Funnel id")
    name: Optional[str] = Field(default=None, description="Funnel name")
    description: Optional[str] = Field(default=None, description="Funnel description")
    template_id: Optional[UUID] = Field(default=None, description="Template funnel id")
    tags: Optional[Any] = Field(default=None, description="Comma-separated or list-like tags")
    metadata: Optional[Any] = Field(default=None, description="JSON object or JSON string")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if not s:
            raise ValueError("name cannot be empty if provided")
        return s

    @field_validator("description", mode="before")
    @classmethod
    def strip_description(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("metadata", mode="before")
    @classmethod
    def normalize_metadata(cls, v: Any) -> Any:
        if v is None or isinstance(v, dict):
            return v
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            try:
                parsed = json.loads(s)
            except Exception as e:
                raise ValueError("metadata must be valid JSON if provided as string") from e
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        try:
            return dict(v)
        except Exception as e:
            raise ValueError("metadata must be a JSON object or JSON string") from e


@dataclass(frozen=True)
class UploadSpec:
    field: str
    max_mb: int
    allowed_types: Optional[Set[str]]
    default_stem: str
    folder_suffix: str
    multiple: bool = False


class UpdateFunnelWithUploadsCommand(BaseCommand):
    """
    Update a funnel with optional uploads:
    - thumbnail: UploadFile -> replaces thumbnail if provided
    - images: List[UploadFile] -> replaces images if provided; empty list clears images
    - attachment: UploadFile -> replaces file_link if provided

    Updates metadata_json when metadata is provided (empty string clears).
    """

    name = "components/funnels/update_with_uploads"
    schema = UpdateFunnelPayload
    require_auth = True
    method = "put"
    type = "file_upload"
    group = "Funnel"

    file_fields = [
        ("thumbnail", False),
        ("images", True),
        ("attachment", False),
    ]

    base_folder = "uploads"
    MAX_IMAGE_MB = 50
    MAX_FILE_MB = 200
    IMAGE_TYPES: Set[str] = {"image/jpeg", "image/png", "image/webp"}
    ANY_FILE_TYPES = None

    async def execute(
        self,
        payload: UpdateFunnelPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        attachment: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        logger.info("[funnels] update start id=%s user_id=%s", str(payload.id), user_id)

        gcs = get_gcs()
        db = SessionLocal()
        try:
            row = db.query(Funnel).filter(Funnel.id == payload.id).one_or_none()
            if not row:
                raise HTTPException(status_code=404, detail="Funnel not found")

            dest_prefix = f"{self.base_folder}/{row.id}"

            keys: Dict[str, Any] = {
                "thumbnail": getattr(row, "thumbnail", None),
                "images": list(getattr(row, "images", []) or []) if getattr(row, "images", None) is not None else None,
                "attachment": getattr(row, "file_link", None),
            }

            specs = [
                UploadSpec(
                    field="thumbnail",
                    max_mb=self.MAX_IMAGE_MB,
                    allowed_types=set(self.IMAGE_TYPES),
                    default_stem="thumbnail",
                    folder_suffix="thumbnail",
                    multiple=False,
                ),
                UploadSpec(
                    field="images",
                    max_mb=self.MAX_IMAGE_MB,
                    allowed_types=set(self.IMAGE_TYPES),
                    default_stem="img",
                    folder_suffix="images",
                    multiple=True,
                ),
                UploadSpec(
                    field="attachment",
                    max_mb=self.MAX_FILE_MB,
                    allowed_types=self.ANY_FILE_TYPES,
                    default_stem="file",
                    folder_suffix="files",
                    multiple=False,
                ),
            ]

            if thumbnail:
                keys["thumbnail"] = await self._upload_one(gcs, thumbnail, dest_prefix, specs[0])

            if images is not None:
                keys["images"] = await self._upload_many(gcs, images, dest_prefix, specs[1])

            if attachment:
                keys["attachment"] = await self._upload_one(gcs, attachment, dest_prefix, specs[2])

            if payload.name is not None:
                row.name = payload.name
            if payload.description is not None:
                row.description = payload.description
            if payload.template_id is not None:
                row.template_id = payload.template_id
            if payload.tags is not None:
                row.tags = _normalize_tags(payload.tags)
            if payload.metadata is not None:
                row.metadata_json = payload.metadata or None

            row.thumbnail = keys["thumbnail"]
            row.images = keys["images"]
            row.file_link = keys["attachment"]
            row.updated_by = user_id

            db.add(row)
            db.commit()
            db.refresh(row)

            signed = await self._best_effort_signed(gcs, keys)
            logger.info(
                "[funnels] update ok id=%s user_id=%s elapsed=%.3fs",
                str(row.id),
                user_id,
                time.monotonic() - t0,
            )

            return {
                "status": "ok",
                "data": _funnel_to_dict(row),
                "gcs": {
                    "thumbnail": signed["thumbnail"],
                    "images": signed["images"],
                    "attachment": signed["attachment"],
                    "funnel_id": str(row.id),
                },
            }
        except HTTPException:
            db.rollback()
            logger.exception("[funnels] HTTPException during update")
            raise
        except Exception:
            db.rollback()
            logger.exception("[funnels] DB error during update")
            raise
        finally:
            db.close()
            self._close_uploads(thumbnail, images, attachment)

    async def _upload_one(self, gcs: Any, f: UploadFile, dest_prefix: str, spec: UploadSpec) -> str:
        ct = await self._validate_upload(f, spec.max_mb, spec.allowed_types)
        fname = make_uuid_name(getattr(f, "filename", "") or "", spec.default_stem)
        folder = f"{dest_prefix}/{spec.folder_suffix}"
        res = await asyncio.to_thread(gcs.upload_fileobj, f.file, fname, folder, False, ct)
        return res["key"]

    async def _upload_many(
        self,
        gcs: Any,
        files: Sequence[UploadFile],
        dest_prefix: str,
        spec: UploadSpec,
    ) -> List[str]:
        out: List[str] = []
        for idx, f in enumerate(files):
            ct = await self._validate_upload(f, spec.max_mb, spec.allowed_types)
            fname = make_uuid_name(getattr(f, "filename", "") or "", f"{spec.default_stem}{idx:03d}")
            folder = f"{dest_prefix}/{spec.folder_suffix}"
            res = await asyncio.to_thread(gcs.upload_fileobj, f.file, fname, folder, False, ct)
            out.append(res["key"])
        return out

    async def _validate_upload(self, f: UploadFile, max_mb: int, allowed: Optional[Set[str]]) -> str:
        data = await f.read()
        try:
            f.file.seek(0)
        except Exception:
            pass

        size = len(data)
        if size > max_mb * 1024 * 1024:
            raise ValueError(f"File '{getattr(f, 'filename', '')}' exceeds {max_mb}MB limit")

        ct = _resolve_content_type(f)
        if allowed is not None and ct not in allowed:
            raise ValueError(f"Unsupported content type '{ct}' for '{getattr(f, 'filename', '')}'")

        return ct

    async def _best_effort_signed(self, gcs: Any, keys: Dict[str, Any]) -> Dict[str, Any]:
        def fallback_url(k: str) -> str:
            return f"https://storage.googleapis.com/{gcs.bucket_name}/{k}"

        out = {"thumbnail": None, "images": [], "attachment": None}

        k_thumb = keys.get("thumbnail")
        if k_thumb:
            try:
                out["thumbnail"] = gcs.signed_get_url(k_thumb, expires_seconds=3600)
            except Exception:
                out["thumbnail"] = fallback_url(k_thumb)

        for k in keys.get("images") or []:
            try:
                out["images"].append(gcs.signed_get_url(k, expires_seconds=3600))
            except Exception:
                out["images"].append(fallback_url(k))

        k_att = keys.get("attachment")
        if k_att:
            try:
                out["attachment"] = gcs.signed_get_url(k_att, expires_seconds=3600)
            except Exception:
                out["attachment"] = fallback_url(k_att)

        return out

    def _close_uploads(
        self,
        thumbnail: Optional[UploadFile],
        images: Optional[List[UploadFile]],
        attachment: Optional[UploadFile],
    ) -> None:
        files: List[Optional[UploadFile]] = [thumbnail, attachment]
        if images is not None:
            files.extend(images)

        for uf in files:
            try:
                if uf and getattr(uf, "file", None) and not uf.file.closed:
                    uf.file.close()
            except Exception:
                pass
