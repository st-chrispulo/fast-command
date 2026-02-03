from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.gcs.gcs import get_gcs
from models.tbl_funnels import Funnel

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.funnels.create_with_uploads")
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
        "id": str(getattr(m, "id", None)) if getattr(m, "id", None) is not None else None,
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


class CreateFunnelPayload(BaseModel):
    name: str = Field(..., description="Funnel name")
    description: Optional[str] = Field(default=None, description="Funnel description")
    template_id: Optional[UUID] = Field(default=None, description="Template funnel id")
    tags: Optional[str] = Field(default=None, description="Comma-separated tags")
    metadata: Optional[Any] = Field(default=None, description="JSON object or JSON string")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name is required")
        return v

    @field_validator("description", mode="before")
    @classmethod
    def strip_description(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("tags", mode="before")
    @classmethod
    def strip_tags(cls, v: Any) -> Any:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

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
    folder: str
    multiple: bool = False


class CreateFunnelWithUploadsCommand(BaseCommand):
    """
    Create a funnel with optional uploads:
    - thumbnail: UploadFile -> stored in thumbnail
    - images: List[UploadFile] -> stored in images
    - attachment: UploadFile -> stored in file_link

    Also stores metadata -> metadata_json.
    """

    name = "components/funnels/create_with_uploads"
    schema = CreateFunnelPayload
    require_auth = True
    method = "post"
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
        payload: CreateFunnelPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        attachment: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        gcs = get_gcs()
        funnel_id = str(uuid4())
        dest_prefix = f"{self.base_folder}/{funnel_id}"

        specs = [
            UploadSpec(
                field="thumbnail",
                max_mb=self.MAX_IMAGE_MB,
                allowed_types=set(self.IMAGE_TYPES),
                default_stem="thumbnail",
                folder=f"{dest_prefix}/thumbnail",
                multiple=False,
            ),
            UploadSpec(
                field="images",
                max_mb=self.MAX_IMAGE_MB,
                allowed_types=set(self.IMAGE_TYPES),
                default_stem="img",
                folder=f"{dest_prefix}/images",
                multiple=True,
            ),
            UploadSpec(
                field="attachment",
                max_mb=self.MAX_FILE_MB,
                allowed_types=self.ANY_FILE_TYPES,
                default_stem="file",
                folder=f"{dest_prefix}/files",
                multiple=False,
            ),
        ]

        uploads: Dict[str, Any] = {"thumbnail": thumbnail, "images": images or [], "attachment": attachment}

        logger.info("[funnels] start name=%s user_id=%s funnel_id=%s", payload.name, user_id, funnel_id)

        keys: Dict[str, Any] = {"thumbnail": None, "images": [], "attachment": None}

        try:
            for spec in specs:
                if spec.multiple:
                    files: Sequence[UploadFile] = uploads.get(spec.field) or []
                    if files:
                        keys[spec.field] = await self._upload_many(gcs, files, spec)
                else:
                    f: Optional[UploadFile] = uploads.get(spec.field)
                    if f:
                        keys[spec.field] = await self._upload_one(gcs, f, spec)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        finally:
            self._close_uploads(thumbnail, images, attachment)

        db = SessionLocal()
        try:
            row = Funnel(
                name=payload.name,
                description=payload.description,
                thumbnail=keys["thumbnail"],
                images=keys["images"] or None,
                template_id=payload.template_id,
                file_link=keys["attachment"],
                created_by=user_id,
                updated_by=user_id,
                tags=_normalize_tags(payload.tags),
                metadata_json=payload.metadata or None,
            )

            db.add(row)
            db.commit()
            db.refresh(row)

            signed = await self._best_effort_signed(gcs, keys)
            took_ms = int((time.monotonic() - t0) * 1000)
            logger.info("[funnels] ok id=%s took_ms=%s", getattr(row, "id", None), took_ms)

            return {
                "status": "ok",
                "data": _funnel_to_dict(row),
                "gcs": {
                    "thumbnail": signed["thumbnail"],
                    "images": signed["images"],
                    "attachment": signed["attachment"],
                    "funnel_id": funnel_id,
                },
            }
        except Exception:
            db.rollback()
            logger.exception("[funnels] DB error")
            raise
        finally:
            db.close()

    async def _upload_one(self, gcs: Any, f: UploadFile, spec: UploadSpec) -> str:
        ct = await self._validate_upload(f, spec.max_mb, spec.allowed_types)
        fname = make_uuid_name(getattr(f, "filename", "") or "", spec.default_stem)
        res = await asyncio.to_thread(gcs.upload_fileobj, f.file, fname, spec.folder, False, ct)
        return res["key"]

    async def _upload_many(self, gcs: Any, files: Sequence[UploadFile], spec: UploadSpec) -> List[str]:
        out: List[str] = []
        for idx, f in enumerate(files):
            ct = await self._validate_upload(f, spec.max_mb, spec.allowed_types)
            fname = make_uuid_name(getattr(f, "filename", "") or "", f"{spec.default_stem}{idx:03d}")
            res = await asyncio.to_thread(gcs.upload_fileobj, f.file, fname, spec.folder, False, ct)
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
        if images:
            files.extend(images)

        for uf in files:
            try:
                if uf and getattr(uf, "file", None) and not uf.file.closed:
                    uf.file.close()
            except Exception:
                pass
