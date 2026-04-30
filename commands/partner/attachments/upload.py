"""partner/conversations/attachments/upload -- upload + summarize one attachment.

Flow per request:
  1. Validate auth, payload, and the file (size / MIME).
  2. Resolve or create the partner conversation (by conversation_key).
  3. Upload bytes to GCS under uploads/partner/<conversation_id>/files/<...>.
  4. Insert the attachment row with status='uploaded'.
  5. Run the extract -> summarize -> apply pipeline:
        * extract: read the file's bytes into text/images
        * summarize: ONE LLM call -> summary_text + summary + extracted_fields
        * apply: upsert high-confidence findings into the conversation row,
                 stash the rest as candidates (see _attachment_apply.py)
  6. Update the attachment row with summary + applied/candidate fields and
     status='ready'.
  7. Return a small envelope the FE can use to show the user what happened.

Failure modes are recorded on the attachment row's ``status`` /
``extraction_error`` so the FE can retry or surface a useful message.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import time
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from commands.partner._attachment_apply import apply_extracted_fields
from commands.partner._attachment_extract import prepare_content
from commands.partner._attachment_summarize import (
    PROMPT_VERSION,
    summarize_attachment,
)
from commands.partner._shared import as_int_user_id, normalize_metadata
from integrations.gcs.gcs import get_gcs
from models.partner.tbl_partner_conversation_attachments import (
    PartnerConversationAttachment,
)
from models.partner.tbl_partner_conversations import PartnerConversation

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("partner.attachments.upload")
except Exception:
    logger = logging.getLogger(__name__)


# --- limits / config -----------------------------------------------------

# Per-file size cap. Mirrors the lenient cap used in components/contents/create.
MAX_FILE_MB = 50

# Overall sync timeout for the whole upload+summarize flow. The summarizer
# itself has its own timeout but we keep a hard ceiling here so the request
# never hangs.
PIPELINE_TIMEOUT_S = 30.0

_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FALLBACK_MIME = {
    ".webp": "image/webp",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".pdf":  "application/pdf",
    ".csv":  "text/csv",
    ".txt":  "text/plain",
    ".md":   "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


# --- helpers -------------------------------------------------------------


def _safe_name(filename: str, default_stem: str = "file") -> str:
    name = filename or ""
    stem, ext = os.path.splitext(name)
    stem = (stem or default_stem).strip()
    stem = _SAFE_CHARS_RE.sub("_", stem) or default_stem
    ext = (ext or "").lower().lstrip(".") or "bin"
    return f"{stem}.{uuid4()}.{ext}"


def _resolve_content_type(upload: UploadFile) -> str:
    ct = (getattr(upload, "content_type", None) or "").strip()
    if ct and ct != "application/octet-stream":
        return ct
    name = getattr(upload, "filename", "") or ""
    ext = os.path.splitext(name)[1].lower()
    if ext in _FALLBACK_MIME:
        return _FALLBACK_MIME[ext]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


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


def _read_all(upload: UploadFile) -> bytes:
    f = getattr(upload, "file", None)
    if f is None:
        raise HTTPException(status_code=400, detail="Empty upload (no file).")
    try:
        f.seek(0)
    except Exception:
        pass
    blob = f.read() or b""
    try:
        f.seek(0)
    except Exception:
        pass
    return blob


def _safe_close(upload: Optional[UploadFile]) -> None:
    if not upload:
        return
    try:
        f = getattr(upload, "file", None)
        if f is not None and not getattr(f, "closed", False):
            f.close()
    except Exception:
        return


def _signed_url(gcs: Any, key: Optional[str], expires_seconds: int = 3600) -> Optional[str]:
    if not key:
        return None
    try:
        return gcs.signed_get_url(key, expires_seconds=expires_seconds)
    except Exception:
        return None


# --- payload -------------------------------------------------------------


class UploadPartnerAttachmentPayload(BaseModel):
    conversation_key: str = Field(..., description="Conversation to attach the file to")
    metadata: Optional[Any] = Field(default=None, description="Optional metadata as JSON object or string")

    @field_validator("conversation_key")
    @classmethod
    def validate_conversation_key(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("conversation_key is required")
        if len(s) > 255:
            raise ValueError("conversation_key must be <= 255 characters")
        return s

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, v: Any) -> Optional[dict]:
        return normalize_metadata(v)


# --- command -------------------------------------------------------------


class UploadPartnerAttachmentCommand(BaseCommand):
    """Upload one file to a partner conversation and run the summarizer."""

    name = "partner/conversations/attachments/upload"
    schema = UploadPartnerAttachmentPayload
    require_auth = True
    method = "post"
    type = "file_upload"
    group = "Partner"

    file_fields = [("attachment", False)]
    base_folder = "uploads/partner"

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _ensure_conversation(
        self,
        db,
        conversation_key: str,
        uid_int: Optional[int],
    ) -> PartnerConversation:
        conversation = (
            db.query(PartnerConversation)
            .filter(PartnerConversation.conversation_key == conversation_key)
            .first()
        )
        if conversation is None:
            conversation = PartnerConversation(
                created_by=uid_int,
                user_id=uid_int,
                conversation_key=conversation_key,
                status="ask_followup",
                current_part=1,
                missing_fields=[
                    "partner_name",
                    "partner_industry",
                    "partner_job_responsibilities",
                    "partner_pains",
                    "partner_wishes",
                ],
                updated_by=uid_int,
                metadata_json={},
            )
            db.add(conversation)
            db.commit()
            db.refresh(conversation)
        return conversation

    def _validate_upload(self, upload: UploadFile) -> str:
        ct = _resolve_content_type(upload)
        size = _file_size_bytes(upload)
        if size and size > MAX_FILE_MB * 1024 * 1024:
            raise HTTPException(
                status_code=413,
                detail=f"File '{upload.filename}' exceeds {MAX_FILE_MB}MB limit",
            )
        return ct

    @staticmethod
    async def _gcs_upload(
        gcs: Any,
        upload: UploadFile,
        *,
        dest_prefix: str,
        content_type: str,
    ) -> str:
        filename_key = _safe_name(getattr(upload, "filename", "") or "", "file")
        fileobj = upload.file
        try:
            fileobj.seek(0)
        except Exception:
            pass

        def _call() -> dict:
            return gcs.upload_fileobj(
                fileobj=fileobj,
                filename=filename_key,
                dest_prefix=dest_prefix,
                public=False,
                content_type=content_type,
            )

        res = await asyncio.to_thread(_call)
        key = (res or {}).get("key")
        if not key:
            raise HTTPException(status_code=500, detail="GCS upload failed.")
        return key

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    async def execute(
        self,
        payload: UploadPartnerAttachmentPayload,
        attachment: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")
        if attachment is None:
            raise HTTPException(status_code=400, detail="No file provided in 'attachment'.")

        uid_int = as_int_user_id(user_id)
        content_type = self._validate_upload(attachment)

        # Read once; we'll need bytes for both GCS upload and the extractor.
        # (GCS uploader rewinds, but the extractor pass needs its own copy.)
        blob = _read_all(attachment)
        size_bytes = len(blob)
        if size_bytes == 0:
            raise HTTPException(status_code=400, detail="Empty file.")

        gcs = get_gcs()

        db = SessionLocal()
        attachment_row: Optional[PartnerConversationAttachment] = None
        try:
            conversation = self._ensure_conversation(db, payload.conversation_key, uid_int)
            dest_prefix = f"{self.base_folder}/{conversation.id}"
            gcs_key = await self._gcs_upload(
                gcs,
                attachment,
                dest_prefix=dest_prefix,
                content_type=content_type,
            )

            # Insert the row up front so it exists even if summarization fails.
            attachment_row = PartnerConversationAttachment(
                conversation_id=conversation.id,
                conversation_key=payload.conversation_key,
                user_id=uid_int,
                filename=attachment.filename or "file",
                gcs_key=gcs_key,
                content_type=content_type,
                size_bytes=size_bytes,
                status="uploaded",
                metadata_json=payload.metadata or None,
                created_by=uid_int,
            )
            db.add(attachment_row)
            db.commit()
            db.refresh(attachment_row)

            # ---------- pipeline: extract -> summarize -> apply ----------
            try:
                attachment_row.status = "extracting"
                db.commit()

                async def _pipeline() -> Dict[str, Any]:
                    prepared = await asyncio.to_thread(
                        prepare_content,
                        blob,
                        filename=attachment_row.filename,
                        content_type=content_type,
                    )
                    summarized = await summarize_attachment(
                        prepared=prepared,
                        filename=attachment_row.filename,
                        timeout=PIPELINE_TIMEOUT_S - 2.0,  # leave a budget for IO
                    )
                    return {"prepared": prepared, "summarized": summarized}

                result = await asyncio.wait_for(_pipeline(), timeout=PIPELINE_TIMEOUT_S)
                summarized = result["summarized"]
                prepared = result["prepared"]

                if summarized.error:
                    attachment_row.status = "failed"
                    attachment_row.extraction_error = summarized.error
                    db.commit()
                    return self._build_response(
                        attachment_row=attachment_row,
                        gcs=gcs,
                        applied={},
                        candidates={},
                        t0=t0,
                    )

                attachment_row.status = "summarizing"
                attachment_row.classification = summarized.classification or prepared.classification
                attachment_row.summary = summarized.summary or {}
                attachment_row.summary_text = summarized.summary_text or None
                attachment_row.extracted_fields = summarized.extracted_fields or {}
                attachment_row.model_provider = summarized.provider
                attachment_row.model_name = summarized.model
                attachment_row.prompt_version = summarized.prompt_version or PROMPT_VERSION

                # Upsert into the conversation row.
                outcome = apply_extracted_fields(
                    conversation,
                    extracted_fields=summarized.extracted_fields or {},
                    attachment_id=attachment_row.id,
                    filename=attachment_row.filename,
                )
                attachment_row.applied_fields = outcome.applied_fields or {}
                attachment_row.candidate_fields = outcome.candidate_fields or {}
                attachment_row.status = "ready"
                conversation.updated_by = uid_int
                db.commit()
                db.refresh(attachment_row)

                return self._build_response(
                    attachment_row=attachment_row,
                    gcs=gcs,
                    applied=outcome.applied_fields,
                    candidates=outcome.candidate_fields,
                    t0=t0,
                )
            except asyncio.TimeoutError:
                attachment_row.status = "failed"
                attachment_row.extraction_error = (
                    f"pipeline timed out after {PIPELINE_TIMEOUT_S}s"
                )
                db.commit()
                logger.warning(
                    "[partner.attachments.upload] timeout attachment_id=%s",
                    attachment_row.id,
                )
                return self._build_response(
                    attachment_row=attachment_row,
                    gcs=gcs,
                    applied={},
                    candidates={},
                    t0=t0,
                )
            except Exception as e:
                logger.exception("[partner.attachments.upload] pipeline failed")
                attachment_row.status = "failed"
                attachment_row.extraction_error = f"{type(e).__name__}: {e}"
                db.commit()
                return self._build_response(
                    attachment_row=attachment_row,
                    gcs=gcs,
                    applied={},
                    candidates={},
                    t0=t0,
                )
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            logger.exception("[partner.attachments.upload] failed")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            db.close()
            _safe_close(attachment)

    # ------------------------------------------------------------------
    # response shaping
    # ------------------------------------------------------------------

    @staticmethod
    def _build_response(
        *,
        attachment_row: PartnerConversationAttachment,
        gcs: Any,
        applied: Dict[str, Any],
        candidates: Dict[str, Any],
        t0: float,
    ) -> dict:
        return {
            "status": "ok",
            "data": {
                "attachment": _attachment_to_dict(attachment_row, gcs),
                "applied_fields": applied or {},
                "candidate_fields": candidates or {},
            },
            "perf_ms": round((time.monotonic() - t0) * 1000, 2),
        }


def _attachment_to_dict(m: PartnerConversationAttachment, gcs: Any) -> dict:
    return {
        "id": str(m.id),
        "conversation_id": str(m.conversation_id) if m.conversation_id else None,
        "conversation_key": m.conversation_key,
        "filename": m.filename,
        "gcs_key": m.gcs_key,
        "signed_url": _signed_url(gcs, m.gcs_key),
        "content_type": m.content_type,
        "size_bytes": m.size_bytes,
        "status": m.status,
        "classification": m.classification,
        "extraction_error": m.extraction_error,
        "summary_text": m.summary_text,
        "summary": m.summary or {},
        "model_provider": m.model_provider,
        "model_name": m.model_name,
        "prompt_version": m.prompt_version,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


__all__ = [
    "UploadPartnerAttachmentCommand",
    "UploadPartnerAttachmentPayload",
]
