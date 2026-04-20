from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.partner.tbl_partner_conversation_histories import PartnerConversationHistory
from models.partner.tbl_partner_conversations import PartnerConversation

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("partner.chat.get")
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


def _history_to_dict(m: PartnerConversationHistory) -> dict:
    return {
        "id": str(m.id),
        "conversation_id": str(m.conversation_id),
        "sequence_no": getattr(m, "sequence_no", None),
        "created_by": getattr(m, "created_by", None),
        "user_id": getattr(m, "user_id", None),
        "user_message": getattr(m, "user_message", None),
        "assistant_message": getattr(m, "assistant_message", None),
        "assistant_suggested_answers": getattr(m, "assistant_suggested_answers", None) or [],
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
        "status": getattr(m, "status", None),
        "reason": getattr(m, "reason", None),
        "model_name": getattr(m, "model_name", None),
        "prompt_version": getattr(m, "prompt_version", None),
        "metadata": getattr(m, "metadata_json", None) or {},
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
    }


class GetPartnerChatPayload(BaseModel):
    conversation_key: str = Field(..., description="Conversation key")
    include_history: bool = Field(default=True, description="Whether to include chat trail")
    limit: Optional[int] = Field(default=None, description="Optional max number of history rows")
    order: str = Field(default="asc", description="History order: asc or desc")

    @field_validator("conversation_key")
    @classmethod
    def validate_conversation_key(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("conversation_key is required")
        if len(s) > 255:
            raise ValueError("conversation_key must be <= 255 characters")
        return s

    @field_validator("limit")
    @classmethod
    def validate_limit(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        if v <= 0:
            raise ValueError("limit must be greater than 0")
        if v > 500:
            raise ValueError("limit must be <= 500")
        return v

    @field_validator("order")
    @classmethod
    def validate_order(cls, v: str) -> str:
        s = (v or "asc").strip().lower()
        if s not in {"asc", "desc"}:
            raise ValueError("order must be either 'asc' or 'desc'")
        return s


class GetPartnerChatCommand(BaseCommand):
    name = "partner/chat/get"
    schema = GetPartnerChatPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Partner"

    async def execute(self, payload: GetPartnerChatPayload, user_id: Optional[str] = None) -> dict:
        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        _ = _as_int_user_id(user_id)
        db = SessionLocal()

        try:
            logger.info(
                "[partner.chat.get] conversation_key=%s requester_user_id=%s",
                payload.conversation_key,
                user_id,
            )

            conversation = (
                db.query(PartnerConversation)
                .filter(PartnerConversation.conversation_key == payload.conversation_key)
                .first()
            )

            if conversation is None:
                raise HTTPException(status_code=404, detail="Conversation not found.")

            history_items: List[Dict[str, Any]] = []

            if payload.include_history:
                q = db.query(PartnerConversationHistory).filter(
                    PartnerConversationHistory.conversation_id == conversation.id
                )

                if payload.order == "desc":
                    q = q.order_by(PartnerConversationHistory.sequence_no.desc())
                else:
                    q = q.order_by(PartnerConversationHistory.sequence_no.asc())

                if payload.limit:
                    q = q.limit(payload.limit)

                history_rows = q.all()
                history_items = [_history_to_dict(row) for row in history_rows]

            return {
                "status": "ok",
                "data": {
                    "conversation": _conversation_to_dict(conversation),
                    "history": history_items,
                    "history_count": len(history_items),
                },
            }

        except HTTPException:
            raise
        except Exception:
            logger.exception("[partner.chat.get] execute failed")
            raise
        finally:
            db.close()


__all__ = ["GetPartnerChatCommand"]