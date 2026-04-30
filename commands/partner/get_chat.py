"""partner/chat/get -- read the conversation row + history.

Returns the full conversation envelope (part1 + part2 + part3 + part4 + part5 fields) and the
chat trail. Optional ``part_no`` filter restricts the trail to a single
sub-conversation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from commands.partner._shared import (
    as_int_user_id,
    conversation_to_dict,
    history_to_dict,
)
from models.partner.tbl_partner_conversation_histories import PartnerConversationHistory
from models.partner.tbl_partner_conversations import PartnerConversation


try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("partner.chat.get")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class GetPartnerChatPayload(BaseModel):
    conversation_key: str = Field(..., description="Conversation key")
    include_history: bool = Field(default=True, description="Whether to include chat trail")
    limit: Optional[int] = Field(default=None, description="Optional max number of history rows")
    order: str = Field(default="asc", description="History order: asc or desc")
    part_no: Optional[int] = Field(
        default=None,
        description="Optional sub-conversation filter: 1=part1 (profile), 2=part2 (solution overview), 3=part3 (objectives), 4=part4 (scope & limitations), 5=part5 (actors & roles)",
    )

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

    @field_validator("part_no")
    @classmethod
    def validate_part_no(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        if v < 1 or v > 9:
            raise ValueError("part_no must be between 1 and 9")
        return v


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

        _ = as_int_user_id(user_id)
        db = SessionLocal()

        try:
            logger.info(
                "[partner.chat.get] conversation_key=%s part_no=%s requester_user_id=%s",
                payload.conversation_key,
                payload.part_no,
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

                if payload.part_no is not None:
                    q = q.filter(PartnerConversationHistory.part_no == payload.part_no)

                if payload.order == "desc":
                    q = q.order_by(PartnerConversationHistory.sequence_no.desc())
                else:
                    q = q.order_by(PartnerConversationHistory.sequence_no.asc())

                if payload.limit:
                    q = q.limit(payload.limit)

                history_rows = q.all()
                history_items = [history_to_dict(row) for row in history_rows]

            return {
                "status": "ok",
                "data": {
                    "conversation": conversation_to_dict(conversation),
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
