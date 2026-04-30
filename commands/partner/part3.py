"""partner/chat/part3 -- objectives.

Sub-conversation 3 of the partner planning flow. Reads the partner profile
and the solution overview, then helps the partner articulate
the objectives behind pursuing Solitud:

  - objectives_high_level    -> what they want to achieve (short keyword/sentence)
  - objectives_intent        -> the *why* behind each objective
  - objectives_success_criteria -> how they'll know it worked
  - objectives_constraints   -> compliance / policy / regulatory constraints

A small template catalog (``OBJECTIVES_TEMPLATE``) seeds the LLM with the
canonical patterns we see across partners. Edit the template in this file
manually to tune behaviour - it is part of the prompt, not user data.
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

    logger = _app_logger.getChild("partner.chat.part3")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


_PART_NO = 3


# ---------------------------------------------------------------------------
# Editable objectives template catalog.
# ---------------------------------------------------------------------------
#
# This catalog is *prompt content*, not data. It seeds the LLM with the kinds
# of objectives, intents, success criteria, and constraints we typically see
# across partners. Edit freely to tune the model's behaviour. Bump the version
# string when you change it so we can correlate captures back to the catalog
# they were produced under.

_TEMPLATE_VERSION = "v1"

OBJECTIVES_TEMPLATE = """
Common objective patterns Solitud sees with partners. Use these as priors -
do NOT force-fit. If the partner's actual situation doesn't match, follow
the partner.

HIGH-LEVEL OBJECTIVES (typical patterns):
- Reduce manual back-and-forth in a recurring workflow.
- Centralize information that's currently scattered across email/spreadsheets/chat.
- Give a specific role (ops manager, partner, customer) clear visibility into status.
- Automate handoffs so work moves without anyone chasing it.
- Make a repeated decision faster or more consistent.
- Replace a brittle manual process before it scales further.
- Stand up a partner/customer portal to professionalize the experience.

INTENT (typical reasons behind those objectives):
- Cost: free up headcount currently doing manual coordination.
- Speed: cut cycle time for a request / approval / onboarding.
- Quality: reduce errors and missed steps in a critical process.
- Visibility: surface what's stuck, who's blocked, and why.
- Scale: handle 2-5x volume without hiring proportionally.
- Compliance: produce audit trails and consistent enforcement.
- Customer experience: make the partner/customer feel taken care of.

SUCCESS CRITERIA (how partners typically know it worked):
- Cycle time on the target workflow drops by a stated percentage.
- A specific role no longer needs to do a specific manual task.
- Status is visible in one place rather than chased across channels.
- Error/escalation rate falls below a target threshold.
- Onboarding time per new partner/customer drops.
- A backlog clears within a defined timeframe.

CONSTRAINTS (typical limits to respect):
- Compliance / audit / data residency requirements.
- Regulatory obligations that cannot be relaxed.
- Internal policy or approval requirements that must be respected.
""".strip()


_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}

_OBJECTIVES_JSON_SCHEMA = {
    "name": "partner_objectives_response",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "reply_to_user",
            "assistant_suggested_answers",
            "objectives_summary",
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
            "objectives_summary": {"type": "string"},
            "extracted": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "objectives_high_level",
                    "objectives_intent",
                    "objectives_success_criteria",
                    "objectives_constraints",
                ],
                "properties": {
                    "objectives_high_level": _STRING_ARRAY,
                    "objectives_intent": _STRING_ARRAY,
                    "objectives_success_criteria": _STRING_ARRAY,
                    "objectives_constraints": _STRING_ARRAY,
                },
            },
            "removed": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "objectives_high_level",
                    "objectives_intent",
                    "objectives_success_criteria",
                    "objectives_constraints",
                ],
                "properties": {
                    "objectives_high_level": _STRING_ARRAY,
                    "objectives_intent": _STRING_ARRAY,
                    "objectives_success_criteria": _STRING_ARRAY,
                    "objectives_constraints": _STRING_ARRAY,
                },
            },
            "missing_fields": _STRING_ARRAY,
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
    },
}


_OBJECTIVES_LIST_FIELDS = (
    "objectives_high_level",
    "objectives_intent",
    "objectives_success_criteria",
    "objectives_constraints",
)


_PART3_MAX_TURNS_BEFORE_CONFIRM = 8


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _profile_payload_from_conversation(conversation: PartnerConversation) -> Dict[str, Any]:
    return {
        "partner_name": conversation.partner_name,
        "partner_industry": conversation.partner_industry,
        "partner_pains": conversation.partner_pains or [],
        "partner_wishes": conversation.partner_wishes or [],
        "partner_job_responsibilities": conversation.partner_job_responsibilities or [],
    }


def _solution_overview_payload(conversation: PartnerConversation) -> Dict[str, Any]:
    return {
        "solution_overview_summary": conversation.solution_overview_summary,
        "solution_overview_recommended_focus": conversation.solution_overview_recommended_focus or [],
        "solution_overview_capability_fits": conversation.solution_overview_capability_fits or [],
    }


def _profile_has_minimum(conversation: PartnerConversation) -> bool:
    if not clean_str(conversation.partner_name):
        return False
    return bool(
        (conversation.partner_pains or [])
        or (conversation.partner_wishes or [])
        or (conversation.partner_job_responsibilities or [])
    )


def _normalize_removed_block(v: Any) -> Dict[str, List[str]]:
    if not isinstance(v, dict):
        return {k: [] for k in _OBJECTIVES_LIST_FIELDS}
    return {k: normalize_list(v.get(k)) for k in _OBJECTIVES_LIST_FIELDS}


# ---------------------------------------------------------------------------
# payload + command
# ---------------------------------------------------------------------------


class RunPartnerObjectivesPayload(BaseModel):
    conversation_key: str = Field(..., description="Conversation key from part1/part2")
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


class RunPartnerObjectivesCommand(BaseCommand):
    name = "partner/chat/part3"
    schema = RunPartnerObjectivesPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Partner"

    SYSTEM_PROMPT = """
