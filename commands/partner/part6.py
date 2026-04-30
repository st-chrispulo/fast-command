"""partner/chat/part6 -- entity model.

Sub-conversation 6 of the partner planning flow. Turns the prior parts'
outputs (customer profile, solution overview, objectives, scope & limitations,
actors & roles) -- plus any user-attached artifacts (Excel/CSV/Word/PDF/
screenshots/messages) -- into a flat, machine-readable entity model that
downstream FE/BE generation parts will consume.

Output shape (per entity, persisted as a JSONB row inside `entity_model`):

    {
      "id": "dog.medical_history",        # hierarchical, globally addressable
      "name": "MedicalHistory",
      "aliases": ["Vet Visit"],
      "description": "...",
      "status": "draft" | "confirmed" | "deprecated",
      "sensitivity": ["pii", "confidential"],
      "volume_estimate": "~1M",
      "relationships": [
        { "kind": "parent",    "to": ["dog.uuid"], "cardinality": "1:N", ... },
        { "kind": "reference", "to": ["vet.uuid"], "cardinality": "N:1", ... }
      ],
      "constraints":   [{ "kind": "unique", "on": [...], ... }],
      "attachments":   [{ "kind": "sample_data", "path": "...", ... }],
      "open_questions": [...],
      "attributes": [
        {
          "id": "dog.medical_history.temperature",
          "type": "decimal(4,1)",
          "role": "attribute",
          "nullable": false,
          "format": null,
          "unit": "celsius",
          "description": "...",
          "aliases": [],
          "sensitivity": [],
          "constraints": { "min": "30", "max": "45", "regex": null, "enum": [] },
          "profile":     { "min": "36.0", "max": "41.2", "mean": "38.6",
                           "distinct_count": null, "min_length": null,
                           "max_length": null, "sample_values": ["38.5"] },
          "confidence":   "high",
          "inferred_from": "...",
          "attachments":  []
        }
      ]
    }

The structure is deliberately FLAT (no nested children). Parent/child
composition is expressed via `relationships[].kind == "parent"` so deep
hierarchies don't blow up indentation and so polymorphic parents stay
trivially expressible (`to: ["dog.uuid", "owner.uuid"]`).
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
    normalize_list,
    normalize_metadata,
    normalize_suggested_answers,
    safe_float,
)
from models.partner.tbl_partner_conversation_attachments import (
    PartnerConversationAttachment,
)
from models.partner.tbl_partner_conversation_histories import PartnerConversationHistory
from models.partner.tbl_partner_conversations import PartnerConversation


try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("partner.chat.part6")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


_PART_NO = 6
_PART6_MAX_TURNS_BEFORE_CONFIRM = 10

# Allowed enum vocabularies. Kept small on purpose -- it's easier to expand
# later than to retrofit consumers when we drop something.
_ENTITY_STATUS_ENUM = ["draft", "confirmed", "deprecated"]
_RELATIONSHIP_KIND_ENUM = ["parent", "reference", "many_to_many"]
_ATTACHMENT_KIND_ENUM = [
    "sample_data",
    "spec",
    "screenshot",
    "erd",
    "message",
    "email",
    "other",
]
_CONSTRAINT_KIND_ENUM = ["unique", "check"]

_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}
_NULLABLE_STRING = {"type": ["string", "null"]}
_NULLABLE_BOOL = {"type": ["boolean", "null"]}


# ---------------------------------------------------------------------------
# JSON schema  -- strict, mirrors the persisted shape so the LLM produces
# something we can store directly with minimal massaging
# ---------------------------------------------------------------------------

_RELATIONSHIP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "to", "cardinality", "nullable", "on_delete", "discriminator"],
    "properties": {
        "kind": {"type": "string", "enum": _RELATIONSHIP_KIND_ENUM},
        # Always an array so polymorphic parents (`["dog.uuid","owner.uuid"]`)
        # and single targets share one shape.
        "to": _STRING_ARRAY,
        "cardinality": _NULLABLE_STRING,  # e.g. "1:N", "N:1", "1:1", "N:M"
        "nullable": _NULLABLE_BOOL,
        "on_delete": _NULLABLE_STRING,    # e.g. "cascade", "set_null", "restrict"
        "discriminator": _NULLABLE_STRING,  # for polymorphic parents
    },
}

_CONSTRAINT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "on", "expression", "rationale"],
    "properties": {
        "kind": {"type": "string", "enum": _CONSTRAINT_KIND_ENUM},
        "on": _STRING_ARRAY,             # column ids the constraint applies to
        "expression": _NULLABLE_STRING,  # for kind=check
        "rationale": _NULLABLE_STRING,
    },
}

_ATTACHMENT_REF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "kind",
        "attachment_id",
        "path",
        "region",
        "description",
        "source",
        "url",
        "excerpt",
    ],
    "properties": {
        "kind": {"type": "string", "enum": _ATTACHMENT_KIND_ENUM},
        "attachment_id": _NULLABLE_STRING,  # FK to tbl_partner_conversation_attachments
        "path": _NULLABLE_STRING,
        "region": _NULLABLE_STRING,         # e.g. "Sheet1!A1:G1500"
        "description": _NULLABLE_STRING,
        "source": _NULLABLE_STRING,         # e.g. "slack", "email"
        "url": _NULLABLE_STRING,
        "excerpt": _NULLABLE_STRING,
    },
}

# Profile + constraint sub-objects on attributes use string fields throughout
# so the strict schema stays simple and we don't fight the type system over
# numbers vs decimals vs strings. Coercion happens in normalisation if we
# ever need it downstream.
_ATTRIBUTE_PROFILE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "min",
        "max",
        "mean",
        "distinct_count",
        "min_length",
        "max_length",
        "sample_values",
    ],
    "properties": {
        "min": _NULLABLE_STRING,
        "max": _NULLABLE_STRING,
        "mean": _NULLABLE_STRING,
        "distinct_count": _NULLABLE_STRING,
        "min_length": _NULLABLE_STRING,
        "max_length": _NULLABLE_STRING,
        "sample_values": _STRING_ARRAY,
    },
}

_ATTRIBUTE_CONSTRAINTS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["min", "max", "regex", "enum"],
    "properties": {
        "min": _NULLABLE_STRING,
        "max": _NULLABLE_STRING,
        "regex": _NULLABLE_STRING,
        "enum": _STRING_ARRAY,
    },
}

_ATTRIBUTE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id",
        "type",
        "role",
        "nullable",
        "format",
        "unit",
        "description",
        "aliases",
        "sensitivity",
        "constraints",
        "profile",
        "confidence",
        "inferred_from",
        "attachments",
    ],
    "properties": {
        "id": {"type": "string"},          # fully qualified, e.g. dog.medical_history.temperature
        "type": {"type": "string"},        # e.g. "uuid" / "string" / "decimal(4,1)" / "date"
        "role": _NULLABLE_STRING,          # primary_key | foreign_key | attribute
        "nullable": _NULLABLE_BOOL,
        "format": _NULLABLE_STRING,        # e.g. "email" / "iso8601"
        "unit": _NULLABLE_STRING,          # e.g. "celsius" / "kg"
        "description": _NULLABLE_STRING,
        "aliases": _STRING_ARRAY,
        "sensitivity": _STRING_ARRAY,      # e.g. ["pii"], ["pci"], ["secret"]
        "constraints": _ATTRIBUTE_CONSTRAINTS_SCHEMA,
        "profile": _ATTRIBUTE_PROFILE_SCHEMA,
        "confidence": _NULLABLE_STRING,    # "high" | "medium" | "low" | null
        "inferred_from": _NULLABLE_STRING,
        "attachments": {"type": "array", "items": _ATTACHMENT_REF_SCHEMA},
    },
}

_ENTITY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id",
        "name",
        "aliases",
        "description",
        "status",
        "sensitivity",
        "volume_estimate",
        "relationships",
        "constraints",
        "attachments",
        "open_questions",
        "attributes",
    ],
    "properties": {
        "id": {"type": "string"},          # hierarchical: e.g. dog.medical_history
        "name": {"type": "string"},        # display name, e.g. MedicalHistory
        "aliases": _STRING_ARRAY,
        "description": _NULLABLE_STRING,
        "status": {"type": "string", "enum": _ENTITY_STATUS_ENUM},
        "sensitivity": _STRING_ARRAY,
        "volume_estimate": _NULLABLE_STRING,  # e.g. "~10" / "~1k" / "~1M" / "~1B"
        "relationships": {"type": "array", "items": _RELATIONSHIP_SCHEMA},
        "constraints": {"type": "array", "items": _CONSTRAINT_SCHEMA},
        "attachments": {"type": "array", "items": _ATTACHMENT_REF_SCHEMA},
        "open_questions": _STRING_ARRAY,
        "attributes": {"type": "array", "items": _ATTRIBUTE_SCHEMA},
    },
}

_ENTITY_MODEL_JSON_SCHEMA = {
    "name": "partner_entity_model_response",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "reply_to_user",
            "assistant_suggested_answers",
            "entity_model_summary",
            "entities",
            "removed_entities",
            "open_questions",
            "missing_fields",
            "confidence",
            "reason",
        ],
        "properties": {
            "status": {"type": "string", "enum": ["ask_followup", "confirm", "complete"]},
            "reply_to_user": {"type": "string"},
            "assistant_suggested_answers": _STRING_ARRAY,
            "entity_model_summary": {"type": "string"},
            "entities": {"type": "array", "items": _ENTITY_SCHEMA},
            # Entity ids the user asked to drop. Strings, not objects, so the
            # model can't accidentally re-add fields while dropping.
            "removed_entities": _STRING_ARRAY,
            # Conversation-level questions the extractor couldn't resolve.
            "open_questions": _STRING_ARRAY,
            "missing_fields": _STRING_ARRAY,
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
    },
}


# ---------------------------------------------------------------------------
# context payloads  -- everything part6 needs from earlier parts
# ---------------------------------------------------------------------------


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


def _context_payload(conversation: PartnerConversation) -> Dict[str, Any]:
    return {
        "solution_overview_summary": conversation.solution_overview_summary,
        "solution_overview_recommended_focus": conversation.solution_overview_recommended_focus or [],
        "objectives_summary": conversation.objectives_summary,
        "objectives_high_level": conversation.objectives_high_level or [],
        "objectives_intent": conversation.objectives_intent or [],
        "objectives_success_criteria": conversation.objectives_success_criteria or [],
        "scope_summary": conversation.scope_summary,
        "scope_in_scope": conversation.scope_in_scope or [],
        "scope_out_of_scope": conversation.scope_out_of_scope or [],
        "scope_assumptions": conversation.scope_assumptions or [],
        "actors_summary": conversation.actors_summary,
        "actors": conversation.actors or [],
    }


def _attachment_summaries(db, conversation_id) -> List[Dict[str, Any]]:
    """Compact list of uploaded attachments so the LLM knows what artifacts
    exist and can reference them by id inside entity/attribute attachments.
    Only safe metadata (no raw bytes) is exposed here.
    """
    rows = (
        db.query(PartnerConversationAttachment)
        .filter(
            PartnerConversationAttachment.conversation_id == conversation_id,
            PartnerConversationAttachment.status == "ready",
        )
        .order_by(PartnerConversationAttachment.created_at.asc())
        .all()
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        summary_text = getattr(row, "summary_text", None)
        out.append(
            {
                "attachment_id": str(getattr(row, "id", "")) or None,
                "filename": getattr(row, "filename", None),
                "content_type": getattr(row, "content_type", None),
                "classification": getattr(row, "classification", None),
                "summary": getattr(row, "summary", None),
                "summary_text_preview": (summary_text[:1000] if summary_text else None),
                "extracted_fields": getattr(row, "extracted_fields", None),
            }
        )
    return out


# ---------------------------------------------------------------------------
# normalisation  -- coerce the model output into a stable, persistable shape
# ---------------------------------------------------------------------------


def _normalize_str_list(v: Any, lower_unique: bool = True) -> List[str]:
    if not isinstance(v, list):
        return []
    out: List[str] = []
    seen = set()
    for item in v:
        s = clean_str(item)
        if not s:
            continue
        key = s.lower() if lower_unique else s
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _normalize_relationship(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    kind = clean_str(item.get("kind"))
    if kind not in _RELATIONSHIP_KIND_ENUM:
        return None
    to_val = item.get("to")
    if isinstance(to_val, str):
        to_list = [s for s in [clean_str(to_val)] if s]
    else:
        to_list = _normalize_str_list(to_val, lower_unique=False)
    if not to_list:
        return None
    nullable = item.get("nullable")
    if not isinstance(nullable, bool):
        nullable = None
    return {
        "kind": kind,
        "to": to_list,
        "cardinality": clean_str(item.get("cardinality")),
        "nullable": nullable,
        "on_delete": clean_str(item.get("on_delete")),
        "discriminator": clean_str(item.get("discriminator")),
    }


def _normalize_constraint(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    kind = clean_str(item.get("kind"))
    if kind not in _CONSTRAINT_KIND_ENUM:
        return None
    return {
        "kind": kind,
        "on": _normalize_str_list(item.get("on"), lower_unique=False),
        "expression": clean_str(item.get("expression")),
        "rationale": clean_str(item.get("rationale")),
    }


def _normalize_attachment_ref(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    kind = clean_str(item.get("kind"))
    if kind not in _ATTACHMENT_KIND_ENUM:
        return None
    return {
        "kind": kind,
        "attachment_id": clean_str(item.get("attachment_id")),
        "path": clean_str(item.get("path")),
        "region": clean_str(item.get("region")),
        "description": clean_str(item.get("description")),
        "source": clean_str(item.get("source")),
        "url": clean_str(item.get("url")),
        "excerpt": clean_str(item.get("excerpt")),
    }


def _normalize_profile(v: Any) -> Dict[str, Any]:
    if not isinstance(v, dict):
        v = {}
    return {
        "min": clean_str(v.get("min")),
        "max": clean_str(v.get("max")),
        "mean": clean_str(v.get("mean")),
        "distinct_count": clean_str(v.get("distinct_count")),
        "min_length": clean_str(v.get("min_length")),
        "max_length": clean_str(v.get("max_length")),
        "sample_values": _normalize_str_list(v.get("sample_values"), lower_unique=False),
    }


def _normalize_attribute_constraints(v: Any) -> Dict[str, Any]:
    if not isinstance(v, dict):
        v = {}
    return {
        "min": clean_str(v.get("min")),
        "max": clean_str(v.get("max")),
        "regex": clean_str(v.get("regex")),
        "enum": _normalize_str_list(v.get("enum"), lower_unique=False),
    }


def _normalize_attribute(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    aid = clean_str(item.get("id"))
    atype = clean_str(item.get("type"))
    if not aid or not atype:
        return None
    nullable = item.get("nullable")
    if not isinstance(nullable, bool):
        nullable = None
    attachments = [
        a
        for a in (
            _normalize_attachment_ref(x) for x in (item.get("attachments") or [])
        )
        if a
    ]
    return {
        "id": aid,
        "type": atype,
        "role": clean_str(item.get("role")),
        "nullable": nullable,
        "format": clean_str(item.get("format")),
        "unit": clean_str(item.get("unit")),
        "description": clean_str(item.get("description")),
        "aliases": _normalize_str_list(item.get("aliases")),
        "sensitivity": _normalize_str_list(item.get("sensitivity")),
        "constraints": _normalize_attribute_constraints(item.get("constraints")),
        "profile": _normalize_profile(item.get("profile")),
        "confidence": clean_str(item.get("confidence")),
        "inferred_from": clean_str(item.get("inferred_from")),
        "attachments": attachments,
    }


def _normalize_entity(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    eid = clean_str(item.get("id"))
    ename = clean_str(item.get("name"))
    if not eid or not ename:
        return None
    status = clean_str(item.get("status"))
    if status not in _ENTITY_STATUS_ENUM:
        status = "draft"
    relationships = [
        r
        for r in (_normalize_relationship(x) for x in (item.get("relationships") or []))
        if r
    ]
    constraints = [
        c
        for c in (_normalize_constraint(x) for x in (item.get("constraints") or []))
        if c
    ]
    attachments = [
        a
        for a in (_normalize_attachment_ref(x) for x in (item.get("attachments") or []))
        if a
    ]
    attributes_seen: set = set()
    attributes: List[Dict[str, Any]] = []
    for raw_attr in item.get("attributes") or []:
        attr = _normalize_attribute(raw_attr)
        if not attr:
            continue
        akey = attr["id"].lower()
        if akey in attributes_seen:
            continue
        attributes_seen.add(akey)
        attributes.append(attr)
    return {
        "id": eid,
        "name": ename,
        "aliases": _normalize_str_list(item.get("aliases")),
        "description": clean_str(item.get("description")),
        "status": status,
        "sensitivity": _normalize_str_list(item.get("sensitivity")),
        "volume_estimate": clean_str(item.get("volume_estimate")),
        "relationships": relationships,
        "constraints": constraints,
        "attachments": attachments,
        "open_questions": _normalize_str_list(item.get("open_questions"), lower_unique=False),
        "attributes": attributes,
    }


def _normalize_entity_list(v: Any) -> List[Dict[str, Any]]:
    if not isinstance(v, list):
        return []
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in v:
        ent = _normalize_entity(item)
        if not ent:
            continue
        key = ent["id"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(ent)
    return out


def _merge_and_remove_entities(
    current: Any,
    added: List[Dict[str, Any]],
    removed_entity_ids: List[str],
) -> List[Dict[str, Any]]:
    """Merge new/updated entities into the existing list, then drop any the
    user asked to remove. Matching is by ``id`` (case-insensitive)."""
    existing = _normalize_entity_list(current if isinstance(current, list) else [])
    merged: List[Dict[str, Any]] = []
    index_by_id: Dict[str, int] = {}

    for row in existing:
        key = row["id"].lower()
        index_by_id[key] = len(merged)
        merged.append(row)

    for row in added:
        key = row["id"].lower()
        pos = index_by_id.get(key)
        if pos is None:
            index_by_id[key] = len(merged)
            merged.append(row)
        else:
            # Replace in place -- the model is producing the up-to-date view
            # of each entity it touches in this turn.
            merged[pos] = row

    if not removed_entity_ids:
        return merged

    removed_keys = {name.lower() for name in removed_entity_ids if clean_str(name)}
    return [row for row in merged if row["id"].lower() not in removed_keys]


# ---------------------------------------------------------------------------
# prerequisites
# ---------------------------------------------------------------------------


def _prerequisites_met(conversation: PartnerConversation) -> bool:
    """Part6 needs SOMETHING substantive from earlier parts to ground the
    entity extraction. We don't require all of them, but at least one of
    objectives, scope, or actors must have content."""
    if not clean_str(conversation.partner_name):
        return False
    return bool(
        (conversation.objectives_high_level or [])
        or (conversation.scope_in_scope or [])
        or (conversation.actors or [])
        or clean_str(conversation.scope_summary)
        or clean_str(conversation.objectives_summary)
    )


# ---------------------------------------------------------------------------
# payload + command
# ---------------------------------------------------------------------------


class RunPartnerEntityModelPayload(BaseModel):
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


class RunPartnerEntityModelCommand(BaseCommand):
    name = "partner/chat/part6"
    schema = RunPartnerEntityModelPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Partner"

    SYSTEM_PROMPT = """
