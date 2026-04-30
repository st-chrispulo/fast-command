"""partner/chat/part2 -- solution overview.

Sub-conversation 2 of the partner planning flow. Reads the partner profile
(part1) and assesses whether each captured pain / wish / responsibility maps
to a Solitud capability. The catalog of capabilities and limitations is a
high-level description (not source code) so the model reasons at the right
level of abstraction and we don't leak implementation IP into prompts.

Output structure (per match):
    { source_field, source_item, fit, capability_families, rationale, caveats }

Aggregates also written:
    solution_overview_capability_fits   -> dedup'd capability families with any positive fit
    solution_overview_recommended_focus -> top items the team should pursue
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
    merge_list,
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

    logger = _app_logger.getChild("partner.chat.part2")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


_PART_NO = 2


# ---------------------------------------------------------------------------
# Solitud capability catalog (high-level description for the LLM)
# ---------------------------------------------------------------------------
#
# This is the *only* description of Solitud's technical capabilities the LLM
# sees. Keep it conceptual - never include source code, library versions, or
# internal architecture detail. The LLM uses this to reason about whether a
# pain / wish / responsibility is something Solitud can deliver.
#
# Edit this catalog (and bump _CATALOG_VERSION) when capabilities change.

_CATALOG_VERSION = "v1"

SOLITUD_CAPABILITY_CATALOG = """
Solitud builds digital workflows that turn messy, manual, people-dependent
operations into structured, observable, repeatable systems. The following
families describe what Solitud can deliver. Use these as the labels in
"capability_families" for each match.

CAPABILITIES (what Solitud CAN do):

1. web_app
   Custom web applications - dashboards, internal tools, partner portals,
   admin consoles. Multi-user, role-based, browser-based.

2. input_streams
   Background apps and cron jobs, webhooks, API sync/scrapers into an app
   database, SQS-style queue polling, and simple ETL pipelines (non
   big-data scale). Used to pull data in from external systems on a
   schedule or in response to events.

3. custom_logic
   Custom server-side logic and workflow orchestration. Third-party libraries
   may be used with approval. Suitable for business rules, validation,
   approvals, scoring, and process automation.

4. ai_capabilities
   AI features delivered through third-party providers (OpenAI, Claude).
   Use cases include text generation, summarization, classification,
   extraction, retrieval-augmented Q&A, and conversational interfaces.

5. mid_fi_uiux
   Mid-fidelity UI/UX design and implementation. Clean, functional
   interfaces. Not a creative-agency-level brand polish, but professional
   and usable.

6. data_visualization
   Charts, tables, dashboards, and reporting views over data already in the
   app database or pulled in via input_streams.

LIMITATIONS (what Solitud does NOT do - mark these "not_fit"):

- windows_app
   Native Windows desktop applications.

- mobile_app
   Native iOS / Android mobile applications. (Responsive web is fine and
   falls under web_app.)

- big_data_etl
   Big-data ETL pipelines (Spark, distributed processing, petabyte-scale
   warehousing). Simple ETL within a single app database is fine and falls
   under input_streams.
