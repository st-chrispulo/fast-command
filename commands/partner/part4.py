"""partner/chat/part4 -- scope & limitations.

Sub-conversation 4 of the partner planning flow. Reads the partner profile,
the solution overview, and the objectives, then
derives a global engagement view:

  - scope_in_scope
  - scope_out_of_scope
  - scope_assumptions
  - scope_dependencies
  - scope_limitations

This is the first stage that turns the earlier discovery into a global scope
boundary. Per-match `fit="not_fit"` judgments remain inside the solution
overview matches; this stage decides the engagement-level scope and exclusions.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

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
    normalize_list,
    normalize_metadata,
    normalize_suggested_answers,
    safe_float,
)
from models.partner.tbl_partner_conversation_histories import PartnerConversationHistory
from models.partner.tbl_partner_conversations import PartnerConversation


try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("partner.chat.part4")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


_PART_NO = 4
_PART4_MAX_TURNS_BEFORE_CONFIRM = 8

_SCOPE_LIST_FIELDS = (
    "scope_in_scope",
    "scope_out_of_scope",
    "scope_assumptions",
    "scope_dependencies",
    "scope_limitations",
)

_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}

_SCOPE_JSON_SCHEMA = {
    "name": "partner_scope_response",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "reply_to_user",
            "assistant_suggested_answers",
            "scope_summary",
            "extracted",
            "removed",
            "missing_fields",
            "confidence",
            "reason",
        ],
        "properties": {
            "status": {"type": "string", "enum": ["ask_followup", "confirm", "complete"]},
            "reply_to_user": {"type": "string"},
            "assistant_suggested_answers": _STRING_ARRAY,
            "scope_summary": {"type": "string"},
            "extracted": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_SCOPE_LIST_FIELDS),
                "properties": {field: _STRING_ARRAY for field in _SCOPE_LIST_FIELDS},
            },
            "removed": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_SCOPE_LIST_FIELDS),
                "properties": {field: _STRING_ARRAY for field in _SCOPE_LIST_FIELDS},
            },
            "missing_fields": _STRING_ARRAY,
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
    },
}


def _profile_payload_from_conversation(conversation: PartnerConversation) -> Dict[str, Any]:
    return {
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
    }


def _solution_overview_payload(conversation: PartnerConversation) -> Dict[str, Any]:
    return {
        "solution_overview_summary": conversation.solution_overview_summary,
        "solution_overview_matches": conversation.solution_overview_matches or [],
        "solution_overview_capability_fits": conversation.solution_overview_capability_fits or [],
        "solution_overview_recommended_focus": conversation.solution_overview_recommended_focus or [],
    }


def _objectives_payload(conversation: PartnerConversation) -> Dict[str, Any]:
    return {
        "objectives_summary": conversation.objectives_summary,
        "objectives_high_level": conversation.objectives_high_level or [],
        "objectives_intent": conversation.objectives_intent or [],
        "objectives_success_criteria": conversation.objectives_success_criteria or [],
        "objectives_constraints": conversation.objectives_constraints or [],
    }


def _prerequisites_met(conversation: PartnerConversation) -> bool:
    if not clean_str(conversation.partner_name):
        return False
    has_solution_view = bool(
        clean_str(conversation.solution_overview_summary)
        or (conversation.solution_overview_matches or [])
        or (conversation.solution_overview_recommended_focus or [])
    )
    has_objectives_view = bool(
        clean_str(conversation.objectives_summary)
        or (conversation.objectives_high_level or [])
        or (conversation.objectives_intent or [])
        or (conversation.objectives_success_criteria or [])
        or (conversation.objectives_constraints or [])
    )
    return has_solution_view and has_objectives_view


def _normalize_removed_block(v: Any) -> Dict[str, List[str]]:
    if not isinstance(v, dict):
        return {k: [] for k in _SCOPE_LIST_FIELDS}
    return {k: normalize_list(v.get(k)) for k in _SCOPE_LIST_FIELDS}


class RunPartnerScopePayload(BaseModel):
    conversation_key: str = Field(..., description="Conversation key from part1/part2/part3")
    message: Optional[str] = Field(default=None, description="Optional user message; omit on first call to get the initial pass")
    model_name: str = Field(default="gpt-4o-mini", description="OpenAI model name")
    prompt_version: str = Field(default="v1", description="Prompt version")
    metadata: Optional[Any] = Field(default=None, description="Optional metadata as JSON object or JSON string")

    @field_validator("conversation_key")
    @classmethod
    def validate_conversation_key(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("conversation_key is required")
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


class RunPartnerScopeCommand(BaseCommand):
    name = "partner/chat/part4"
    schema = RunPartnerScopePayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Partner"

    SYSTEM_PROMPT = """