You are Solitud's entity-model interviewer.

Your job in this sub-conversation is to turn the prior parts' outputs and any
attached artifacts into a flat, machine-readable entity model. The model you
produce will be consumed by downstream FE/BE generation steps, so it must be
precise, self-describing, and addressable.

Output rules (REPEAT THESE TO YOURSELF):

1. Entities are FLAT. There is no nested children list. Composition is
   expressed only via relationships[].kind == "parent".
2. Every entity id is hierarchical and globally unique
   (e.g. "dog", "dog.medical_history"). Every attribute id is the entity id
   plus the attribute name (e.g. "dog.medical_history.temperature").
3. relationships[].kind is exactly one of:
     - "parent"        composition; child can't exist without parent.
                       The implied FK is NOT a separate attribute.
     - "reference"     association; peer FK relationship.
     - "many_to_many"  shorthand when no junction entity is defined yet.
   `to` is ALWAYS an array of target ids. A polymorphic parent uses multiple
   ids: e.g. ["dog.uuid","owner.uuid"].
4. Attribute types should be inferred from sample values when present
   (decimal(4,1), date, uuid, string, text, integer, boolean, timestamp, ...).
   When inferred from samples, set confidence="high" and inferred_from to a
   short note describing the evidence. When assumed without evidence, set
   confidence="low".