""".strip()


# ---------------------------------------------------------------------------
# JSON schema enforced on the LLM response
# ---------------------------------------------------------------------------

_FIT_VALUES = ["strong", "partial", "tentative", "not_fit"]
_SOURCE_FIELD_VALUES = [
    "partner_pains",
    "partner_wishes",
    "partner_job_responsibilities",
]
_CAPABILITY_FAMILY_VALUES = [
    "web_app",
    "input_streams",
    "custom_logic",
    "ai_capabilities",
    "mid_fi_uiux",
    "data_visualization",
    "windows_app",
    "mobile_app",
    "big_data_etl",
]

_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}

_SOLUTION_OVERVIEW_JSON_SCHEMA = {
    "name": "partner_solution_overview_response",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "reply_to_user",
            "assistant_suggested_answers",
            "solution_overview_summary",
            "matches",
            "recommended_focus",
            "missing_fields",
            "confidence",
            "reason",
        ],
        "properties": {
            "status": {"type": "string", "enum": ["ask_followup", "confirm", "complete"]},
            "reply_to_user": {"type": "string"},
            "assistant_suggested_answers": _STRING_ARRAY,
            "solution_overview_summary": {"type": "string"},
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "source_field",
                        "source_item",
                        "fit",
                        "capability_families",
                        "rationale",
                        "caveats",
                    ],
                    "properties": {
                        "source_field": {"type": "string", "enum": _SOURCE_FIELD_VALUES},
                        "source_item": {"type": "string"},
                        "fit": {"type": "string", "enum": _FIT_VALUES},
                        "capability_families": {
                            "type": "array",
                            "items": {"type": "string", "enum": _CAPABILITY_FAMILY_VALUES},
                        },
                        "rationale": {"type": "string"},
                        "caveats": _STRING_ARRAY,
                    },
                },
            },
            "recommended_focus": _STRING_ARRAY,
            "missing_fields": _STRING_ARRAY,
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
    },
}


_PART2_MAX_TURNS_BEFORE_CONFIRM = 8


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _profile_payload_from_conversation(conversation: PartnerConversation) -> Dict[str, Any]:
    """Distill the part1 profile down to just what part2 needs to reason."""
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


def _profile_has_minimum(conversation: PartnerConversation) -> bool:
    """True when the part1 profile has enough content to do a part2 pass."""
    if not clean_str(conversation.partner_name):
        return False
    pains = conversation.partner_pains or []
    wishes = conversation.partner_wishes or []
    responsibilities = conversation.partner_job_responsibilities or []
    return bool(pains or wishes or responsibilities)


def _normalize_match(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    source_field = clean_str(item.get("source_field"))
    if source_field not in _SOURCE_FIELD_VALUES:
        return None
    source_item = clean_str(item.get("source_item"))
    if not source_item:
        return None
    fit = clean_str(item.get("fit"))
    if fit not in _FIT_VALUES:
        fit = "tentative"
    families = [
        f for f in normalize_list(item.get("capability_families")) if f in _CAPABILITY_FAMILY_VALUES
    ]
    return {
        "source_field": source_field,
        "source_item": source_item,
        "fit": fit,
        "capability_families": families,
        "rationale": clean_str(item.get("rationale")) or "",
        "caveats": normalize_list(item.get("caveats")),
    }


def _aggregate_capability_fits(matches: List[Dict[str, Any]]) -> List[str]:
    seen: List[str] = []
    seen_lower = set()
    for m in matches:
        if m.get("fit") == "not_fit":
            continue
        for fam in m.get("capability_families") or []:
            key = fam.lower()
            if key in seen_lower:
                continue
            seen_lower.add(key)
            seen.append(fam)
    return seen


# ---------------------------------------------------------------------------
# payload + command
# ---------------------------------------------------------------------------


class RunPartnerSolutionOverviewPayload(BaseModel):
    conversation_key: str = Field(..., description="Conversation key from part1")
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


class RunPartnerSolutionOverviewCommand(BaseCommand):
    name = "partner/chat/part2"
    schema = RunPartnerSolutionOverviewPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Partner"

    SYSTEM_PROMPT = """
You are Solitud's solution-fit analyst.

Your job in this sub-conversation is to take a partner's captured profile -
their responsibilities, pains, and wishes - and assess whether each item is
something Solitud can deliver. You reason against the high-level capability
catalog provided in a separate system message; do NOT invent capabilities.

You are NOT scoping or estimating. You are NOT designing implementation. You
are deciding fit and naming the relevant capability families per item.

For every relevant pain / wish / responsibility produce one match entry:
- source_field: one of "partner_pains", "partner_wishes", "partner_job_responsibilities".
- source_item: the exact string from the partner's profile (verbatim).
- fit: one of "strong" | "partial" | "tentative" | "not_fit".
    strong    -> Solitud can clearly deliver this with the listed families.
    partial   -> Solitud can address part of it; meaningful pieces remain.
    tentative -> Plausible, but more discovery needed.
    not_fit   -> Outside Solitud's scope (Windows app, mobile app, big data ETL,
                 or simply unrelated to digital workflows).
- capability_families: which catalog families apply (use the snake_case keys).
- rationale: 1-2 sentences explaining the fit decision.
- caveats: short list of risks/dependencies/assumptions, or [].