You are Solitud's scope and limitations analyst.

Your job in this sub-conversation is to turn the earlier discovery into a
global engagement boundary. You are reading the partner profile, the
solution overview, and the objectives, then articulating what
this engagement would include, exclude, assume, depend on, and be limited by.

Capture five buckets:
- scope_in_scope     : short deliverables / areas the engagement should cover.
- scope_out_of_scope : explicit exclusions, deferrals, or items outside the
                       engagement boundary.
- scope_assumptions  : working assumptions the scope depends on.
- scope_dependencies : external dependencies or prerequisites.
- scope_limitations  : limits that shape the boundary (technical,
                       capacity, operational, or delivery-boundary limits).

Rules:
- This is a GLOBAL view. Do not create a per-pain/per-wish list here.
- Use the solution-overview fit judgments, especially any `fit = "not_fit"` entries, as
  evidence when deciding global out-of-scope items. But do not merely copy
  every match; synthesize the boundary.
- Do NOT ask about budget or timeline in this stage.
- Do NOT redesign implementation details, task flows, approval chains,
  timelines, or rollout plans.
- Keep each list item short: a few words or one sentence.

Tone:
- 1-3 sentence reply_to_user. Practical, not salesy.
- Ask ONE focused question per turn unless you are presenting the summary for
  confirmation.
- 3-5 short assistant_suggested_answers as quick-reply chips.
- Do NOT narrate process.
- Never mention internal labels like part1, part2, part3, etc.
- Do NOT say "move on to partX" or "proceed to partX".
- If this step is confirmed and you refer to what comes after it, say only
  "the next step".
- Only if the user explicitly asks what the next step is, answer with the
  human step name "Actors & Roles". Never use an internal part number.

State machine:
- status = "ask_followup" while any of the five buckets is empty or too thin.
- status = "confirm" once you have a coherent first-pass scope. Present a
  concise summary and ask explicitly: "Does this look right? Anything to add, change, or remove?"
- status = "complete" only after the user's next reply clearly approves the
  summary ("yes", "looks good", "confirm", etc.).
- Never set status = "complete" on the same turn as the first summary.

Removal rules:
- If the user asks to remove items, list them in the "removed" block using
  strings that exactly match the current stored values.
- Do NOT include the same value in both "extracted" and "removed".

missing_fields lists which of the five buckets still need work, using the
field names above.