5. Attribute profile carries data PHYSICALITY: min/max/mean for numbers,
   min_length/max_length for strings, distinct_count and a small
   sample_values list. Use null when not knowable.
6. sensitivity uses an array of: public | internal | confidential | pii | phi
   | pci | secret. Multiple may apply (e.g. ["pii","confidential"]).
7. Attachments may be referenced at entity OR attribute level. Use the
   attachment_id from the attachments-context list when applicable; otherwise
   leave attachment_id null and put a description.
8. open_questions (entity-level AND top-level) capture what you could NOT
   resolve. Surface them; do not silently guess.

Tone:
- 1-3 sentence reply_to_user. Practical, not salesy.
- Ask ONE focused question per turn unless presenting the summary.
- 3-5 short assistant_suggested_answers as quick-reply chips.
- Never mention internal labels like part1, part2, part3, etc.
- Do NOT say "move on to partX" or "proceed to partX".
- If you refer to what comes after this step, say only "the next step".

State machine:
- status = "ask_followup" while entities are missing, attributes are too
  thin, or unresolved open questions block confidence.
- status = "confirm" once you have a coherent first-pass entity model.
  Present a concise summary and ask: "Does this look right? Anything to add,
  change, or remove?"
- status = "complete" only after the user's next reply clearly approves
  ("yes", "looks good", "confirm", etc.).