After the matches, produce:
- recommended_focus: 2-5 items (typically the strongest fits) the team should
  pursue first. Use the original source_item strings.
- solution_overview_summary: 2-4 sentence narrative of what Solitud can and
  cannot do for this partner, based on the per-match fit decisions.
- missing_fields: list any high-level info you'd want before designing
  (e.g. "scale of usage", "primary user role"). Keep this short.

State machine:
- status = "ask_followup" if you need clarification on specific pains/wishes
  before you can confidently classify them.
- status = "confirm" once you've produced a coherent first-pass overview and
  want the user to validate or correct. Present a concise summary and ask
  explicitly: "Does this look right? Anything to add, change, or remove?"
- status = "complete" only after the user's next reply clearly approves the
  overview ("yes", "looks good", "confirm", etc.).
- Never set status = "complete" on the same turn as the first summary.

Tone:
- 1-3 sentence reply_to_user. Plain, practical, not salesy.
- 3-5 short assistant_suggested_answers as quick-reply chips for the user.
- Do NOT narrate process ("I will now analyze..."). Just answer.
- Never mention internal labels like part1, part2, part3, etc.
- Do NOT say "move on to partX" or "proceed to partX".
- If this step is confirmed and you refer to what comes after it, say only
  "the next step".
- Only if the user explicitly asks what the next step is, answer with the
  human step name "Objectives". Never use an internal part number.

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
                "content": "Solitud capability catalog:\n" + SOLITUD_CAPABILITY_CATALOG,
            },
            {
                "role": "system",
                "content": "Partner profile:\n"
                + json.dumps(_profile_payload_from_conversation(conversation), ensure_ascii=False),
            },
        ]

        # Surface any prior solution-overview state so iterative messages refine instead of restart.
        prior_state = {
            "solution_overview_status": conversation.solution_overview_status,
            "solution_overview_summary": conversation.solution_overview_summary,
            "solution_overview_matches": conversation.solution_overview_matches or [],
            "solution_overview_capability_fits": conversation.solution_overview_capability_fits or [],
            "solution_overview_recommended_focus": conversation.solution_overview_recommended_focus or [],
            "solution_overview_missing_fields": conversation.solution_overview_missing_fields or [],
            "solution_overview_confidence": (
                float(conversation.solution_overview_confidence)
                if conversation.solution_overview_confidence is not None
                else None
            ),
        }
        prior_state = {k: v for k, v in prior_state.items() if v not in (None, [], "")}
        if prior_state:
            messages.append(
                {
                    "role": "system",
                    "content": "Current solution overview state:\n" + json.dumps(prior_state, ensure_ascii=False),
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
                        "Produce the first-pass solution overview now. Walk through every "
                        "captured pain, wish, and responsibility and return the matches, "
                        "recommended focus, and the summary."
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
            "reply_to_user": "I had trouble parsing that response. Want me to retry the overview?",
            "assistant_suggested_answers": ["Retry the overview", "Show the summary again"],
            "solution_overview_summary": "",
            "matches": [],
            "recommended_focus": [],
            "missing_fields": [],
            "confidence": 0.0,
            "reason": "Model did not return valid JSON.",
        }

    def _normalize_result(self, parsed: dict) -> dict:
        raw_matches = parsed.get("matches")
        cleaned_matches: List[Dict[str, Any]] = []
        if isinstance(raw_matches, list):
            for item in raw_matches:
                m = _normalize_match(item)
                if m:
                    cleaned_matches.append(m)

        return {
            "status": clean_str(parsed.get("status")) or "ask_followup",
            "reply_to_user": clean_str(parsed.get("reply_to_user"))
            or "Here's a first pass at how Solitud maps to what you described.",
            "assistant_suggested_answers": normalize_suggested_answers(parsed.get("assistant_suggested_answers")),
            "solution_overview_summary": clean_str(parsed.get("solution_overview_summary")),
            "matches": cleaned_matches,
            "recommended_focus": normalize_list(parsed.get("recommended_focus")),
            "missing_fields": normalize_list(parsed.get("missing_fields")),
            "confidence": safe_float(parsed.get("confidence")),
            "reason": clean_str(parsed.get("reason")),
        }

    async def execute(self, payload: RunPartnerSolutionOverviewPayload, user_id: Optional[str] = None) -> dict:
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        uid_int = as_int_user_id(user_id)
        db = SessionLocal()

        try:
            logger.info(
                "[partner.chat.part2] start conversation_key=%s user_id=%s",
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
                    detail="Partner profile is too thin for solution overview. Capture pains/wishes/responsibilities in part1 first.",
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
                response_format={"type": "json_schema", "json_schema": _SOLUTION_OVERVIEW_JSON_SCHEMA},
            )

            raw_text = completion.choices[0].message.content or ""
            parsed = self._parse_model_output(raw_text)
            result = self._normalize_result(parsed)

            # Aggregates derived from matches.
            capability_fits = _aggregate_capability_fits(result["matches"])

            # Merge new aggregates with prior state so iterative refinements
            # don't accidentally erase earlier observations.
            merged_capability_fits = merge_list(
                conversation.solution_overview_capability_fits or [],
                capability_fits,
            )
            merged_recommended_focus = merge_list(
                conversation.solution_overview_recommended_focus or [],
                result["recommended_focus"],
            )

            # The matches list is fully replaced each turn (it's a derived view),
            # but we keep prior matches when the model returns nothing.
            new_matches = result["matches"] or normalize_dict_list(
                conversation.solution_overview_matches,
                allowed_keys=[
                    "source_field",
                    "source_item",
                    "fit",
                    "capability_families",
                    "rationale",
                    "caveats",
                ],
            )

            previous_status = conversation.solution_overview_status
            status = result["status"]
            # Mirror part1's safety net: cap iteration at a few turns.
            turn_count = (
                db.query(func.count(PartnerConversationHistory.id))
                .filter(
                    PartnerConversationHistory.conversation_id == conversation.id,
                    PartnerConversationHistory.part_no == _PART_NO,
                )
                .scalar()
            ) or 0
            if status == "ask_followup" and turn_count >= _PART2_MAX_TURNS_BEFORE_CONFIRM and new_matches:
                status = "confirm"

            status = enforce_confirm_before_complete(
                proposed_status=status,
                previous_status=previous_status,
                user_message=payload.message,
            )

            conversation.solution_overview_status = status
            conversation.solution_overview_summary = (
                clean_str(result["solution_overview_summary"]) or conversation.solution_overview_summary
            )
            conversation.solution_overview_matches = new_matches
            conversation.solution_overview_capability_fits = merged_capability_fits
            conversation.solution_overview_recommended_focus = merged_recommended_focus
            conversation.solution_overview_missing_fields = result["missing_fields"]
            conversation.solution_overview_confidence = result["confidence"]
            conversation.solution_overview_last_user_message = payload.message
            conversation.solution_overview_last_assistant_message = result["reply_to_user"]
            conversation.solution_overview_assistant_suggested_answers = result["assistant_suggested_answers"]
            conversation.updated_by = uid_int

            current_metadata = dict(conversation.metadata_json or {})
            current_metadata.update(payload.metadata or {})
            current_metadata["part2_prompt_version"] = payload.prompt_version
            current_metadata["part2_catalog_version"] = _CATALOG_VERSION
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

                # Carry the part1 profile snapshot so each row is self-contained.
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

                # part2 snapshot
                solution_overview_summary=conversation.solution_overview_summary,
                solution_overview_matches=conversation.solution_overview_matches or [],
                solution_overview_capability_fits=conversation.solution_overview_capability_fits or [],
                solution_overview_recommended_focus=conversation.solution_overview_recommended_focus or [],
                solution_overview_confidence=conversation.solution_overview_confidence,
                solution_overview_missing_fields=conversation.solution_overview_missing_fields or [],
                solution_overview_status=conversation.solution_overview_status,

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
                    "matches": new_matches,
                    "recommended_focus": merged_recommended_focus,
                    "capability_fits": merged_capability_fits,
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
            logger.exception("[partner.chat.part2] execute failed")
            raise
        finally:
            db.close()


__all__ = [
    "RunPartnerSolutionOverviewCommand",
    "RunPartnerSolutionOverviewPayload",
    "SOLITUD_CAPABILITY_CATALOG",
]