You are Solitud's objectives interviewer.

Your job in this sub-conversation is to articulate WHY this partner is
pursuing a Solitud engagement and what success looks like - given their
captured profile and the solution overview. You are NOT
re-doing fit analysis or capturing more pains; you are turning the picture
into objectives.

Capture four buckets:
- objectives_high_level     : short keyword or sentence per objective.
- objectives_intent         : the WHY behind each objective (business rationale).
                              Pair by index where reasonable.
- objectives_success_criteria: how the partner will know it worked
                              (short outcome statements).
- objectives_constraints    : compliance, regulatory, policy, or governance
                              constraints that shape success.

Do NOT ask about budget or timeline in this stage.
Do NOT ask about scope limits in this stage. Scope boundaries belong to the next step, not this one.

Use the objectives template (provided in a separate system message) as priors
for what to listen for - but follow the partner; do not invent.

Tone:
- 1-3 sentence reply_to_user. Practical, not salesy.
- Ask ONE focused question per turn.
- 3-5 short assistant_suggested_answers as quick-reply chips.
- Do NOT narrate process ("I will now capture..."). Just ask.
- Never mention internal labels like part1, part2, part3, etc.
- Do NOT say "move on to partX" or "proceed to partX".
- If this step is confirmed and you refer to what comes after it, say only
  "the next step".
- Only if the user explicitly asks what the next step is, answer with the
  human step name "Scope & Limitations". Never use an internal part number.

State machine:
- status = "ask_followup" while any of the four buckets is empty or thin.
- status = "confirm" once each bucket has at least one solid entry and you
  want the user to validate or correct. Present a concise summary and ask
  explicitly: "Does this look right? Anything to add, change, or remove?"
- status = "complete" only after the user's next reply clearly approves the
  summary ("yes", "looks good", "confirm", etc.).
- Never set status = "complete" on the same turn as the first summary.

Removal rules:
- If the user asks to drop an objective/intent/criteria/constraint, list it
  in the "removed" block under the matching field.
- "removed" entries must match the strings currently stored in state.
- Do NOT include the same value in both "extracted" and "removed".