- Never set status = "complete" on the same turn as the first summary.

Removal rules:
- If the user asks to remove an entity, list its id in "removed_entities"
  using the exact stored id (e.g. "dog.medical_history").
- Do NOT include the same id in both "entities" and "removed_entities".

missing_fields should be a short list of what's still thin (for example
"entities", "attributes:dog", "relationships:dog.medical_history").

Return only the JSON object that satisfies the response schema.
""".strip()

    # ------------------------------------------------------------------
    # input building
    # ------------------------------------------------------------------

    def _build_input_messages(
        self,
        conversation: PartnerConversation,
        history_rows: List[PartnerConversationHistory],
        attachment_context: List[Dict[str, Any]],
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
                "content": "Context from earlier parts:\n"
                + json.dumps(_context_payload(conversation), ensure_ascii=False),
            },
        ]

        if attachment_context:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Attachments uploaded to this conversation. When an entity or "
                        "attribute is grounded in one of these, reference it by "
                        "attachment_id inside the entity/attribute attachments array "
                        "so provenance is preserved:\n"
                        + json.dumps(attachment_context, ensure_ascii=False)
                    ),
                }
            )

        prior_state = {
            "entity_model_status": conversation.entity_model_status,
            "entity_model_summary": conversation.entity_model_summary,
            "entity_model": conversation.entity_model or [],
            "entity_model_open_questions": conversation.entity_model_open_questions or [],
            "entity_model_missing_fields": conversation.entity_model_missing_fields or [],
            "entity_model_confidence": (
                float(conversation.entity_model_confidence)
                if conversation.entity_model_confidence is not None
                else None
            ),
        }
        prior_state = {k: v for k, v in prior_state.items() if v not in (None, [], "")}
        if prior_state:
            messages.append(
                {
                    "role": "system",
                    "content": "Current entity model state:\n"
                    + json.dumps(prior_state, ensure_ascii=False),
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
                        "Produce the first-pass entity model now. Extract the entities "
                        "implied by the prior parts and any attached artifacts, give "
                        "each one a hierarchical id, list attributes with inferred "
                        "types and value profiles, and express parent/reference "
                        "relationships explicitly. Surface anything you couldn't "
                        "resolve as open_questions instead of guessing."
                    ),
                }
            )

        return messages

    # ------------------------------------------------------------------
    # parsing + normalisation
    # ------------------------------------------------------------------

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
                "I had trouble with that response. What's the most important entity "
                "we should model first -- the central thing your business creates, "
                "tracks, or sells?"
            ),
            "assistant_suggested_answers": [
                "Customer",
                "Order",
                "Product",
                "Reservation",
            ],
            "entity_model_summary": "",
            "entities": [],
            "removed_entities": [],
            "open_questions": [],
            "missing_fields": ["entities"],
            "confidence": 0.0,
            "reason": "Model did not return valid JSON.",
        }

    def _normalize_result(self, parsed: dict) -> dict:
        entities = _normalize_entity_list(parsed.get("entities"))
        removed_entities = normalize_list(parsed.get("removed_entities"))
        entity_ids_lower = {row["id"].lower() for row in entities}
        removed_entities = [
            name for name in removed_entities if name.lower() not in entity_ids_lower
        ]

        return {
            "status": clean_str(parsed.get("status")) or "ask_followup",
            "reply_to_user": clean_str(parsed.get("reply_to_user"))
            or (
                "What's the central thing in this business that gets created, "
                "tracked, or sold? That's usually the first entity to model."
            ),
            "assistant_suggested_answers": normalize_suggested_answers(
                parsed.get("assistant_suggested_answers")
            ),
            "entity_model_summary": clean_str(parsed.get("entity_model_summary")),
            "entities": entities,
            "removed_entities": removed_entities,
            "open_questions": _normalize_str_list(
                parsed.get("open_questions"), lower_unique=False
            ),
            "missing_fields": normalize_list(parsed.get("missing_fields")),
            "confidence": safe_float(parsed.get("confidence")),
            "reason": clean_str(parsed.get("reason")),
        }

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    async def execute(
        self,
        payload: RunPartnerEntityModelPayload,
        user_id: Optional[str] = None,
    ) -> dict:
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        uid_int = as_int_user_id(user_id)
        db = SessionLocal()

        try:
            logger.info(
                "[partner.chat.part6] start conversation_key=%s user_id=%s",
                payload.conversation_key,
                user_id,
            )

            conversation = (
                db.query(PartnerConversation)
                .filter(PartnerConversation.conversation_key == payload.conversation_key)
                .first()
            )

            if conversation is None:
                raise HTTPException(
                    status_code=404,
                    detail="Conversation not found. Run part1 first.",
                )

            if not _prerequisites_met(conversation):
                raise HTTPException(
                    status_code=409,
                    detail="Earlier discovery is too thin to derive an entity model.",
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

            try:
                attachment_context = _attachment_summaries(db, conversation.id)
            except Exception:
                # Attachment table may not be exposed in every environment
                # (e.g. early tests). Fail soft -- entity extraction still
                # works without attachment grounding.
                logger.exception(
                    "[partner.chat.part6] attachment_summaries failed; continuing without"
                )
                attachment_context = []

            client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
            input_messages = self._build_input_messages(
                conversation=conversation,
                history_rows=history_rows,
                attachment_context=attachment_context,
                user_message=payload.message,
                metadata=payload.metadata,
            )

            completion = await client.chat.completions.create(
                model=payload.model_name,
                messages=input_messages,
                response_format={"type": "json_schema", "json_schema": _ENTITY_MODEL_JSON_SCHEMA},
            )

            raw_text = completion.choices[0].message.content or ""
            parsed = self._parse_model_output(raw_text)
            result = self._normalize_result(parsed)

            new_entity_rows = result["entities"] or _normalize_entity_list(
                conversation.entity_model
            )
            merged_entities = _merge_and_remove_entities(
                conversation.entity_model,
                new_entity_rows,
                result["removed_entities"],
            )

            conversation.entity_model = merged_entities
            conversation.entity_model_summary = (
                clean_str(result["entity_model_summary"])
                or conversation.entity_model_summary
            )
            conversation.entity_model_confidence = result["confidence"]
            conversation.entity_model_missing_fields = result["missing_fields"]
            conversation.entity_model_open_questions = result["open_questions"]
            conversation.entity_model_last_user_message = payload.message
            conversation.entity_model_last_assistant_message = result["reply_to_user"]
            conversation.entity_model_assistant_suggested_answers = (
                result["assistant_suggested_answers"]
            )
            conversation.updated_by = uid_int

            previous_status = conversation.entity_model_status
            status = result["status"]
            if status == "ask_followup" and merged_entities:
                turn_count = (
                    db.query(func.count(PartnerConversationHistory.id))
                    .filter(
                        PartnerConversationHistory.conversation_id == conversation.id,
                        PartnerConversationHistory.part_no == _PART_NO,
                    )
                    .scalar()
                ) or 0
                if turn_count >= _PART6_MAX_TURNS_BEFORE_CONFIRM:
                    status = "confirm"

            status = enforce_confirm_before_complete(
                proposed_status=status,
                previous_status=previous_status,
                user_message=payload.message,
            )
            conversation.entity_model_status = status

            current_metadata = dict(conversation.metadata_json or {})
            current_metadata.update(payload.metadata or {})
            current_metadata["part6_prompt_version"] = payload.prompt_version
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

                entity_model_summary=conversation.entity_model_summary,
                entity_model=conversation.entity_model or [],
                entity_model_open_questions=conversation.entity_model_open_questions or [],
                entity_model_confidence=conversation.entity_model_confidence,
                entity_model_missing_fields=conversation.entity_model_missing_fields or [],
                entity_model_status=conversation.entity_model_status,

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
                    "entity_model": conversation.entity_model or [],
                    "entity_model_open_questions": conversation.entity_model_open_questions or [],
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
            logger.exception("[partner.chat.part6] execute failed")
            raise
        finally:
            db.close()


__all__ = [
    "RunPartnerEntityModelCommand",
    "RunPartnerEntityModelPayload",
]
