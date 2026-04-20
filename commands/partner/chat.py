from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import HTTPException
from openai import OpenAI
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.partner.tbl_partner_conversation_histories import PartnerConversationHistory
from models.partner.tbl_partner_conversations import PartnerConversation

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("partner.chat.run")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


def _as_int_user_id(v: Optional[str]) -> Optional[int]:
    if not v:
        return None
    try:
        iv = int(v)
        return iv if iv > 0 else None
    except Exception:
        return None


def _clean_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _safe_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        fv = float(v)
        if fv < 0:
            return 0.0
        if fv > 1:
            return 1.0
        return fv
    except Exception:
        return None


def _normalize_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if not isinstance(v, list):
        return []

    out: List[str] = []
    seen = set()
    for item in v:
        s = _clean_str(item)
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _normalize_suggested_answers(v: Any) -> List[str]:
    return _normalize_list(v)


def _normalize_metadata(v: Any) -> Optional[dict]:
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
            raise ValueError(f"metadata must be valid JSON if provided as string: {e}")
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    try:
        return dict(v)
    except Exception:
        raise ValueError("metadata must be a JSON object or JSON string")


def _merge_scalar(old_v: Optional[str], new_v: Optional[str]) -> Optional[str]:
    return _clean_str(new_v) or _clean_str(old_v)


def _merge_list(old_v: Optional[List[str]], new_v: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    seen = set()

    for source in (old_v or [], new_v or []):
        for item in source:
            s = _clean_str(item)
            if not s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)

    return out


def _compute_missing_fields(snapshot: Dict[str, Any]) -> List[str]:
    missing = []

    if not _clean_str(snapshot.get("partner_name")):
        missing.append("partner_name")

    if not _clean_str(snapshot.get("partner_industry")):
        missing.append("partner_industry")

    if not _normalize_list(snapshot.get("partner_job_responsibilities")):
        missing.append("partner_job_responsibilities")

    if not _normalize_list(snapshot.get("partner_pains")):
        missing.append("partner_pains")

    if not _normalize_list(snapshot.get("partner_wishes")):
        missing.append("partner_wishes")

    return missing


def _conversation_to_dict(m: PartnerConversation) -> dict:
    return {
        "id": str(m.id),
        "created_by": getattr(m, "created_by", None),
        "user_id": getattr(m, "user_id", None),
        "conversation_key": getattr(m, "conversation_key", None),
        "status": getattr(m, "status", None),
        "partner_name": getattr(m, "partner_name", None),
        "partner_industry": getattr(m, "partner_industry", None),
        "partner_job_responsibilities": getattr(m, "partner_job_responsibilities", None) or [],
        "partner_pains": getattr(m, "partner_pains", None) or [],
        "partner_wishes": getattr(m, "partner_wishes", None) or [],
        "partner_customer_segments": getattr(m, "partner_customer_segments", None) or [],
        "partner_customer_relationships": getattr(m, "partner_customer_relationships", None) or [],
        "partner_channels": getattr(m, "partner_channels", None) or [],
        "partner_key_activities": getattr(m, "partner_key_activities", None) or [],
        "partner_key_resources": getattr(m, "partner_key_resources", None) or [],
        "partner_key_partners": getattr(m, "partner_key_partners", None) or [],
        "partner_revenue_streams": getattr(m, "partner_revenue_streams", None) or [],
        "conversation_summary": getattr(m, "conversation_summary", None),
        "confidence": float(m.confidence) if getattr(m, "confidence", None) is not None else None,
        "missing_fields": getattr(m, "missing_fields", None) or [],
        "last_user_message": getattr(m, "last_user_message", None),
        "last_assistant_message": getattr(m, "last_assistant_message", None),
        "assistant_suggested_answers": getattr(m, "assistant_suggested_answers", None) or [],
        "metadata": getattr(m, "metadata_json", None) or {},
        "updated_by": getattr(m, "updated_by", None),
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }


