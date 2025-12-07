# commands/components/authentications/update_with_uploads.py

import os
import re
import mimetypes
import time
import asyncio
import json
from uuid import uuid4
from typing import Optional, List, Any, Dict
from fastapi import UploadFile, HTTPException
from pydantic import BaseModel, field_validator
from uuid import UUID

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_authentications import CompAuthentication

# Prefer your app logger if available; fallback to stdlib
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("authentications_update_with_uploads")
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)

# ---------- helpers ----------

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


def _is_truthy(v: Optional[str]) -> bool:
    if v is None:
        return False
    return str(v).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


# ---------- payload ----------

class UpdateCompAuthenticationsPayload(BaseModel):
    id: UUID
    name: Optional[str] = None
    description: Optional[str] = None
    template_id: Optional[UUID] = None
    tags: Optional[str] = None  # comma-separated

    # metadata JSON (maps to CompAuthentication.metadata_json / JSONB)
    # Accepts dict or JSON string; full replacement of existing metadata_json.
    metadata: Optional[Any] = None

    # clears/removals come as strings in form-data ("1", "true", etc.)
    clear_thumbnail: Optional[str] = None
    clear_images: Optional[str] = None
    clear_file_links: Optional[str] = None

    # arrays in form-data: remove_image_keys[]=... remove_file_link_keys[]=...
    remove_image_keys: Optional[List[str]] = None
    remove_file_link_keys: Optional[List[str]] = None  # keys: login,register,logout_button,user_profile

    @field_validator("name")
    @classmethod
    def _name_trim(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("description", mode="before")
    @classmethod
    def _strip_optional(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("tags", mode="before")
    @classmethod
    def _tags_in(cls, v):
        return str(v).strip() if v is not None else v

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata_in(cls, v):
        """
        Accept dict, None, or JSON string and normalize to dict/None.
        Same behavior as in create_with_uploads & other component commands.
        """
        if v is None or isinstance(v, dict):
            return v
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    return parsed
                # valid JSON but not an object -> wrap
                return {"value": parsed}
            except Exception as e:
                raise ValueError(f"metadata must be valid JSON if provided as string: {e}")
        # Fallback: best-effort cast to dict
        try:
            return dict(v)
        except Exception:
            raise ValueError("metadata must be a JSON object or JSON string")


# ---------- command ----------

class UpdateCompAuthenticationsWithUploadsCommand(BaseCommand):
    """
    Update a CompAuthentication with optional uploads/clears.

    Accepts multipart/form-data:

    Text:
      - id (UUID, required)
      - name?, description?, template_id?, tags?
      - metadata? (JSON object or JSON string) -> stored in metadata_json (full replacement)
      - clear_thumbnail? ("1"/"true")
      - clear_images? ("1"/"true")
      - remove_image_keys[]?
      - clear_file_links? ("1"/"true")
      - remove_file_link_keys[]?  (login|register|logout_button|user_profile)

    Files:
      - thumbnail (single image)              -> replaces thumbnail
      - images (multiple images)              -> appended to images[]
      - login/register/logout_button/user_profile (single each) -> set/replace in file_links map
    """
    name = "components/authentications/update_with_uploads"
    schema = UpdateCompAuthenticationsPayload
    require_auth = True
    method = "put"
    type = "file_upload"
    group = "Authentication"

    file_fields = [
        ("thumbnail", False),
        ("images", True),
        ("login", False),
        ("register", False),
        ("logout_button", False),
        ("user_profile", False),
    ]

    base_folder = "uploads"

    async def execute(
        self,
        payload: UpdateCompAuthenticationsPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        login: Optional[UploadFile] = None,
        register: Optional[UploadFile] = None,
        logout_button: Optional[UploadFile] = None,
        user_profile: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ):
        t0 = time.monotonic()
        logger.info("[authentications:update] id=%s user_id=%s", getattr(payload, "id", None), user_id)
        gcs = get_gcs()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        db = SessionLocal()
        try:
            row: Optional[CompAuthentication] = db.query(CompAuthentication).get(str(payload.id))
            if not row:
                raise HTTPException(status_code=404, detail="Authentication component not found")

            row_id = str(row.id)
            dest_prefix = f"{self.base_folder}/{row_id}"

            async def _read_and_check_img(f: UploadFile):
                if f is None:
                    return None, None
                data = await f.read()
                try:
                    f.file.seek(0)
                except Exception:
                    pass
                if len(data) > MAX_IMAGE_MB * 1024 * 1024:
                    raise ValueError(f"Image '{f.filename}' exceeds {MAX_IMAGE_MB}MB limit")
                ct = _resolve_content_type(f)
                if ct not in IMAGE_TYPES:
                    raise ValueError(f"Unsupported image type '{ct}' for '{f.filename}'")
                return data, ct

            async def _read_and_check_any(f: UploadFile):
                if f is None:
                    return None, None
                data = await f.read()
                try:
                    f.file.seek(0)
                except Exception:
                    pass
                if len(data) > MAX_FILE_MB * 1024 * 1024:
                    raise ValueError(f"File '{f.filename}' exceeds {MAX_FILE_MB}MB limit")
                ct = _resolve_content_type(f)
                return data, ct

            async def _upload_one(f: Optional[UploadFile], subfolder: str, default_stem: str, image: bool = False):
                if not f:
                    return None
                if image:
                    _, ct = await _read_and_check_img(f)
                else:
                    _, ct = await _read_and_check_any(f)
                fname = make_uuid_name(f.filename, default_stem)
                res = await asyncio.to_thread(
                    gcs.upload_fileobj, f.file, fname, f"{dest_prefix}/{subfolder}", False, ct
                )
                try:
                    f.file.seek(0, os.SEEK_END)
                    size = f.file.tell()
                    f.file.seek(0)
                except Exception:
                    size = None
                return {
                    "key": res["key"],
                    "filename": f.filename,
                    "content_type": ct,
                    "size": size,
                }

            # ---------- text updates ----------
            if payload.name is not None:
                row.name = payload.name
            if payload.description is not None:
                row.description = payload.description
            if payload.template_id is not None:
                row.template_id = payload.template_id
            if payload.tags is not None:
                row.tags = _normalize_tags(payload.tags)

            # metadata: if provided, full replacement of metadata_json
            if payload.metadata is not None:
                row.metadata_json = payload.metadata or None

            # ---------- thumbnail ----------
            if _is_truthy(payload.clear_thumbnail):
                row.thumbnail = None
            if thumbnail:
                meta = await _upload_one(thumbnail, "thumbnail", "thumbnail", image=True)
                row.thumbnail = meta["key"] if meta else row.thumbnail

            # ---------- images array ----------
            imgs = list(row.images or [])
            if _is_truthy(payload.clear_images):
                imgs = []
            if payload.remove_image_keys:
                rmset = set(k for k in payload.remove_image_keys if k)
                imgs = [k for k in imgs if k not in rmset]
            if images:
                for idx, img in enumerate(images):
                    meta = await _upload_one(img, "images", f"img{idx:03d}", image=True)
                    if meta:
                        imgs.append(meta["key"])
            row.images = imgs or None

            # ---------- file_links (map) ----------
            links: Dict[str, Any] = dict(row.file_links or {})
            if _is_truthy(payload.clear_file_links):
                links = {}

            if payload.remove_file_link_keys:
                for k in payload.remove_file_link_keys:
                    if k in links:
                        links.pop(k, None)

            for k, uf in {
                "login": login,
                "register": register,
                "logout_button": logout_button,
                "user_profile": user_profile,
            }.items():
                if uf:
                    meta = await _upload_one(uf, "auth", k, image=False)
                    if meta:
                        links[k] = meta

            row.file_links = links or None
            row.updated_by = user_id

            db.add(row)
            db.commit()
            db.refresh(row)

            # ---------- signed urls (best-effort) ----------
            signed = {"thumbnail": None, "images": [], "file_links": {}}
            if row.thumbnail:
                try:
                    signed["thumbnail"] = gcs.signed_get_url(row.thumbnail, expires_seconds=3600)
                except Exception:
                    signed["thumbnail"] = f"https://storage.googleapis.com/{gcs.bucket_name}/{row.thumbnail}"

            for k in row.images or []:
                try:
                    signed["images"].append(gcs.signed_get_url(k, expires_seconds=3600))
                except Exception:
                    signed["images"].append(f"https://storage.googleapis.com/{gcs.bucket_name}/{k}")

            for key, meta in (row.file_links or {}).items():
                try:
                    signed["file_links"][key] = gcs.signed_get_url(meta["key"], expires_seconds=3600)
                except Exception:
                    signed["file_links"][key] = f"https://storage.googleapis.com/{gcs.bucket_name}/{meta['key']}"

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
            logger.exception("[authentications:update] DB error")
            raise
        finally:
            db.close()
            # Close uploads if any
            all_files: List[UploadFile] = [thumbnail] + (images or []) + [
                login,
                register,
                logout_button,
                user_profile,
            ]
            for uf in all_files:
                try:
                    if uf and getattr(uf, "file", None) and not uf.file.closed:
                        uf.file.close()
                except Exception:
                    pass


# ---------- serializer ----------

def _comp_auth_to_dict(m: CompAuthentication) -> dict:
    return {
        "id": str(m.id) if getattr(m, "id", None) is not None else None,
        "name": m.name,
        "description": m.description,
        "thumbnail": getattr(m, "thumbnail", None),
        "images": getattr(m, "images", None),
        "template_id": str(m.template_id) if getattr(m, "template_id", None) else None,
        "file_links": getattr(m, "file_links", None) or {},
        "metadata": getattr(m, "metadata_json", None) or {},
        "tags": getattr(m, "tags", []) or [],
        "created_by": m.created_by,
        "updated_by": m.updated_by,
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }
