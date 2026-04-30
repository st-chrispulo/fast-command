"""Single-pass summarizer for partner attachments.

ONE LLM call produces three things at once:
    summary_text       -- 100-300 word prose summary (what we drop in part1-5 prompts)
    summary            -- structured JSONB summary (purpose, key_topics, ...)
    extracted_fields   -- per-stage-column values with confidence + evidence

Call site: ``commands/partner/attachments/upload.py``. Output then flows into
``_attachment_apply.py`` which decides what gets auto-applied vs stashed as a
candidate, based on confidence thresholds.

Prompt version: ``v1`` (initial draft -- we'll tune separately after seeing
real outputs in the wild).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from commands.partner._attachment_extract import PreparedContent
from integrations.llm import get_llm

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("partner.attachment.summarize")
except Exception:
    logger = logging.getLogger(__name__)


PROMPT_VERSION = "v1"

# The use-case key here matches the env override: LLM_PROVIDER_PARTNER_SUMMARIZER
# / LLM_MODEL_PARTNER_SUMMARIZER.
LLM_USE_CASE = "PARTNER_SUMMARIZER"


# --- JSON schema for the response ---------------------------------------
# Kept flat-ish so it's easy to validate. The summarizer is asked to emit
# every key (with null/0/[] defaults if it can't find anything) so the
# downstream apply logic doesn't need defensive .get() calls everywhere.

_FIELD_OBJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "evidence": {"type": ["string", "null"]},
    },
    "required": ["value", "confidence"],
    "additionalProperties": False,
}

_FIELD_LIST_SCHEMA = {
    "type": "array",
    "items": _FIELD_OBJECT_SCHEMA,
}

_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary_text": {"type": "string"},
        "summary": {
            "type": "object",
            "properties": {
                "purpose":        {"type": ["string", "null"]},
                "classification": {"type": ["string", "null"]},
                "key_topics":     {"type": "array", "items": {"type": "string"}},
                "key_insights":   {"type": "array", "items": {"type": "string"}},
                "structures":     {"type": "array", "items": {"type": "string"}},
                "entities": {
                    "type": "object",
                    "properties": {
                        "companies": {"type": "array", "items": {"type": "string"}},
                        "people":    {"type": "array", "items": {"type": "string"}},
                        "products":  {"type": "array", "items": {"type": "string"}},
                        "metrics":   {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": True,
        },
        "extracted_fields": {
            "type": "object",
            "properties": {
                # part1 -- profile & BMC
                "partner_name":      _FIELD_OBJECT_SCHEMA,
                "partner_industry":  _FIELD_OBJECT_SCHEMA,
                "partner_job_responsibilities":   _FIELD_LIST_SCHEMA,
                "partner_pains":                  _FIELD_LIST_SCHEMA,
                "partner_wishes":                 _FIELD_LIST_SCHEMA,
                "partner_customer_segments":      _FIELD_LIST_SCHEMA,
                "partner_customer_relationships": _FIELD_LIST_SCHEMA,
                "partner_channels":               _FIELD_LIST_SCHEMA,
                "partner_key_activities":         _FIELD_LIST_SCHEMA,
                "partner_key_resources":          _FIELD_LIST_SCHEMA,
                "partner_key_partners":           _FIELD_LIST_SCHEMA,
                "partner_revenue_streams":        _FIELD_LIST_SCHEMA,
                # part3 -- objectives
                "objectives_high_level":       _FIELD_LIST_SCHEMA,
                "objectives_intent":           _FIELD_LIST_SCHEMA,
                "objectives_success_criteria": _FIELD_LIST_SCHEMA,
                "objectives_constraints":      _FIELD_LIST_SCHEMA,
                # part4 -- scope & limitations
                "scope_in_scope":     _FIELD_LIST_SCHEMA,
                "scope_out_of_scope": _FIELD_LIST_SCHEMA,
                "scope_assumptions":  _FIELD_LIST_SCHEMA,
                "scope_dependencies": _FIELD_LIST_SCHEMA,
                "scope_limitations":  _FIELD_LIST_SCHEMA,
                # part5 -- actors & roles  (formatted as "actor: role" strings)
                "actors":             _FIELD_LIST_SCHEMA,
            },
            "additionalProperties": True,
        },
    },
    "required": ["summary_text", "summary", "extracted_fields"],
    "additionalProperties": False,
}


# Field categories the summarizer knows about. Used by `_attachment_apply.py`
# to drive the upsert rules.
SCALAR_FIELDS: List[str] = ["partner_name", "partner_industry"]

LIST_FIELDS: List[str] = [
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
    "objectives_high_level",
    "objectives_intent",
    "objectives_success_criteria",
    "objectives_constraints",
    "scope_in_scope",
    "scope_out_of_scope",
    "scope_assumptions",
    "scope_dependencies",
    "scope_limitations",
    "actors",
]


# --- prompt --------------------------------------------------------------
# v1 draft -- we will tune iteratively. Keep the system prompt focused on
# structure and confidence semantics; the user prompt carries the file.

_SYSTEM_PROMPT_V1 = """\
You are an analyst preparing a partner-discovery brief from a single attached document.

