"""partner/chat/part1 -- partner profile + business model sketch.

Phase-1 / sub-conversation 1 of the partner planning flow. Captures the
primary partner profile (name, industry, responsibilities, pains, wishes)
and a high-level business-model sketch. All other deep analysis (capability
fit, objectives, scoping, implementation) belongs in subsequent parts.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from commands.partner._shared import (
    as_int_user_id,
    clean_str,
    conversation_to_dict,
    enforce_confirm_before_complete,
    merge_and_remove,
    merge_scalar,
    normalize_list,
    normalize_metadata,
    normalize_suggested_answers,
    safe_float,
)
from models.partner.tbl_partner_conversation_histories import PartnerConversationHistory
from models.partner.tbl_partner_conversations import PartnerConversation


try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("partner.chat.part1")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


_PART_NO = 1


_REMOVABLE_LIST_FIELDS = (
    "partner_job_responsibilities",
    "partner_pains",
    "partner_wishes",
    "partner_customer_segments",
    "partner_customer_relationships",
    "partner_channels",
    "partner_key_activities",
    "partner_key_resources",
    "partner_key_partners",
    "partner_revenue_streams",
)


def _normalize_removed(v: Any) -> Dict[str, List[str]]:
    """Normalize the model's ``removed`` block into a dict of clean string lists."""
    if not isinstance(v, dict):
        return {k: [] for k in _REMOVABLE_LIST_FIELDS}
    out: Dict[str, List[str]] = {}
    for k in _REMOVABLE_LIST_FIELDS:
        out[k] = normalize_list(v.get(k))
    return out


_EXTRACTED_FIELDS = (
    "partner_name",
    "partner_industry",
    "partner_job_responsibilities",
    "partner_pains",
    "partner_wishes",
    "partner_customer_segments",
    "partner_customer_relationships",
    "partner_channels",
    "partner_key_activities",
    "partner_key_resources",
    "partner_key_partners",
    "partner_revenue_streams",
)

_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}

_PARTNER_CHAT_JSON_SCHEMA = {
    "name": "partner_chat_response",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "reply_to_user",
            "assistant_suggested_answers",
            "conversation_summary",
            "confidence",
            "missing_fields",
            "reason",
            "extracted",
            "removed",
        ],
        "properties": {
            "status": {"type": "string", "enum": ["ask_followup", "confirm", "complete"]},
            "reply_to_user": {"type": "string"},
            "assistant_suggested_answers": _STRING_ARRAY,
            "conversation_summary": {"type": "string"},
            "confidence": {"type": "number"},
            "missing_fields": _STRING_ARRAY,
            "reason": {"type": "string"},
            "extracted": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_EXTRACTED_FIELDS),
                "properties": {
                    "partner_name": {"type": ["string", "null"]},
                    "partner_industry": {"type": ["string", "null"]},
                    **{
                        field: _STRING_ARRAY
                        for field in _EXTRACTED_FIELDS
                        if field not in ("partner_name", "partner_industry")
                    },
                },
            },
            "removed": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_REMOVABLE_LIST_FIELDS),
                "properties": {field: _STRING_ARRAY for field in _REMOVABLE_LIST_FIELDS},
            },
        },
    },
}


def _compute_missing_fields(snapshot: Dict[str, Any]) -> List[str]:
    missing = []

    if not clean_str(snapshot.get("partner_name")):
        missing.append("partner_name")

    if not clean_str(snapshot.get("partner_industry")):
        missing.append("partner_industry")

    if not normalize_list(snapshot.get("partner_job_responsibilities")):
        missing.append("partner_job_responsibilities")

    if not normalize_list(snapshot.get("partner_pains")):
        missing.append("partner_pains")

    if not normalize_list(snapshot.get("partner_wishes")):
        missing.append("partner_wishes")

    return missing


# Minimum number of entries required for a primary list field to be considered
# "saturated" enough to stop drilling.
_PRIMARY_LIST_MIN_ITEMS = 2

