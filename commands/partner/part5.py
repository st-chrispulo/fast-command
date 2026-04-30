"""partner/chat/part5 -- actors & roles.

Sub-conversation 5 of the partner planning flow. Identifies who is involved
and what their role is. It intentionally does NOT map actors to journey
steps, handoffs, or process stages; that belongs to a future part7.
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
    normalize_dict_list,
    normalize_list,
    normalize_metadata,
    normalize_suggested_answers,
    safe_float,
)
from models.partner.tbl_partner_conversation_histories import PartnerConversationHistory
from models.partner.tbl_partner_conversations import PartnerConversation


try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("partner.chat.part5")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


_PART_NO = 5
_PART5_MAX_TURNS_BEFORE_CONFIRM = 8
_ACTOR_ALLOWED_KEYS = ["actor", "role", "notes"]
_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}

_ACTORS_JSON_SCHEMA = {
    "name": "partner_actors_response",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "reply_to_user",
            "assistant_suggested_answers",
            "actors_summary",
            "actors",
            "removed_actors",
            "missing_fields",
            "confidence",
            "reason",
        ],
        "properties": {
            "status": {"type": "string", "enum": ["ask_followup", "confirm", "complete"]},
            "reply_to_user": {"type": "string"},
            "assistant_suggested_answers": _STRING_ARRAY,
            "actors_summary": {"type": "string"},
            "actors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["actor", "role", "notes"],
                    "properties": {
                        "actor": {"type": "string"},
                        "role": {"type": "string"},
                        "notes": {"type": ["string", "null"]},
                    },
                },
            },
            "removed_actors": _STRING_ARRAY,
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
    }


def _context_payload(conversation: PartnerConversation) -> Dict[str, Any]:
    return {
        "solution_overview_summary": conversation.solution_overview_summary,
        "solution_overview_recommended_focus": conversation.solution_overview_recommended_focus or [],
        "objectives_summary": conversation.objectives_summary,
        "objectives_high_level": conversation.objectives_high_level or [],
        "scope_summary": conversation.scope_summary,
        "scope_in_scope": conversation.scope_in_scope or [],
        "scope_out_of_scope": conversation.scope_out_of_scope or [],
    }


def _normalize_actor(item: Any) -> Optional[Dict[str, Optional[str]]]:
    if not isinstance(item, dict):
        return None
    actor = clean_str(item.get("actor"))
    role = clean_str(item.get("role"))
    notes = clean_str(item.get("notes"))
    if not actor or not role:
        return None
    return {
        "actor": actor,
        "role": role,
        "notes": notes,
    }


def _normalize_actor_list(v: Any) -> List[Dict[str, Optional[str]]]:
    if not isinstance(v, list):
        return []
    out: List[Dict[str, Optional[str]]] = []
    seen = set()
    for item in v:
        actor_row = _normalize_actor(item)
        if not actor_row:
            continue
        key = actor_row["actor"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(actor_row)
    return out


def _merge_and_remove_actors(
    current: Any,
    added: List[Dict[str, Optional[str]]],
    removed_actor_names: List[str],
) -> List[Dict[str, Optional[str]]]:
    existing = _normalize_actor_list(
        normalize_dict_list(current, allowed_keys=_ACTOR_ALLOWED_KEYS)
    )
    merged: List[Dict[str, Optional[str]]] = []
    index_by_actor: Dict[str, int] = {}

    for row in existing:
        key = row["actor"].lower()
        index_by_actor[key] = len(merged)
        merged.append(row)

    for row in added:
        key = row["actor"].lower()
        pos = index_by_actor.get(key)
        if pos is None:
            index_by_actor[key] = len(merged)
            merged.append(row)
        else:
            merged[pos] = row

    if not removed_actor_names:
        return merged

    removed_keys = {name.lower() for name in removed_actor_names if clean_str(name)}
    return [row for row in merged if row["actor"].lower() not in removed_keys]


def _prerequisites_met(conversation: PartnerConversation) -> bool:
    if not clean_str(conversation.partner_name):
        return False
    return bool(
        (conversation.partner_job_responsibilities or [])
        or (conversation.partner_pains or [])
        or (conversation.partner_wishes or [])
        or clean_str(conversation.scope_summary)
        or (conversation.scope_in_scope or [])
        or (conversation.objectives_high_level or [])
    )


class RunPartnerActorsPayload(BaseModel):
    conversation_key: str = Field(..., description="Conversation key from earlier partner parts")
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


class RunPartnerActorsCommand(BaseCommand):
    name = "partner/chat/part5"
    schema = RunPartnerActorsPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Partner"

    SYSTEM_PROMPT = """
You are Solitud's actors-and-roles interviewer.

Your job in this sub-conversation is to identify WHO is involved and WHAT
their role is. Focus only on actors and roles.

Capture:
- actors: JSON array of { actor, role, notes }
  - actor: person, team, department, or external party name.
  - role: what they are responsible for in this context.
  - notes: short optional note if useful.

Critical boundary:
- Do NOT describe where an actor appears in the business journey.
- Do NOT talk about stages, touchpoints, workflow steps, handoffs, or process
  sequencing.
- Do NOT turn this into a journey map. That is reserved for a future part7.

Tone:
- 1-3 sentence reply_to_user. Practical, not salesy.
- Ask ONE focused question per turn unless you are presenting the summary for
  confirmation.
- 3-5 short assistant_suggested_answers as quick-reply chips.
- Do NOT narrate process.
- Never mention internal labels like part1, part2, part3, etc.
- Do NOT say "move on to partX" or "proceed to partX".
- If you refer to what comes after this step, say only "the next step" unless
  the user explicitly asks for a name.