Your job is to:
  1. Write a concise prose summary (100-300 words) that captures the document's
     purpose, structure, and the most important insights -- written so a
     teammate can understand the doc WITHOUT re-reading it.
  2. Produce a structured summary (purpose, classification, key topics,
     key insights, structures, entities).
  3. Extract any values that fit pre-defined partner-discovery fields, with a
     confidence score in [0, 1] and a short evidence excerpt.

Confidence semantics (BE STRICT):
  - 0.9+   the doc explicitly states this with no ambiguity
  - 0.7-0.9 strong implication or aggregated from multiple consistent mentions
  - 0.5-0.7 reasonable inference; could be wrong
  - <0.5    speculative -- prefer to omit rather than emit low-confidence noise

Field guidance:
  - partner_name / partner_industry: scalar; only one value each.
  - List fields: short phrases, not full sentences. Dedupe.
  - actors: format as "<actor>: <role>" e.g. "Operations Manager: approves rollouts".
  - If the document is unrelated to a partner discovery (e.g. it's a personal
    photo or random text), return empty arrays / nulls and reflect that in
    summary_text.

Always return ALL keys defined in the schema. Use null / 0 / [] when nothing
is found. Do NOT add commentary outside the JSON.
"""


# --- output ---------------------------------------------------------------


@dataclass
class SummarizerOutput:
    summary_text: str = ""
    summary: Dict[str, Any] = field(default_factory=dict)
    extracted_fields: Dict[str, Any] = field(default_factory=dict)
    classification: str = "other"
    model: str = ""
    provider: str = ""
    prompt_version: str = PROMPT_VERSION
    error: Optional[str] = None


# --- main entry point ----------------------------------------------------


def _build_user_prompt(prepared: PreparedContent, filename: str) -> str:
    parts: List[str] = []
    parts.append(f"Filename: {filename}")
    parts.append(f"Detected classification: {prepared.classification}")
    if prepared.page_count:
        parts.append(f"Page count: {prepared.page_count}")
    if prepared.row_count:
        parts.append(f"Row count: {prepared.row_count}")
    if prepared.col_count:
        parts.append(f"Column count: {prepared.col_count}")
    if prepared.truncated:
        parts.append("Note: content was truncated for length.")
    if prepared.notes:
        parts.append("Reader notes: " + "; ".join(prepared.notes))

    parts.append("")
    if prepared.text_excerpt:
        parts.append("=== DOCUMENT CONTENT ===")
        parts.append(prepared.text_excerpt)
    elif prepared.images:
        parts.append("=== DOCUMENT CONTENT ===")
        parts.append("(see attached image(s); analyze visually)")
    else:
        parts.append("=== DOCUMENT CONTENT ===")
        parts.append(
            "(no readable content was extracted; produce a metadata-only summary "
            "and empty extracted_fields)"
        )

    return "\n".join(parts)


async def summarize_attachment(
    *,
    prepared: PreparedContent,
    filename: str,
    timeout: Optional[float] = 30.0,
) -> SummarizerOutput:
    """Run the single-pass summarizer over a prepared attachment."""
    out = SummarizerOutput(classification=prepared.classification)

    try:
        llm = get_llm(use_case=LLM_USE_CASE)
    except Exception as e:
        logger.exception("summarize_attachment: failed to init LLM provider")
        out.error = f"llm_init_failed: {e}"
        return out

    user_prompt = _build_user_prompt(prepared, filename)

    try:
        result = await llm.complete_json(
            system=_SYSTEM_PROMPT_V1,
            user=user_prompt,
            schema=_RESPONSE_SCHEMA,
            schema_name="partner_attachment_summary",
            images=prepared.images or None,
            timeout=timeout,
            max_output_tokens=2000,
        )
    except Exception as e:
        logger.exception("summarize_attachment: LLM call failed")
        out.error = f"llm_call_failed: {e}"
        return out

    content = result.content or {}
    out.summary_text = str(content.get("summary_text") or "").strip()
    out.summary = content.get("summary") or {}
    out.extracted_fields = content.get("extracted_fields") or {}
    out.model = result.model
    out.provider = result.provider

    # Trust the LLM's classification if it gave one, but fall back to the
    # file-type sniffer's guess.
    summary_class = (out.summary or {}).get("classification")
    if isinstance(summary_class, str) and summary_class.strip():
        out.classification = summary_class.strip()

    return out


__all__ = [
    "SummarizerOutput",
    "summarize_attachment",
    "PROMPT_VERSION",
    "LLM_USE_CASE",
    "SCALAR_FIELDS",
    "LIST_FIELDS",
]