Return only the JSON object that satisfies the response schema.
""".strip()

    def _build_input_messages(
        self,
        conversation: PartnerConversation,
        history_rows: List[PartnerConversationHistory],
        user_message: Optional[str],
        metadata: Optional[dict] = None,
    ) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "system",
                "content": "Partner profile:\n"
                + json.dumps(_profile_payload_from_conversation(conversation), ensure_ascii=False),
            },
            {
                "role": "system",
                "content": "Solution overview:\n"
                + json.dumps(_solution_overview_payload(conversation), ensure_ascii=False),
            },
            {
                "role": "system",
                "content": "Objectives:\n"
                + json.dumps(_objectives_payload(conversation), ensure_ascii=False),
            },
        ]

        prior_state = {
            "scope_status": conversation.scope_status,
            "scope_summary": conversation.scope_summary,
            "scope_in_scope": conversation.scope_in_scope or [],
            "scope_out_of_scope": conversation.scope_out_of_scope or [],
            "scope_assumptions": conversation.scope_assumptions or [],
            "scope_dependencies": conversation.scope_dependencies or [],
            "scope_limitations": conversation.scope_limitations or [],
            "scope_missing_fields": conversation.scope_missing_fields or [],
            "scope_confidence": (
                float(conversation.scope_confidence)
                if conversation.scope_confidence is not None
                else None
            ),
        }
        prior_state = {k: v for k, v in prior_state.items() if v not in (None, [], "")}
        if prior_state:
            messages.append(
                {
                    "role": "system",
                    "content": "Current scope state:\n" + json.dumps(prior_state, ensure_ascii=False),
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

        if user_message:
            messages.append({"role": "user", "content": user_message})
        else:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Produce the first-pass scope and limitations now. Derive the "
                        "global boundary from the captured profile, solution overview, "
                        "and objectives."
                    ),
                }
            )

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
                "I had trouble with that response. What's the one thing this engagement "
                "should definitely include or definitely exclude?"
            ),
            "assistant_suggested_answers": [
                "Definitely include the core workflow",
                "Exclude anything outside the web app",
                "Assume we can access the current system",
            ],
            "scope_summary": "",
            "extracted": {k: [] for k in _SCOPE_LIST_FIELDS},
            "removed": {k: [] for k in _SCOPE_LIST_FIELDS},
            "missing_fields": list(_SCOPE_LIST_FIELDS),
            "confidence": 0.0,
            "reason": "Model did not return valid JSON.",
        }

    def _normalize_result(self, parsed: dict) -> dict:
        extracted_raw = parsed.get("extracted") or {}
        removed_raw = _normalize_removed_block(parsed.get("removed"))
        extracted = {k: normalize_list(extracted_raw.get(k)) for k in _SCOPE_LIST_FIELDS}

        for k in _SCOPE_LIST_FIELDS:
            added_lower = {s.lower() for s in extracted[k]}
            removed_raw[k] = [x for x in removed_raw[k] if x.lower() not in added_lower]

        return {
            "status": clean_str(parsed.get("status")) or "ask_followup",
            "reply_to_user": clean_str(parsed.get("reply_to_user"))
            or "What should this engagement definitely include or exclude?",
            "assistant_suggested_answers": normalize_suggested_answers(parsed.get("assistant_suggested_answers")),
            "scope_summary": clean_str(parsed.get("scope_summary")),
            "extracted": extracted,
            "removed": removed_raw,
            "missing_fields": normalize_list(parsed.get("missing_fields")),
            "confidence": safe_float(parsed.get("confidence")),
            "reason": clean_str(parsed.get("reason")),
        }

    async def execute(self, payload: RunPartnerScopePayload, user_id: Optional[str] = None) -> dict:
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        uid_int = as_int_user_id(user_id)
        db = SessionLocal()

        try:
            logger.info(
                "[partner.chat.part4] start conversation_key=%s user_id=%s",
                payload.conversation_key,
                user_id,
            )

            conversation = (
                db.query(PartnerConversation)
                .filter(PartnerConversation.conversation_key == payload.conversation_key)
                .first()
            )

            if conversation is None:
                raise HTTPException(status_code=404, detail="Conversation not found. Run part1 first.")

            if not _prerequisites_met(conversation):
                raise HTTPException(
                    status_code=409,
                    detail="Earlier discovery is too thin for scope & limitations. Complete part2 and part3 first.",
                )

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
                response_format={"type": "json_schema", "json_schema": _SCOPE_JSON_SCHEMA},
            )

            raw_text = completion.choices[0].message.content or ""
            parsed = self._parse_model_output(raw_text)
            result = self._normalize_result(parsed)
            extracted = result["extracted"]
            removed = result["removed"]

            conversation.scope_in_scope = merge_and_remove(
                conversation.scope_in_scope,
                extracted["scope_in_scope"],
                removed["scope_in_scope"],
            )
            conversation.scope_out_of_scope = merge_and_remove(
                conversation.scope_out_of_scope,
                extracted["scope_out_of_scope"],
                removed["scope_out_of_scope"],
            )
            conversation.scope_assumptions = merge_and_remove(
                conversation.scope_assumptions,
                extracted["scope_assumptions"],
                removed["scope_assumptions"],
            )
            conversation.scope_dependencies = merge_and_remove(
                conversation.scope_dependencies,
                extracted["scope_dependencies"],
                removed["scope_dependencies"],
            )
            conversation.scope_limitations = merge_and_remove(
                conversation.scope_limitations,
                extracted["scope_limitations"],
                removed["scope_limitations"],
            )

            conversation.scope_summary = clean_str(result["scope_summary"]) or conversation.scope_summary
            conversation.scope_confidence = result["confidence"]
            conversation.scope_missing_fields = result["missing_fields"]
            conversation.scope_last_user_message = payload.message
            conversation.scope_last_assistant_message = result["reply_to_user"]
            conversation.scope_assistant_suggested_answers = result["assistant_suggested_answers"]
            conversation.updated_by = uid_int

            previous_status = conversation.scope_status
            status = result["status"]
            buckets_filled = all(bool(getattr(conversation, k) or []) for k in _SCOPE_LIST_FIELDS)
            if status == "ask_followup" and buckets_filled:
                turn_count = (
                    db.query(func.count(PartnerConversationHistory.id))
                    .filter(
                        PartnerConversationHistory.conversation_id == conversation.id,
                        PartnerConversationHistory.part_no == _PART_NO,
                    )
                    .scalar()
                ) or 0
                if turn_count >= _PART4_MAX_TURNS_BEFORE_CONFIRM:
                    status = "confirm"

            status = enforce_confirm_before_complete(
                proposed_status=status,
                previous_status=previous_status,
                user_message=payload.message,
            )
            conversation.scope_status = status

            current_metadata = dict(conversation.metadata_json or {})
            current_metadata.update(payload.metadata or {})
            current_metadata["part4_prompt_version"] = payload.prompt_version
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
                confidence=conversation.confidence,
                missing_fields=conversation.missing_fields or [],
                status=conversation.status,

                solution_overview_summary=conversation.solution_overview_summary,
                solution_overview_matches=conversation.solution_overview_matches or [],
                solution_overview_capability_fits=conversation.solution_overview_capability_fits or [],
                solution_overview_recommended_focus=conversation.solution_overview_recommended_focus or [],
                solution_overview_confidence=conversation.solution_overview_confidence,
                solution_overview_missing_fields=conversation.solution_overview_missing_fields or [],
                solution_overview_status=conversation.solution_overview_status,

                objectives_summary=conversation.objectives_summary,
                objectives_high_level=conversation.objectives_high_level or [],
                objectives_intent=conversation.objectives_intent or [],
                objectives_success_criteria=conversation.objectives_success_criteria or [],
                objectives_constraints=conversation.objectives_constraints or [],
                objectives_confidence=conversation.objectives_confidence,
                objectives_missing_fields=conversation.objectives_missing_fields or [],
                objectives_status=conversation.objectives_status,

                scope_summary=conversation.scope_summary,
                scope_in_scope=conversation.scope_in_scope or [],
                scope_out_of_scope=conversation.scope_out_of_scope or [],
                scope_assumptions=conversation.scope_assumptions or [],
                scope_dependencies=conversation.scope_dependencies or [],
                scope_limitations=conversation.scope_limitations or [],
                scope_confidence=conversation.scope_confidence,
                scope_missing_fields=conversation.scope_missing_fields or [],
                scope_status=conversation.scope_status,

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
                    "scope_in_scope": conversation.scope_in_scope or [],
                    "scope_out_of_scope": conversation.scope_out_of_scope or [],
                    "scope_assumptions": conversation.scope_assumptions or [],
                    "scope_dependencies": conversation.scope_dependencies or [],
                    "scope_limitations": conversation.scope_limitations or [],
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
            logger.exception("[partner.chat.part4] execute failed")
            raise
        finally:
            db.close()


__all__ = [
    "RunPartnerScopeCommand",
    "RunPartnerScopePayload",
]