# After this many turns with primary fields saturated, force the conversation
# into `confirm` even if the model still wants to ask follow-ups. Sized to
# give room for the primary set (~5 turns) plus a short BMC sketch (~5 turns)
# before the safety net kicks in.
_PHASE1_MAX_TURNS_BEFORE_CONFIRM = 12


def _primary_fields_saturated(snapshot: Dict[str, Any], min_items: int = _PRIMARY_LIST_MIN_ITEMS) -> bool:
    """Return True when the 5 primary fields have enough content to stop drilling.

    Phase-1 scope backstop: once this is True and the model still keeps
    asking follow-ups after a few turns, the wrapper forces the conversation
    into `confirm` so we stop digging into implementation detail.
    """
    if not clean_str(snapshot.get("partner_name")):
        return False
    if not clean_str(snapshot.get("partner_industry")):
        return False
    for field in ("partner_job_responsibilities", "partner_pains", "partner_wishes"):
        if len(normalize_list(snapshot.get(field))) < min_items:
            return False
    return True


class RunPartnerChatPayload(BaseModel):
    conversation_key: Optional[str] = Field(default=None, description="Primary external conversation identifier")
    message: Optional[str] = Field(default=None, description="Latest user message; optional for greeting mode")
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


class RunPartnerProfileCommand(BaseCommand):
    name = "partner/chat/part1"
    schema = RunPartnerChatPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Partner"

    SYSTEM_PROMPT = """
You are a partner discovery assistant for Solitud.

This is the first stage of Solitud's planning workflow. Your only job in this
conversation is to understand the partner well enough for the next step.
Later stages will match Solitud's capabilities to the partner's needs, scope
solutions, and plan implementation. DO NOT try to do any of that here.

What you are trying to understand:

Primary set (the core of the conversation):
- partner_name        -> the company or team
- partner_industry    -> their industry
- partner_job_responsibilities -> what the team/person does day to day (2-5 short entries)
- partner_pains                -> biggest operational frustrations (2-5 short entries)
- partner_wishes               -> what they want to be different (2-5 short entries)

Business model sketch (SECOND sub-phase, still in this same conversation):
Once the primary set is covered, move into a short business-model sketch.
Capture ONE short line or a few keywords per field - just enough for a big
picture. Not every field needs to be filled; aim for the ones that naturally
come up.
- partner_customer_segments      (who they serve)
- partner_customer_relationships (how they relate to customers)
- partner_channels               (how they reach customers)
- partner_key_activities         (what they do to operate)
- partner_key_resources          (what they rely on)
- partner_key_partners           (important external partners)
- partner_revenue_streams        (how they make money)

Work the primary set first. When it is covered, transition naturally into the
business-model sketch. Keep every BMC entry at keyword or single-sentence
level - never multi-item operational detail.

Scope fence (critical):
- Stay high-level. Do NOT drill into SLAs, approval chains, escalation ladders,
  intake field lists, automation rules, priority tiers, reminder cadences,
  status models, integrations, or dashboards. Those belong in a later stage.
- If the user volunteers deep detail, compress it to a short keyword or
  one-line note and move on. Never draft specs, rule sets, or design artifacts.
- Never populate a list field with more than ~5 entries.
- This sub-conversation is ONLY about understanding the partner. You are NOT
  doing the next steps. NEVER use the words "overview", "scope", "lock scope",
  "pilot", "rollout", "next steps", "plan", "missing fields", "gaps",
  "import", "sample data", "phase 2", "phase 1", or "part 2". Do not propose
  imports, sample exports, pilots, or implementation steps. Anything that
  sounds like "let's start with a light pilot..." or "share the missing
  fields so we can lock scope" is forbidden -- another sub-conversation
  handles that.
- When the primary set + business-model sketch are covered, end with a plain
  summary and ask the user to confirm. Nothing more. The handoff to the next
  step is automatic and is not yours to narrate.

Tone:
- Talk like a knowledgeable colleague, not a form.
- Do NOT narrate your own process to the user. Never say "lean partner profile",
  "required fields", "first phase", "capture", "extract", or similar process talk.
- Do NOT prefix questions with explanations of what you are doing. Just ask.
    Bad:  "To start your lean partner profile, what's the partner name?"
    Good: "What's the company or team called?"
    Bad:  "I need to capture your pain points next."
    Good: "What's the biggest friction in the day-to-day right now?"
- Keep reply_to_user to 1-3 sentences. Warm, practical, never salesy.
- Ask ONE focused question per turn.
- Always provide 3-5 assistant_suggested_answers as short answer chips the user
  could click to reply quickly. These are hints, not full sentences.
- Never mention internal labels like part1, part2, part3, etc.
- Do NOT say "move on to partX" or "proceed to partX".
- If this step is confirmed and you refer to what comes after it, say only
  "the next step".
- Only if the user explicitly asks what the next step is, answer with the
  human step name "Solution Overview". Never use an internal part number.

State machine:
- status = "ask_followup" while any primary field is empty, OR while the
  business-model sketch has not been addressed at all.
- status = "confirm" once the primary set is covered and a reasonable
  business-model sketch exists. Present a brief summary and ask explicitly:
  "Does this look right? Anything to add, change, or remove?"
- status = "complete" only after the user's next reply clearly approves the
  summary ("yes", "looks good", "confirm", etc.). Stop capturing new items.
- Never set status = "complete" on the same turn as the first summary.
- missing_fields tracks ONLY the 5 primary fields: partner_name,
  partner_industry, partner_job_responsibilities, partner_pains, partner_wishes.
- confidence is a 0.0-1.0 estimate of overall completeness.
- conversation_summary is a short running summary, not a transcript replay.

Removal rules (IMPORTANT):
- If the user asks to remove, delete, drop, clear, forget, or get rid of items
  (e.g. "remove X", "delete the following", "no longer need Y"), list those items
  in the "removed" block using strings that exactly match the entries currently
  stored in the distilled conversation state.
- If a "Selected canvas items" context block is provided (either in the user
  message or as a system hint), treat those selected items as the targets of
  add/remove operations unless the user's text clearly overrides them.
- Do NOT include the same value in both "extracted" and "removed".
- Only list fields support "removed"; scalar fields (partner_name,
  partner_industry) cannot be removed via this block.
- If nothing is being removed, return empty arrays for every key in "removed".

The response shape is enforced by a JSON schema; you do not need to restate it.
Return only the JSON object that satisfies the schema.
""".strip()

    def _build_input_messages(
        self,
        conversation: Optional[PartnerConversation],
        history_rows: List[PartnerConversationHistory],
        user_message: str,
        metadata: Optional[dict] = None,
    ) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = [{"role": "system", "content": self.SYSTEM_PROMPT}]

        if conversation:
            raw_state = {
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
            # Drop empty/null values to reduce token usage on partial profiles.
            current_state = {
                k: v
                for k, v in raw_state.items()
                if v is not None and not (isinstance(v, list) and len(v) == 0)
            }
            if current_state:
                messages.append(
                    {
                        "role": "system",
                        "content": "Current distilled conversation state:\n" + json.dumps(current_state, ensure_ascii=False),
                    }
                )

        if isinstance(metadata, dict):
            selected_items = metadata.get("selected_canvas_items")
            active_tab = metadata.get("active_canvas_tab")
            if selected_items or active_tab:
                canvas_ctx = {
                    "active_canvas_tab": active_tab,
                    "selected_canvas_items": selected_items or [],
                }
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Canvas context from the client. If the user refers to "
                            "'the following', 'these', or 'selected items', resolve "
                            "those references to the entries below:\n"
                            + json.dumps(canvas_ctx, ensure_ascii=False)
                        ),
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
                "Sorry, could you say that again? What does your team handle, "
                "and what's the biggest friction right now?"
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
            "removed": {k: [] for k in _REMOVABLE_LIST_FIELDS},
        }

    def _normalize_result(self, parsed: dict) -> dict:
        extracted = parsed.get("extracted") or {}
        removed = _normalize_removed(parsed.get("removed"))

        # Guard against the model putting the same value in both blocks.
        for field in _REMOVABLE_LIST_FIELDS:
            added_lower = {s.lower() for s in normalize_list(extracted.get(field))}
            removed[field] = [x for x in removed[field] if x.lower() not in added_lower]

        normalized = {
            "status": clean_str(parsed.get("status")) or "ask_followup",
            "reply_to_user": clean_str(parsed.get("reply_to_user"))
            or "Can you share a bit more about your role, your pain points, and what you want to improve?",
            "assistant_suggested_answers": normalize_suggested_answers(parsed.get("assistant_suggested_answers")),
            "conversation_summary": clean_str(parsed.get("conversation_summary")),
            "confidence": safe_float(parsed.get("confidence")),
            "missing_fields": normalize_list(parsed.get("missing_fields")),
            "reason": clean_str(parsed.get("reason")),
            "extracted": {
                "partner_name": clean_str(extracted.get("partner_name")),
                "partner_industry": clean_str(extracted.get("partner_industry")),
                "partner_job_responsibilities": normalize_list(extracted.get("partner_job_responsibilities")),
                "partner_pains": normalize_list(extracted.get("partner_pains")),
                "partner_wishes": normalize_list(extracted.get("partner_wishes")),
                "partner_customer_segments": normalize_list(extracted.get("partner_customer_segments")),
                "partner_customer_relationships": normalize_list(extracted.get("partner_customer_relationships")),
                "partner_channels": normalize_list(extracted.get("partner_channels")),
                "partner_key_activities": normalize_list(extracted.get("partner_key_activities")),
                "partner_key_resources": normalize_list(extracted.get("partner_key_resources")),
                "partner_key_partners": normalize_list(extracted.get("partner_key_partners")),
                "partner_revenue_streams": normalize_list(extracted.get("partner_revenue_streams")),
            },
            "removed": removed,
        }
        return normalized

    def _build_intro_response(self) -> dict:
        return {
            "status": "ask_followup",
            "reply_to_user": (
                "Hi! What's the company or team we're talking about, and what do they do day to day?"
            ),
            "assistant_suggested_answers": [
                "We handle last-mile delivery and dispatch.",
                "We run partner onboarding and compliance.",
                "We coordinate internal approvals and back-office work.",
                "We manage property operations and maintenance.",
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
            "removed": {k: [] for k in _REMOVABLE_LIST_FIELDS},
        }

    async def execute(self, payload: RunPartnerChatPayload, user_id: Optional[str] = None) -> dict:
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        uid_int = as_int_user_id(user_id)
        db = SessionLocal()

        try:
            conversation_key = payload.conversation_key or str(uuid4())

            logger.info(
                "[partner.chat.part1] start conversation_key=%s user_id=%s",
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
                    part_no=_PART_NO,
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
                        "conversation": conversation_to_dict(conversation),
                        "reply_to_user": result["reply_to_user"],
                        "assistant_suggested_answers": result["assistant_suggested_answers"],
                        "reason": result["reason"],
                        "history_sequence_no": next_seq,
                    },
                    "perf_ms": round((time.monotonic() - t0) * 1000, 2),
                }

            # Pull recent history scoped to this part so we don't leak part2/part3
            # turns into the part1 prompt context.
            history_rows = (
                db.query(PartnerConversationHistory)
                .filter(
                    PartnerConversationHistory.conversation_id == conversation.id,
                    PartnerConversationHistory.part_no == _PART_NO,
                )
                .order_by(PartnerConversationHistory.sequence_no.desc())
                .limit(6)
                .all()
            )
            history_rows = list(reversed(history_rows))

            client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
            input_messages = self._build_input_messages(
                conversation=conversation,
                history_rows=history_rows,
                user_message=payload.message,
                metadata=payload.metadata,
            )

            completion = await client.chat.completions.create(
                model=payload.model_name,
                messages=input_messages,
                response_format={"type": "json_schema", "json_schema": _PARTNER_CHAT_JSON_SCHEMA},
            )

            raw_text = completion.choices[0].message.content or ""
            parsed = self._parse_model_output(raw_text)
            result = self._normalize_result(parsed)
            extracted = result["extracted"]
            removed = result["removed"]

            conversation.user_id = uid_int
            conversation.partner_name = merge_scalar(conversation.partner_name, extracted["partner_name"])
            conversation.partner_industry = merge_scalar(conversation.partner_industry, extracted["partner_industry"])

            conversation.partner_job_responsibilities = merge_and_remove(
                conversation.partner_job_responsibilities,
                extracted["partner_job_responsibilities"],
                removed["partner_job_responsibilities"],
            )
            conversation.partner_pains = merge_and_remove(
                conversation.partner_pains,
                extracted["partner_pains"],
                removed["partner_pains"],
            )
            conversation.partner_wishes = merge_and_remove(
                conversation.partner_wishes,
                extracted["partner_wishes"],
                removed["partner_wishes"],
            )

            conversation.partner_customer_segments = merge_and_remove(
                conversation.partner_customer_segments,
                extracted["partner_customer_segments"],
                removed["partner_customer_segments"],
            )
            conversation.partner_customer_relationships = merge_and_remove(
                conversation.partner_customer_relationships,
                extracted["partner_customer_relationships"],
                removed["partner_customer_relationships"],
            )
            conversation.partner_channels = merge_and_remove(
                conversation.partner_channels,
                extracted["partner_channels"],
                removed["partner_channels"],
            )
            conversation.partner_key_activities = merge_and_remove(
                conversation.partner_key_activities,
                extracted["partner_key_activities"],
                removed["partner_key_activities"],
            )
            conversation.partner_key_resources = merge_and_remove(
                conversation.partner_key_resources,
                extracted["partner_key_resources"],
                removed["partner_key_resources"],
            )
            conversation.partner_key_partners = merge_and_remove(
                conversation.partner_key_partners,
                extracted["partner_key_partners"],
                removed["partner_key_partners"],
            )
            conversation.partner_revenue_streams = merge_and_remove(
                conversation.partner_revenue_streams,
                extracted["partner_revenue_streams"],
                removed["partner_revenue_streams"],
            )

            conversation.conversation_summary = (
                clean_str(result["conversation_summary"]) or conversation.conversation_summary
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

            previous_status = conversation.status
            status = result["status"]
            if computed_missing and status == "complete":
                status = "ask_followup"

            # Phase-1 scope backstop: if primary fields are saturated and the
            # model keeps trying to drill (ask_followup), force `confirm` after
            # _PHASE1_MAX_TURNS_BEFORE_CONFIRM turns. This prevents runaway
            # implementation-level questioning once we already have what we need.
            if status == "ask_followup" and not computed_missing:
                primary_snapshot = {
                    "partner_name": conversation.partner_name,
                    "partner_industry": conversation.partner_industry,
                    "partner_job_responsibilities": conversation.partner_job_responsibilities,
                    "partner_pains": conversation.partner_pains,
                    "partner_wishes": conversation.partner_wishes,
                }
                if _primary_fields_saturated(primary_snapshot):
                    turn_count = (
                        db.query(func.count(PartnerConversationHistory.id))
                        .filter(
                            PartnerConversationHistory.conversation_id == conversation.id,
                            PartnerConversationHistory.part_no == _PART_NO,
                        )
                        .scalar()
                    ) or 0
                    if turn_count >= _PHASE1_MAX_TURNS_BEFORE_CONFIRM:
                        status = "confirm"

            status = enforce_confirm_before_complete(
                proposed_status=status,
                previous_status=previous_status,
                user_message=payload.message,
            )

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
                part_no=_PART_NO,
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
                    "conversation": conversation_to_dict(conversation),
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
            logger.exception("[partner.chat.part1] execute failed")
            raise
        finally:
            db.close()


__all__ = ["RunPartnerProfileCommand", "RunPartnerChatPayload"]