State machine:
- status = "ask_followup" while the actor list is empty or roles are too thin.
- status = "confirm" once you have a coherent first-pass actor/role list.
  Present a concise summary and ask explicitly: "Does this look right? Anything to add, change, or remove?"
- status = "complete" only after the user's next reply clearly approves the
  summary ("yes", "looks good", "confirm", etc.).
- Never set status = "complete" on the same turn as the first summary.

Removal rules:
- If the user asks to remove an actor, list the actor names in
  "removed_actors" using strings that exactly match the stored actor values.
- Do NOT include the same actor in both "actors" and "removed_actors".

missing_fields should be a short list of what's still thin (for example
"actors" or "roles").

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
                "content": "Current context from later parts:\n"
                + json.dumps(_context_payload(conversation), ensure_ascii=False),
            },
        ]

        prior_state = {
            "actors_status": conversation.actors_status,
            "actors_summary": conversation.actors_summary,
            "actors": conversation.actors or [],
            "actors_missing_fields": conversation.actors_missing_fields or [],
            "actors_confidence": (
                float(conversation.actors_confidence)
                if conversation.actors_confidence is not None
                else None
            ),
        }
        prior_state = {k: v for k, v in prior_state.items() if v not in (None, [], "")}
        if prior_state:
            messages.append(
                {
                    "role": "system",
                    "content": "Current actors state:\n" + json.dumps(prior_state, ensure_ascii=False),
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
                        "Produce the first-pass actors and roles now. Identify who is "
                        "involved and what each person/team's role is."
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
                "I had trouble with that response. Who are the main people or teams "
                "involved here, and what role does each one play?"
            ),
            "assistant_suggested_answers": [
                "Ops team runs the workflow",
                "Managers approve exceptions",
                "Customers submit requests",
            ],
            "actors_summary": "",
            "actors": [],
            "removed_actors": [],
            "missing_fields": ["actors"],
            "confidence": 0.0,
            "reason": "Model did not return valid JSON.",
        }

    def _normalize_result(self, parsed: dict) -> dict:
        actors = _normalize_actor_list(parsed.get("actors"))
        removed_actors = normalize_list(parsed.get("removed_actors"))
        actor_names_lower = {row["actor"].lower() for row in actors}
        removed_actors = [name for name in removed_actors if name.lower() not in actor_names_lower]

        return {
            "status": clean_str(parsed.get("status")) or "ask_followup",
            "reply_to_user": clean_str(parsed.get("reply_to_user"))
            or "Who are the main people or teams involved here, and what role does each one play?",
            "assistant_suggested_answers": normalize_suggested_answers(parsed.get("assistant_suggested_answers")),
            "actors_summary": clean_str(parsed.get("actors_summary")),
            "actors": actors,
            "removed_actors": removed_actors,
            "missing_fields": normalize_list(parsed.get("missing_fields")),
            "confidence": safe_float(parsed.get("confidence")),
            "reason": clean_str(parsed.get("reason")),
        }

    async def execute(self, payload: RunPartnerActorsPayload, user_id: Optional[str] = None) -> dict:
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        uid_int = as_int_user_id(user_id)
        db = SessionLocal()

        try:
            logger.info(
                "[partner.chat.part5] start conversation_key=%s user_id=%s",
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
                    detail="Earlier discovery is too thin for actors & roles.",
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
                response_format={"type": "json_schema", "json_schema": _ACTORS_JSON_SCHEMA},
            )

            raw_text = completion.choices[0].message.content or ""
            parsed = self._parse_model_output(raw_text)
            result = self._normalize_result(parsed)

            new_actor_rows = result["actors"] or _normalize_actor_list(
                normalize_dict_list(conversation.actors, allowed_keys=_ACTOR_ALLOWED_KEYS)
            )
            merged_actors = _merge_and_remove_actors(
                conversation.actors,
                new_actor_rows,
                result["removed_actors"],
            )

            conversation.actors = merged_actors
            conversation.actors_summary = clean_str(result["actors_summary"]) or conversation.actors_summary
            conversation.actors_confidence = result["confidence"]
            conversation.actors_missing_fields = result["missing_fields"]
            conversation.actors_last_user_message = payload.message
            conversation.actors_last_assistant_message = result["reply_to_user"]
            conversation.actors_assistant_suggested_answers = result["assistant_suggested_answers"]
            conversation.updated_by = uid_int

            previous_status = conversation.actors_status
            status = result["status"]
            if status == "ask_followup" and merged_actors:
                turn_count = (
                    db.query(func.count(PartnerConversationHistory.id))
                    .filter(
                        PartnerConversationHistory.conversation_id == conversation.id,
                        PartnerConversationHistory.part_no == _PART_NO,
                    )
                    .scalar()
                ) or 0
                if turn_count >= _PART5_MAX_TURNS_BEFORE_CONFIRM:
                    status = "confirm"

            status = enforce_confirm_before_complete(
                proposed_status=status,
                previous_status=previous_status,
                user_message=payload.message,
            )
            conversation.actors_status = status

            current_metadata = dict(conversation.metadata_json or {})
            current_metadata.update(payload.metadata or {})
            current_metadata["part5_prompt_version"] = payload.prompt_version
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

                actors_summary=conversation.actors_summary,
                actors=conversation.actors or [],
                actors_confidence=conversation.actors_confidence,
                actors_missing_fields=conversation.actors_missing_fields or [],
                actors_status=conversation.actors_status,

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
                    "actors": conversation.actors or [],
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
            logger.exception("[partner.chat.part5] execute failed")
            raise
        finally:
            db.close()


__all__ = [
    "RunPartnerActorsCommand",
    "RunPartnerActorsPayload",
]