class RunPartnerChatPayload(BaseModel):
    conversation_key: Optional[str] = Field(default=None, description="Primary external conversation identifier")
    message: Optional[str] = Field(default=None, description="Latest user message; optional for greeting mode")
    model_name: str = Field(default="gpt-5", description="OpenAI model name")
    prompt_version: str = Field(default="v1", description="Prompt version")
    metadata: Optional[Any] = Field(default=None, description="Optional metadata as JSON object or JSON string")

    @field_validator("conversation_key")
    @classmethod
    def validate_conversation_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if not s:
            return None
        if len(s) > 255:
            raise ValueError("conversation_key must be <= 255 characters")
        return s

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None

    @field_validator("model_name")
    @classmethod
    def validate_model_name(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("model_name is required")
        return s

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, v: Any) -> Optional[dict]:
        return _normalize_metadata(v)


class RunPartnerChatCommand(BaseCommand):
    name = "partner/chat/run"
    schema = RunPartnerChatPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Partner"

    SYSTEM_PROMPT = """
You are a partner discovery chatbot for business workflow and solution scoping.

Your job is to extract structured partner information from the user's message and prior conversation context.

You must extract these fields when available:

Partner General Information
- partner_name
- partner_industry

Partner Profile
- partner_job_responsibilities
- partner_pains
- partner_wishes

Optional Business Model
- partner_customer_segments
- partner_customer_relationships
- partner_channels
- partner_key_activities
- partner_key_resources
- partner_key_partners
- partner_revenue_streams

Rules:
- Business model fields are optional. Only populate them if the user actually provided enough relevant information.
- Do not hallucinate business model information.
- Be helpful and guide the user to provide missing details.
- Provide assistant_suggested_answers as short clickable-style suggestions that can help the user continue.
- Keep reply_to_user concise, warm, and practical.
- confidence must be a number from 0 to 1.
- conversation_summary should be a concise running summary of the conversation.
- missing_fields should focus on the core required fields:
  partner_name, partner_industry, partner_job_responsibilities, partner_pains, partner_wishes

Return valid JSON only with this shape:

{
  "status": "ask_followup" | "confirm" | "complete",
  "reply_to_user": "string",
  "assistant_suggested_answers": ["string"],
  "conversation_summary": "string",
  "confidence": 0.0,
  "missing_fields": ["string"],
  "reason": "string",
  "extracted": {
    "partner_name": "string|null",
    "partner_industry": "string|null",
    "partner_job_responsibilities": ["string"],
    "partner_pains": ["string"],
    "partner_wishes": ["string"],
    "partner_customer_segments": ["string"],
    "partner_customer_relationships": ["string"],
    "partner_channels": ["string"],
    "partner_key_activities": ["string"],
    "partner_key_resources": ["string"],
    "partner_key_partners": ["string"],
    "partner_revenue_streams": ["string"]
  }
}
""".strip()

    def _build_input_messages(
        self,
        conversation: Optional[PartnerConversation],
        history_rows: List[PartnerConversationHistory],
        user_message: str,
    ) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = [{"role": "system", "content": self.SYSTEM_PROMPT}]

        if conversation:
            current_state = {
                "partner_name": conversation.partner_name,
                "partner_industry": conversation.partner_industry,
                "partner_job_responsibilities": conversation.partner_job_responsibilities or [],
                "partner_pains": conversation.partner_pains or [],
                "partner_wishes": conversation.partner_wishes or [],
                "partner_customer_segments": conversation.partner_customer_segments or [],
                "partner_customer_relationships": conversation.partner_customer_relationships or [],
                "partner_channels": conversation.partner_channels or [],
                "partner_key_activities": conversation.partner_key_activities or [],
                "partner_key_resources": conversation.partner_key_resources or [],
                "partner_key_partners": conversation.partner_key_partners or [],
                "partner_revenue_streams": conversation.partner_revenue_streams or [],
                "conversation_summary": conversation.conversation_summary,
                "missing_fields": conversation.missing_fields or [],
                "status": conversation.status,
                "confidence": float(conversation.confidence) if conversation.confidence is not None else None,
            }
            messages.append(
                {
                    "role": "system",
                    "content": "Current distilled conversation state:\n" + json.dumps(current_state, ensure_ascii=False),
                }
            )

        for row in history_rows:
            if row.user_message:
                messages.append({"role": "user", "content": row.user_message})
            if row.assistant_message:
                messages.append({"role": "assistant", "content": row.assistant_message})

        messages.append({"role": "user", "content": user_message})
        return messages

    def _parse_model_output(self, raw_text: str) -> dict:
        try:
            parsed = json.loads((raw_text or "").strip())
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        return {
            "status": "ask_followup",
            "reply_to_user": (
                "Thanks. I want to make sure I capture this accurately. "
                "Can you tell me your role, your main challenges, and what outcome you want most?"
            ),
            "assistant_suggested_answers": [
                "We handle onboarding and compliance for partners.",
                "Our biggest pain is manual follow-ups and missing documents.",
                "We want clearer status tracking and less back-and-forth.",
            ],
            "conversation_summary": "",
            "confidence": 0.2,
            "missing_fields": [
                "partner_name",
                "partner_industry",
                "partner_job_responsibilities",
                "partner_pains",
                "partner_wishes",
            ],
            "reason": "Model did not return valid JSON.",
            "extracted": {
                "partner_name": None,
                "partner_industry": None,
                "partner_job_responsibilities": [],
                "partner_pains": [],
                "partner_wishes": [],
                "partner_customer_segments": [],
                "partner_customer_relationships": [],
                "partner_channels": [],
                "partner_key_activities": [],
                "partner_key_resources": [],
                "partner_key_partners": [],
                "partner_revenue_streams": [],
            },
        }

    def _normalize_result(self, parsed: dict) -> dict:
        extracted = parsed.get("extracted") or {}

        normalized = {
            "status": _clean_str(parsed.get("status")) or "ask_followup",
            "reply_to_user": _clean_str(parsed.get("reply_to_user"))
            or "Can you share a bit more about your role, your pain points, and what you want to improve?",
            "assistant_suggested_answers": _normalize_suggested_answers(parsed.get("assistant_suggested_answers")),
            "conversation_summary": _clean_str(parsed.get("conversation_summary")),
            "confidence": _safe_float(parsed.get("confidence")),
            "missing_fields": _normalize_list(parsed.get("missing_fields")),
            "reason": _clean_str(parsed.get("reason")),
            "extracted": {
                "partner_name": _clean_str(extracted.get("partner_name")),
                "partner_industry": _clean_str(extracted.get("partner_industry")),
                "partner_job_responsibilities": _normalize_list(extracted.get("partner_job_responsibilities")),
                "partner_pains": _normalize_list(extracted.get("partner_pains")),
                "partner_wishes": _normalize_list(extracted.get("partner_wishes")),
                "partner_customer_segments": _normalize_list(extracted.get("partner_customer_segments")),
                "partner_customer_relationships": _normalize_list(extracted.get("partner_customer_relationships")),
                "partner_channels": _normalize_list(extracted.get("partner_channels")),
                "partner_key_activities": _normalize_list(extracted.get("partner_key_activities")),
                "partner_key_resources": _normalize_list(extracted.get("partner_key_resources")),
                "partner_key_partners": _normalize_list(extracted.get("partner_key_partners")),
                "partner_revenue_streams": _normalize_list(extracted.get("partner_revenue_streams")),
            },
        }
        return normalized

    def _build_intro_response(self) -> dict:
        return {
            "status": "ask_followup",
            "reply_to_user": (
                "Hi, I can help capture your business profile, responsibilities, pain points, wishes, "
                "and any business model details that are relevant. To start, what does your team mainly do today, "
                "and what is the biggest challenge you want to improve?"
            ),
            "assistant_suggested_answers": [
                "We handle partner onboarding and compliance.",
                "We coordinate internal approvals and operations.",
                "Our biggest issue is manual follow-ups and poor visibility.",
            ],
            "conversation_summary": "New conversation started. Awaiting partner discovery details.",
            "confidence": 0.1,
            "missing_fields": [
                "partner_name",
                "partner_industry",
                "partner_job_responsibilities",
                "partner_pains",
                "partner_wishes",
            ],
            "reason": "Conversation initialized without user message.",
            "extracted": {
                "partner_name": None,
                "partner_industry": None,
                "partner_job_responsibilities": [],
                "partner_pains": [],
                "partner_wishes": [],
                "partner_customer_segments": [],
                "partner_customer_relationships": [],
                "partner_channels": [],
                "partner_key_activities": [],
                "partner_key_resources": [],
                "partner_key_partners": [],
                "partner_revenue_streams": [],
            },
        }

    async def execute(self, payload: RunPartnerChatPayload, user_id: Optional[str] = None) -> dict:
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        uid_int = _as_int_user_id(user_id)
        db = SessionLocal()

        try:
            conversation_key = payload.conversation_key or str(uuid4())

            logger.info(
                "[partner.chat] start conversation_key=%s user_id=%s",
                conversation_key,
                user_id,
            )

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
                    missing_fields=[
                        "partner_name",
                        "partner_industry",
                        "partner_job_responsibilities",
                        "partner_pains",
                        "partner_wishes",
                    ],
                    updated_by=uid_int,
                    metadata_json=payload.metadata or {},
                )
                db.add(conversation)
                db.flush()
            else:
                conversation.user_id = uid_int
                conversation.updated_by = uid_int

            if not payload.message:
                result = self._build_intro_response()

                conversation.user_id = uid_int
                conversation.last_user_message = None
                conversation.last_assistant_message = result["reply_to_user"]
                conversation.assistant_suggested_answers = result["assistant_suggested_answers"]
                conversation.conversation_summary = result["conversation_summary"]
                conversation.confidence = result["confidence"]
                conversation.missing_fields = result["missing_fields"]
                conversation.status = result["status"]
                conversation.updated_by = uid_int

                current_metadata = dict(conversation.metadata_json or {})
                current_metadata.update(payload.metadata or {})
                current_metadata["prompt_version"] = payload.prompt_version
                conversation.metadata_json = current_metadata

                current_max_seq = (
                    db.query(func.max(PartnerConversationHistory.sequence_no))
                    .filter(PartnerConversationHistory.conversation_id == conversation.id)
                    .scalar()
                )
                next_seq = int(current_max_seq or 0) + 1

                history_row = PartnerConversationHistory(
                    conversation_id=conversation.id,
                    user_id=uid_int,
                    sequence_no=next_seq,
                    user_message=None,
                    assistant_message=result["reply_to_user"],
                    assistant_suggested_answers=result["assistant_suggested_answers"],
                    partner_name=conversation.partner_name,
                    partner_industry=conversation.partner_industry,
                    partner_job_responsibilities=conversation.partner_job_responsibilities or [],
                    partner_pains=conversation.partner_pains or [],
                    partner_wishes=conversation.partner_wishes or [],
                    partner_customer_segments=conversation.partner_customer_segments or [],
                    partner_customer_relationships=conversation.partner_customer_relationships or [],
                    partner_channels=conversation.partner_channels or [],
                    partner_key_activities=conversation.partner_key_activities or [],
                    partner_key_resources=conversation.partner_key_resources or [],
                    partner_key_partners=conversation.partner_key_partners or [],
                    partner_revenue_streams=conversation.partner_revenue_streams or [],
                    conversation_summary=result["conversation_summary"],
                    confidence=result["confidence"],
                    missing_fields=result["missing_fields"],
                    status=result["status"],
                    reason=result["reason"],
                    model_name=payload.model_name,
                    prompt_version=payload.prompt_version,
                    metadata_json=payload.metadata or {},
                    created_by=uid_int,
                )
                db.add(history_row)

                db.commit()
                db.refresh(conversation)

                return {
                    "status": "ok",
                    "data": {
                        "conversation": _conversation_to_dict(conversation),
                        "reply_to_user": result["reply_to_user"],
                        "assistant_suggested_answers": result["assistant_suggested_answers"],
                        "reason": result["reason"],
                        "history_sequence_no": next_seq,
                    },
                    "perf_ms": round((time.monotonic() - t0) * 1000, 2),
                }

            history_rows = (
                db.query(PartnerConversationHistory)
                .filter(PartnerConversationHistory.conversation_id == conversation.id)
                .order_by(PartnerConversationHistory.sequence_no.asc())
                .limit(12)
                .all()
            )

            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            input_messages = self._build_input_messages(
                conversation=conversation,
                history_rows=history_rows,
                user_message=payload.message,
            )

            completion = client.chat.completions.create(
                model=payload.model_name,
                messages=input_messages,
                response_format={"type": "json_object"},
            )

            raw_text = completion.choices[0].message.content or ""
            parsed = self._parse_model_output(raw_text)
            result = self._normalize_result(parsed)
            extracted = result["extracted"]

            conversation.user_id = uid_int
            conversation.partner_name = _merge_scalar(conversation.partner_name, extracted["partner_name"])
            conversation.partner_industry = _merge_scalar(conversation.partner_industry, extracted["partner_industry"])

            conversation.partner_job_responsibilities = _merge_list(
                conversation.partner_job_responsibilities, extracted["partner_job_responsibilities"]
            )
            conversation.partner_pains = _merge_list(conversation.partner_pains, extracted["partner_pains"])
            conversation.partner_wishes = _merge_list(conversation.partner_wishes, extracted["partner_wishes"])

            conversation.partner_customer_segments = _merge_list(
                conversation.partner_customer_segments, extracted["partner_customer_segments"]
            )
            conversation.partner_customer_relationships = _merge_list(
                conversation.partner_customer_relationships, extracted["partner_customer_relationships"]
            )
            conversation.partner_channels = _merge_list(
                conversation.partner_channels, extracted["partner_channels"]
            )
            conversation.partner_key_activities = _merge_list(
                conversation.partner_key_activities, extracted["partner_key_activities"]
            )
            conversation.partner_key_resources = _merge_list(
                conversation.partner_key_resources, extracted["partner_key_resources"]
            )
            conversation.partner_key_partners = _merge_list(
                conversation.partner_key_partners, extracted["partner_key_partners"]
            )
            conversation.partner_revenue_streams = _merge_list(
                conversation.partner_revenue_streams, extracted["partner_revenue_streams"]
            )

            conversation.conversation_summary = (
                _clean_str(result["conversation_summary"]) or conversation.conversation_summary
            )
            conversation.confidence = result["confidence"]
            conversation.last_user_message = payload.message
            conversation.last_assistant_message = result["reply_to_user"]
            conversation.assistant_suggested_answers = result["assistant_suggested_answers"]

            snapshot_for_missing = {
                "partner_name": conversation.partner_name,
                "partner_industry": conversation.partner_industry,
                "partner_job_responsibilities": conversation.partner_job_responsibilities,
                "partner_pains": conversation.partner_pains,
                "partner_wishes": conversation.partner_wishes,
            }
            computed_missing = _compute_missing_fields(snapshot_for_missing)
            conversation.missing_fields = computed_missing

            status = result["status"]
            if computed_missing and status == "complete":
                status = "ask_followup"
            conversation.status = status

            current_metadata = dict(conversation.metadata_json or {})
            current_metadata.update(payload.metadata or {})
            current_metadata["prompt_version"] = payload.prompt_version
            conversation.metadata_json = current_metadata
            conversation.updated_by = uid_int

            current_max_seq = (
                db.query(func.max(PartnerConversationHistory.sequence_no))
                .filter(PartnerConversationHistory.conversation_id == conversation.id)
                .scalar()
            )
            next_seq = int(current_max_seq or 0) + 1

            history_row = PartnerConversationHistory(
                conversation_id=conversation.id,
                user_id=uid_int,
                sequence_no=next_seq,
                user_message=payload.message,
                assistant_message=result["reply_to_user"],
                assistant_suggested_answers=result["assistant_suggested_answers"],
                partner_name=conversation.partner_name,
                partner_industry=conversation.partner_industry,
                partner_job_responsibilities=conversation.partner_job_responsibilities,
                partner_pains=conversation.partner_pains,
                partner_wishes=conversation.partner_wishes,
                partner_customer_segments=conversation.partner_customer_segments,
                partner_customer_relationships=conversation.partner_customer_relationships,
                partner_channels=conversation.partner_channels,
                partner_key_activities=conversation.partner_key_activities,
                partner_key_resources=conversation.partner_key_resources,
                partner_key_partners=conversation.partner_key_partners,
                partner_revenue_streams=conversation.partner_revenue_streams,
                conversation_summary=conversation.conversation_summary,
                confidence=result["confidence"],
                missing_fields=computed_missing,
                status=status,
                reason=result["reason"],
                model_name=payload.model_name,
                prompt_version=payload.prompt_version,
                metadata_json=payload.metadata or {},
                created_by=uid_int,
            )
            db.add(history_row)

            db.commit()
            db.refresh(conversation)

            return {
                "status": "ok",
                "data": {
                    "conversation": _conversation_to_dict(conversation),
                    "reply_to_user": result["reply_to_user"],
                    "assistant_suggested_answers": result["assistant_suggested_answers"],
                    "reason": result["reason"],
                    "history_sequence_no": next_seq,
                },
                "perf_ms": round((time.monotonic() - t0) * 1000, 2),
            }

        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("[partner.chat] execute failed")
            raise
        finally:
            db.close()


__all__ = ["RunPartnerChatCommand"]