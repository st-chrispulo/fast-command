# commands/components/navigations/update_with_uploads.py

import os
import re
import json
import mimetypes
import time
import asyncio
from uuid import uuid4
from typing import Optional, List, Any, Dict, Literal

from fastapi import UploadFile, HTTPException
from pydantic import BaseModel, field_validator, Field
from uuid import UUID

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_navigations import CompNavigation

# logger fallback
try:
    from logger import logger
except Exception:
    import logging as _logging
    logger = _logging.getLogger("navigations_update_with_uploads")
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
ANY_FILE_TYPES = None  # allow any (validate size only)


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
    out, seen = [], set()
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ---------------- Payload ----------------

class UpdateCompNavigationsPayload(BaseModel):
    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    template_id: Optional[UUID] = None

    tags: Optional[str] = None
    tags_mode: Literal["append", "replace", "remove"] = Field(default="replace")
    tags_clear: bool = False

    images_mode: Literal["append", "replace"] = Field(default="append")

    thumbnail_clear: bool = False
    images_clear: bool = False

    # for JSONB file_links { top, side, bottom }
    file_links_clear: bool = False
    top_clear: bool = False
    side_clear: bool = False
    bottom_clear: bool = False

    # NEW: metadata controls (JSONB metadata_json)
    metadata: Optional[Any] = None   # accepts dict or JSON string
    metadata_clear: bool = False                # clear metadata_json when True

    @field_validator("name")
    @classmethod
    def _name_trim(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("description", mode="before")
    @classmethod
    def _desc_trim(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("tags", mode="before")
    @classmethod
    def _tags_in(cls, v):
        if v is None:
            return None
        return str(v).strip() or None

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata_in(cls, v):
        """
        Accept dict, None, or JSON string and normalize to dict/None.
        Same semantics as other *_with_uploads commands.
        """
        # Already dict / None
        if v is None or isinstance(v, dict):
            return v

        # JSON string from multipart/form-data
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

            # Valid JSON but not an object → wrap
            return {"value": parsed}

        # Fallback: best-effort cast to dict
        try:
            return dict(v)
        except Exception:
            raise ValueError("metadata must be a JSON object or JSON string")


# ---------------- Command ----------------

class UpdateNavigationsWithUploadsCommand(BaseCommand):
    """
    Partially updates a CompNavigation row:
      - name, description, template_id, tags, metadata_json, thumbnail, images, file_links, updated_by

    File uploads (multipart/form-data):
      - thumbnail: UploadFile (single)  -> stored as GCS key in 'thumbnail'
      - images: List[UploadFile]        -> stored as list of GCS keys in 'images'
      - top: UploadFile                 -> stored in file_links.top (JSON)
      - side: UploadFile                -> stored in file_links.side (JSON)
      - bottom: UploadFile              -> stored in file_links.bottom (JSON)
    """
    name = "components/navigations/update_with_uploads"
    schema = UpdateCompNavigationsPayload
    require_auth = True
    method = "put"
    type = "file_upload"
    group = "Navigation"

    file_fields = [
        ("thumbnail", False),
        ("images", True),
        ("top", False),
        ("side", False),
        ("bottom", False),
    ]

    base_folder = "uploads"

    async def execute(
        self,
        payload: UpdateCompNavigationsPayload,
        thumbnail: Optional[UploadFile] = None,
        images: Optional[List[UploadFile]] = None,
        top: Optional[UploadFile] = None,
        side: Optional[UploadFile] = None,
        bottom: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ):
        start_t = time.monotonic()
        logger.info("[navigations.update] start id=%s user_id=%s", payload.id, user_id)

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        db = SessionLocal()
        try:
            row: Optional[CompNavigation] = db.query(CompNavigation).get(payload.id)
            if not row:
                raise HTTPException(status_code=404, detail="Navigation not found")

            gcs = get_gcs()
            dest_prefix = f"{self.base_folder}/{row.id}"

            async def _read_and_check(
                f: UploadFile,
                max_mb: int,
                allowed: Optional[set],
            ):
                if f is None:
                    return None, None
                t0 = time.monotonic()
                data = await f.read()
                try:
                    f.file.seek(0)
                except Exception:
                    pass
                ct = _resolve_content_type(f)
                size = len(data)
                logger.debug(
                    "[navigations.update] _read file=%s size=%d ct=%s elapsed=%.3fs",
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
                return data, ct

            async def _upload_one(
                f: Optional[UploadFile],
                subfolder: str,
                default_stem: str,
                image: bool = False,
            ):
                if not f:
                    return None
                if image:
                    await _read_and_check(f, MAX_IMAGE_MB, IMAGE_TYPES)
                else:
                    await _read_and_check(f, MAX_FILE_MB, ANY_FILE_TYPES)
                fname = make_uuid_name(f.filename, default_stem)
                res = await asyncio.to_thread(
                    gcs.upload_fileobj,
                    f.file,
                    fname,
                    f"{dest_prefix}/{subfolder}",
                    False,
                    _resolve_content_type(f),
                )
                # try to get size
                try:
                    f.file.seek(0, os.SEEK_END)
                    size = f.file.tell()
                    f.file.seek(0)
                except Exception:
                    size = None
                return {
                    "key": res["key"],
                    "filename": f.filename,
                    "content_type": _resolve_content_type(f),
                    "size": size,
                }

            # ------------ scalar fields ------------
            if payload.name is not None and payload.name.strip():
                row.name = payload.name.strip()
            if payload.description is not None:
                row.description = payload.description
            if payload.template_id is not None:
                row.template_id = payload.template_id

            # ------------ tags ------------
            current_tags: List[str] = list(row.tags or [])
            if payload.tags_clear:
                current_tags = []
            incoming: List[str] = _normalize_tags(payload.tags)

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
            else:
                if payload.tags_mode == "replace" and not payload.tags_clear:
                    # do nothing: no incoming tags, no explicit clear
                    pass

            if payload.tags_clear or payload.tags is not None or row.tags is None:
                row.tags = current_tags

            # ------------ metadata_json ------------
            if payload.metadata_clear:
                row.metadata_json = None
            elif payload.metadata is not None:
                # fully replace when provided
                row.metadata_json = payload.metadata or None

            # ------------ thumbnail (key) ------------
            if payload.thumbnail_clear:
                row.thumbnail = None
            if thumbnail:
                _, ct = await _read_and_check(thumbnail, MAX_IMAGE_MB, IMAGE_TYPES)
                fname = make_uuid_name(thumbnail.filename, "thumbnail")
                res = await asyncio.to_thread(
                    gcs.upload_fileobj,
                    thumbnail.file,
                    fname,
                    f"{dest_prefix}/thumbnail",
                    False,
                    ct,
                )
                row.thumbnail = res["key"]

            # ------------ images (list of keys) ------------
            current_images: List[str] = list(row.images or [])
            if payload.images_clear:
                current_images = []

            new_image_keys: List[str] = []
            if images:
                for idx, img in enumerate(images):
                    _, ct = await _read_and_check(img, MAX_IMAGE_MB, IMAGE_TYPES)
                    fname = make_uuid_name(img.filename, f"img{idx:03d}")
                    res = await asyncio.to_thread(
                        gcs.upload_fileobj,
                        img.file,
                        fname,
                        f"{dest_prefix}/images",
                        False,
                        ct,
                    )
                    if res and res.get("key"):
                        new_image_keys.append(res["key"])

            if new_image_keys:
                if payload.images_mode == "replace":
                    current_images = new_image_keys
                else:
                    current_images.extend(new_image_keys)

            if payload.images_clear or new_image_keys or (row.images is None):
                row.images = current_images

            # ------------ file_links JSONB (top/side/bottom) ------------
            current_links: Dict[str, Any] = dict(getattr(row, "file_links", {}) or {})

            if payload.file_links_clear:
                current_links = {}

            # clear specific slots
            if payload.top_clear:
                current_links.pop("top", None)
            if payload.side_clear:
                current_links.pop("side", None)
            if payload.bottom_clear:
                current_links.pop("bottom", None)

            # upload new files for top/side/bottom
            for slot, uf in {
                "top": top,
                "side": side,
                "bottom": bottom,
            }.items():
                if not uf:
                    continue
                try:
                    meta = await _upload_one(uf, "nav", slot, image=False)
                    if meta:
                        current_links[slot] = meta
                except Exception as e:
                    logger.exception("[navigations.update] upload failed for %s", slot)
                    raise HTTPException(status_code=400, detail=str(e))

            row.file_links = current_links or None

            # updated_by
            row.updated_by = user_id

            # ------------ persist ------------
            t0 = time.monotonic()
            db.add(row)
            db.commit()
            db.refresh(row)
            logger.info(
                "[navigations.update] DB commit elapsed=%.3fs id=%s",
                time.monotonic() - t0,
                row.id,
            )

            # ------------ signed URLs (best-effort) ------------
            signed = {
                "thumbnail": None,
                "images": [],
                "file_links": {},
                "row_id": str(row.id),
            }

            # thumbnail
            if row.thumbnail:
                try:
                    signed["thumbnail"] = gcs.signed_get_url(row.thumbnail, expires_seconds=3600)
                except Exception:
                    signed["thumbnail"] = f"https://storage.googleapis.com/{gcs.bucket_name}/{row.thumbnail}"

            # images
            for key in row.images or []:
                try:
                    signed["images"].append(gcs.signed_get_url(key, expires_seconds=3600))
                except Exception:
                    signed["images"].append(f"https://storage.googleapis.com/{gcs.bucket_name}/{key}")

            # file_links
            for slot, meta in (row.file_links or {}).items():
                try:
                    signed["file_links"][slot] = gcs.signed_get_url(meta["key"], expires_seconds=3600)
                except Exception:
                    signed["file_links"][slot] = (
                        f"https://storage.googleapis.com/{gcs.bucket_name}/{meta['key']}"
                    )

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
            raise HTTPException(status_code=500, detail="Failed to update navigation")
        finally:
            db.close()
            # close any open file handles
            for uf in [thumbnail] + (images or []) + [top, side, bottom]:
                try:
                    if uf and getattr(uf, "file", None) and not uf.file.closed:
                        uf.file.close()
                except Exception:
                    pass


# ---------- serializer ----------

def _comp_nav_to_dict(m: CompNavigation) -> dict:
    return {
        "id": str(m.id) if getattr(m, "id", None) is not None else None,
        "name": m.name,
        "description": m.description,
        "thumbnail": getattr(m, "thumbnail", None),
        "images": getattr(m, "images", None),
        "template_id": str(m.template_id) if getattr(m, "template_id", None) else None,
        "file_links": getattr(m, "file_links", None) or {},
        "tags": getattr(m, "tags", []) or [],
        "metadata": getattr(m, "metadata_json", None) or {},
        "created_by": m.created_by,
        "updated_by": m.updated_by,
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }
