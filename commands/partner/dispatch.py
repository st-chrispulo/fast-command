"""partner/chat/dispatch -- single front-door for the partner planning flow.

The frontend calls this endpoint with ``{ conversation_key?, message? }``.
The dispatcher reads the conversation's ``current_part`` pointer, forwards
the call to the matching sub-conversation command (part1 / part2 / part3 /
part4 / part5 / part6), and -- when that sub-conversation reports its local
status as ``complete`` -- advances the pointer and immediately invokes the
next stage with no message so the FE gets the next stage's first-pass
response in the same round-trip.

The response envelope intentionally hides per-part state machines from the
frontend:

    {
        "stage":        "customer_profile" | "solution_overview" | "objectives" |
                        "scope_limitations" | "actors_roles" | "entity_model" |
                        "complete",
        "stage_label":  "Customer Profile" | ...,
        "current_part": <int>,
        "reply_to_user": "...",
        "assistant_suggested_answers": [...],
        "conversation": { ... full row, all parts ... },
        "perf_ms": <float>,
    }
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from commands.partner._shared import (
    LAST_PART_NO,
    as_int_user_id,
    conversation_to_dict,
    get_local_status,
    get_stage_info,
    next_part_no,
    normalize_metadata,
)
from commands.partner.part1 import RunPartnerChatPayload, RunPartnerProfileCommand
from commands.partner.part2 import (
    RunPartnerSolutionOverviewCommand,
    RunPartnerSolutionOverviewPayload,
)
from commands.partner.part3 import (
    RunPartnerObjectivesCommand,
    RunPartnerObjectivesPayload,
)
from commands.partner.part4 import RunPartnerScopeCommand, RunPartnerScopePayload
from commands.partner.part5 import RunPartnerActorsCommand, RunPartnerActorsPayload
from commands.partner.part6 import (
    RunPartnerEntityModelCommand,
    RunPartnerEntityModelPayload,
)
from models.partner.tbl_partner_conversations import PartnerConversation


try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("partner.chat.dispatch")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


# Cap how many auto-advances can happen in a single dispatch call. Each
# advance is one extra OpenAI call, so we keep this small. In practice it
# only advances once per request -- this is just a safety net.
_MAX_AUTO_ADVANCES = 2


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


class DispatchPartnerChatPayload(BaseModel):
    conversation_key: Optional[str] = Field(default=None, description="Conversation key; omit on first call to start a new conversation")
    message: Optional[str] = Field(default=None, description="Latest user message; omit to fetch the active stage's first-pass response")
    model_name: str = Field(default="gpt-4o-mini", description="OpenAI model name")
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
        return normalize_metadata(v)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


class DispatchPartnerChatCommand(BaseCommand):
    name = "partner/chat/dispatch"
    schema = DispatchPartnerChatPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Partner"

    # The order here mirrors STAGE_REGISTRY in _shared.py.
    _PART_COMMANDS: Dict[int, Any] = {
        1: (RunPartnerProfileCommand, RunPartnerChatPayload),
        2: (RunPartnerSolutionOverviewCommand, RunPartnerSolutionOverviewPayload),
        3: (RunPartnerObjectivesCommand, RunPartnerObjectivesPayload),
        4: (RunPartnerScopeCommand, RunPartnerScopePayload),
        5: (RunPartnerActorsCommand, RunPartnerActorsPayload),
        6: (RunPartnerEntityModelCommand, RunPartnerEntityModelPayload),
    }

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _ensure_conversation(self, db, conversation_key: str, uid_int: Optional[int]) -> PartnerConversation:
        """Find or create the conversation row so current_part exists before we dispatch."""
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

    def _read_current_part(self, conversation_key: str) -> Optional[int]:
        db = SessionLocal()
        try:
            row = (
                db.query(PartnerConversation)
                .filter(PartnerConversation.conversation_key == conversation_key)
                .first()
            )
            if row is None:
                return None
            return int(row.current_part or 1)
        finally:
            db.close()

    def _advance_current_part_if_complete(
        self,
        conversation_key: str,
        active_part: int,
    ) -> Optional[int]:
        """If the active part's local status is `complete`, advance current_part.

        Returns the new current_part if an advance happened (or the same value
        if already at terminal state), or None if no advance is warranted.
        """
        db = SessionLocal()
        try:
            conversation = (
                db.query(PartnerConversation)
                .filter(PartnerConversation.conversation_key == conversation_key)
                .first()
            )
            if conversation is None:
                return None

            local_status = get_local_status(conversation, active_part)
            if local_status != "complete":
                return None

            nxt = next_part_no(active_part)
            if nxt is None:
                # We're past the last stage; mark the conversation as flow-complete
                # by parking current_part one past the last registered stage.
                terminal = LAST_PART_NO + 1
                if int(conversation.current_part or 1) != terminal:
                    conversation.current_part = terminal
                    db.commit()
                return terminal

            if int(conversation.current_part or 1) != nxt:
                conversation.current_part = nxt
                db.commit()
            return nxt
        finally:
            db.close()

    def _build_part_payload(
        self,
        part_no: int,
        conversation_key: str,
        message: Optional[str],
        model_name: str,
        prompt_version: str,
        metadata: Optional[dict],
    ):
        _, payload_cls = self._PART_COMMANDS[part_no]
        return payload_cls(
            conversation_key=conversation_key,
            message=message,
            model_name=model_name,
            prompt_version=prompt_version,
            metadata=metadata,
        )

    async def _invoke_part(
        self,
        part_no: int,
        conversation_key: str,
        message: Optional[str],
        model_name: str,
        prompt_version: str,
        metadata: Optional[dict],
        user_id: Optional[str],
    ) -> dict:
        command_cls, _ = self._PART_COMMANDS[part_no]
        payload = self._build_part_payload(
            part_no=part_no,
            conversation_key=conversation_key,
            message=message,
            model_name=model_name,
            prompt_version=prompt_version,
            metadata=metadata,
        )
        return await command_cls().execute(payload=payload, user_id=user_id)

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    async def execute(self, payload: DispatchPartnerChatPayload, user_id: Optional[str] = None) -> dict:
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        uid_int = as_int_user_id(user_id)
        conversation_key = payload.conversation_key or str(uuid4())

        logger.info(
            "[partner.chat.dispatch] start conversation_key=%s user_id=%s message_present=%s",
            conversation_key,
            user_id,
            bool(payload.message),
        )

        # Make sure the conversation exists and current_part is readable before
        # we forward to a sub-command. Each sub-command opens its own session,
        # so the row needs to be visible to them via its conversation_key.
        bootstrap_db = SessionLocal()
        try:
            self._ensure_conversation(bootstrap_db, conversation_key, uid_int)
        finally:
            bootstrap_db.close()

        active_part = self._read_current_part(conversation_key) or 1
        if active_part > LAST_PART_NO:
            # Flow already finished. Surface a stable "complete" envelope.
            return self._build_complete_response(conversation_key, t0)

        # First call: forward the user message to the active part.
        last_response = await self._invoke_part(
            part_no=active_part,
            conversation_key=conversation_key,
            message=payload.message,
            model_name=payload.model_name,
            prompt_version=payload.prompt_version,
            metadata=payload.metadata,
            user_id=user_id,
        )
        last_part_invoked = active_part

        # Auto-advance loop: while the active part has reported `complete`,
        # bump current_part and immediately invoke the next stage with no
        # message so the FE gets the next stage's first-pass right away.
        for _ in range(_MAX_AUTO_ADVANCES):
            advanced = self._advance_current_part_if_complete(conversation_key, last_part_invoked)
            if advanced is None:
                break  # active part isn't complete yet; nothing to advance
            if advanced > LAST_PART_NO:
                # The whole flow just finished. Use the latest response we have.
                break
            # Auto-invoke the next stage with no user message.
            last_response = await self._invoke_part(
                part_no=advanced,
                conversation_key=conversation_key,
                message=None,
                model_name=payload.model_name,
                prompt_version=payload.prompt_version,
                metadata=payload.metadata,
                user_id=user_id,
            )
            last_part_invoked = advanced

        return self._build_response(
            inner_response=last_response,
            conversation_key=conversation_key,
            t0=t0,
        )

    # ------------------------------------------------------------------
    # response shaping
    # ------------------------------------------------------------------

    def _build_response(self, inner_response: dict, conversation_key: str, t0: float) -> dict:
        inner_data = (inner_response or {}).get("data") or {}
        conversation = inner_data.get("conversation") or {}
        current_part = int(conversation.get("current_part") or 1)

        stage_key, stage_label = get_stage_info(current_part)

        return {
            "status": "ok",
            "data": {
                "stage": stage_key,
                "stage_label": stage_label,
                "current_part": current_part,
                "reply_to_user": inner_data.get("reply_to_user"),
                "assistant_suggested_answers": inner_data.get("assistant_suggested_answers") or [],
                "conversation": conversation,
                "history_sequence_no": inner_data.get("history_sequence_no"),
            },
            "perf_ms": round((time.monotonic() - t0) * 1000, 2),
        }

    def _build_complete_response(self, conversation_key: str, t0: float) -> dict:
        """Stable envelope for conversations that have already finished the flow."""
        db = SessionLocal()
        try:
            conversation = (
                db.query(PartnerConversation)
                .filter(PartnerConversation.conversation_key == conversation_key)
                .first()
            )
            conv_dict = conversation_to_dict(conversation) if conversation else {}
        finally:
            db.close()

        stage_key, stage_label = get_stage_info((conversation.current_part if conversation else None))

        return {
            "status": "ok",
            "data": {
                "stage": stage_key,
                "stage_label": stage_label,
                "current_part": int((conversation.current_part if conversation else LAST_PART_NO + 1)),
                "reply_to_user": None,
                "assistant_suggested_answers": [],
                "conversation": conv_dict,
                "history_sequence_no": None,
            },
            "perf_ms": round((time.monotonic() - t0) * 1000, 2),
        }


__all__ = ["DispatchPartnerChatCommand", "DispatchPartnerChatPayload"]