missing_fields lists which of the four buckets is still empty enough to
need work (use the field names above).

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
            {"role": "system", "content": "Objectives template:\n" + OBJECTIVES_TEMPLATE},
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
        ]

        prior_state = {
            "objectives_status": conversation.objectives_status,
            "objectives_summary": conversation.objectives_summary,
            "objectives_high_level": conversation.objectives_high_level or [],
            "objectives_intent": conversation.objectives_intent or [],
            "objectives_success_criteria": conversation.objectives_success_criteria or [],
            "objectives_constraints": conversation.objectives_constraints or [],
            "objectives_missing_fields": conversation.objectives_missing_fields or [],
            "objectives_confidence": (
                float(conversation.objectives_confidence)
                if conversation.objectives_confidence is not None
                else None
            ),
        }
        prior_state = {k: v for k, v in prior_state.items() if v not in (None, [], "")}
        if prior_state:
            messages.append(
                {
                    "role": "system",
                    "content": "Current objectives state:\n" + json.dumps(prior_state, ensure_ascii=False),
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
                        "Kick off the objectives conversation. Ask the partner what they "
                        "actually want to achieve and why - one focused question."
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
                "I had trouble with that response. What's the main outcome you'd want "
                "from doing this with Solitud?"
            ),
            "assistant_suggested_answers": [
                "Cut manual coordination time",
                "Get one source of truth for status",
                "Onboard partners faster",
            ],
            "objectives_summary": "",
            "extracted": {k: [] for k in _OBJECTIVES_LIST_FIELDS},
            "removed": {k: [] for k in _OBJECTIVES_LIST_FIELDS},
            "missing_fields": list(_OBJECTIVES_LIST_FIELDS),
            "confidence": 0.0,
            "reason": "Model did not return valid JSON.",
        }

    def _normalize_result(self, parsed: dict) -> dict:
        extracted_raw = parsed.get("extracted") or {}
        removed_raw = _normalize_removed_block(parsed.get("removed"))

        extracted = {k: normalize_list(extracted_raw.get(k)) for k in _OBJECTIVES_LIST_FIELDS}

        # Guard against the model placing the same value in both blocks.
        for k in _OBJECTIVES_LIST_FIELDS:
            added_lower = {s.lower() for s in extracted[k]}
            removed_raw[k] = [x for x in removed_raw[k] if x.lower() not in added_lower]

        return {
            "status": clean_str(parsed.get("status")) or "ask_followup",
            "reply_to_user": clean_str(parsed.get("reply_to_user"))
            or "What's the main outcome you'd want from doing this with Solitud?",
            "assistant_suggested_answers": normalize_suggested_answers(parsed.get("assistant_suggested_answers")),
            "objectives_summary": clean_str(parsed.get("objectives_summary")),
            "extracted": extracted,
            "removed": removed_raw,
            "missing_fields": normalize_list(parsed.get("missing_fields")),
            "confidence": safe_float(parsed.get("confidence")),
            "reason": clean_str(parsed.get("reason")),
        }

    async def execute(self, payload: RunPartnerObjectivesPayload, user_id: Optional[str] = None) -> dict:
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        uid_int = as_int_user_id(user_id)
        db = SessionLocal()

        try:
            logger.info(
                "[partner.chat.part3] start conversation_key=%s user_id=%s",
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

            if not _profile_has_minimum(conversation):
                raise HTTPException(
                    status_code=409,
                    detail="Partner profile is too thin for objectives. Capture pains/wishes/responsibilities in part1 first.",
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
                response_format={"type": "json_schema", "json_schema": _OBJECTIVES_JSON_SCHEMA},
            )

            raw_text = completion.choices[0].message.content or ""
            parsed = self._parse_model_output(raw_text)
            result = self._normalize_result(parsed)
            extracted = result["extracted"]
            removed = result["removed"]

            conversation.objectives_high_level = merge_and_remove(
                conversation.objectives_high_level,
                extracted["objectives_high_level"],
                removed["objectives_high_level"],
            )
            conversation.objectives_intent = merge_and_remove(
                conversation.objectives_intent,
                extracted["objectives_intent"],
                removed["objectives_intent"],
            )
            conversation.objectives_success_criteria = merge_and_remove(
                conversation.objectives_success_criteria,
                extracted["objectives_success_criteria"],
                removed["objectives_success_criteria"],
            )
            conversation.objectives_constraints = merge_and_remove(
                conversation.objectives_constraints,
                extracted["objectives_constraints"],
                removed["objectives_constraints"],
            )

            conversation.objectives_summary = (
                clean_str(result["objectives_summary"]) or conversation.objectives_summary
            )
            conversation.objectives_confidence = result["confidence"]
            conversation.objectives_missing_fields = result["missing_fields"]
            conversation.objectives_last_user_message = payload.message
            conversation.objectives_last_assistant_message = result["reply_to_user"]
            conversation.objectives_assistant_suggested_answers = result["assistant_suggested_answers"]
            conversation.updated_by = uid_int

            previous_status = conversation.objectives_status
            status = result["status"]
            # Safety net: cap iteration once each bucket has content.
            buckets_filled = all(
                bool(getattr(conversation, k) or [])
                for k in _OBJECTIVES_LIST_FIELDS
            )
            if status == "ask_followup" and buckets_filled:
                turn_count = (
                    db.query(func.count(PartnerConversationHistory.id))
                    .filter(
                        PartnerConversationHistory.conversation_id == conversation.id,
                        PartnerConversationHistory.part_no == _PART_NO,
                    )
                    .scalar()
                    ) or 0
                if turn_count >= _PART3_MAX_TURNS_BEFORE_CONFIRM:
                    status = "confirm"

            status = enforce_confirm_before_complete(
                proposed_status=status,
                previous_status=previous_status,
                user_message=payload.message,
            )
            conversation.objectives_status = status

            current_metadata = dict(conversation.metadata_json or {})
            current_metadata.update(payload.metadata or {})
            current_metadata["part3_prompt_version"] = payload.prompt_version
            current_metadata["part3_template_version"] = _TEMPLATE_VERSION
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

                # part1 + part2 snapshot for self-contained history rows
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

                # part3 snapshot
                objectives_summary=conversation.objectives_summary,
                objectives_high_level=conversation.objectives_high_level or [],
                objectives_intent=conversation.objectives_intent or [],
                objectives_success_criteria=conversation.objectives_success_criteria or [],
                objectives_constraints=conversation.objectives_constraints or [],
                objectives_confidence=conversation.objectives_confidence,
                objectives_missing_fields=conversation.objectives_missing_fields or [],
                objectives_status=conversation.objectives_status,

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
                    "objectives_high_level": conversation.objectives_high_level or [],
                    "objectives_intent": conversation.objectives_intent or [],
                    "objectives_success_criteria": conversation.objectives_success_criteria or [],
                    "objectives_constraints": conversation.objectives_constraints or [],
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
            logger.exception("[partner.chat.part3] execute failed")
            raise
        finally:
            db.close()


__all__ = [
    "RunPartnerObjectivesCommand",
    "RunPartnerObjectivesPayload",
    "OBJECTIVES_TEMPLATE",
]
