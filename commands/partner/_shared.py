"""Shared helpers for the partner/chat/* command family.

Each part (part1, part2, part3, ...) follows the same input/output envelope
and writes to the same conversation row, so the normalisation, merge, and
serialisation helpers are shared from here.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from models.partner.tbl_partner_conversation_histories import PartnerConversationHistory
from models.partner.tbl_partner_conversations import PartnerConversation


# ---------------------------------------------------------------------------
# Stage registry  -- single source of truth for the dispatcher
# ---------------------------------------------------------------------------
#
# Maps `current_part` (the integer pointer stored on the conversation row) to
# a (stage_key, stage_label) pair. The dispatcher uses this to:
#   - decide which sub-conversation command to invoke,
#   - emit the global `stage` / `stage_label` fields in the response.
#
# Adding a new sub-conversation later is a one-line edit here. Keys must
# stay contiguous starting at 1.

STAGE_REGISTRY: Dict[int, Tuple[str, str]] = {
    1: ("customer_profile", "Customer Profile"),
    2: ("solution_overview", "Solution Overview"),
    3: ("objectives", "Objectives"),
    4: ("scope_limitations", "Scope & Limitations"),
    5: ("actors_roles", "Actors & Roles"),
    6: ("entity_model", "Entity Model"),
}

LAST_PART_NO: int = max(STAGE_REGISTRY.keys())

# Sentinel returned for any part_no past the last registered stage. The flow
# is over and there is nothing left to dispatch to.
COMPLETE_STAGE: Tuple[str, str] = ("complete", "Complete")


def get_stage_info(part_no: Optional[int]) -> Tuple[str, str]:
    """Return (stage_key, stage_label) for the given part_no.

    A None or non-positive part_no falls back to the first stage so that
    new conversations get a sensible label even before they are persisted.
    """
    if part_no is None or part_no < 1:
        return STAGE_REGISTRY[1]
    if part_no > LAST_PART_NO:
        return COMPLETE_STAGE
    return STAGE_REGISTRY.get(part_no, COMPLETE_STAGE)


def next_part_no(part_no: Optional[int]) -> Optional[int]:
    """Return the next part_no after ``part_no``, or None if the flow is done."""
    if part_no is None or part_no < 1:
        return 1
    nxt = part_no + 1
    if nxt > LAST_PART_NO:
        return None
    return nxt


# Local per-part status column names. The dispatcher reads these to know
# whether a given sub-conversation has reported `complete` and the pointer
# should advance.
PART_LOCAL_STATUS_COLUMN: Dict[int, str] = {
    1: "status",
    2: "solution_overview_status",
    3: "objectives_status",
    4: "scope_status",
    5: "actors_status",
    6: "entity_model_status",
}


def get_local_status(conversation: PartnerConversation, part_no: int) -> Optional[str]:
    column = PART_LOCAL_STATUS_COLUMN.get(part_no)
    if not column:
        return None
    return getattr(conversation, column, None)


# ---------------------------------------------------------------------------
# scalar helpers
# ---------------------------------------------------------------------------


def as_int_user_id(v: Optional[str]) -> Optional[int]:
    if not v:
        return None
    try:
        iv = int(v)
        return iv if iv > 0 else None
    except Exception:
        return None


def clean_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def safe_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        fv = float(v)
        if fv < 0:
            return 0.0
        if fv > 1:
            return 1.0
        return fv
    except Exception:
        return None


def normalize_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if not isinstance(v, list):
        return []

    out: List[str] = []
    seen = set()
    for item in v:
        s = clean_str(item)
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def normalize_suggested_answers(v: Any) -> List[str]:
    return normalize_list(v)[:5]


def normalize_metadata(v: Any) -> Optional[dict]:
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
        except Exception as e:
            raise ValueError(f"metadata must be valid JSON if provided as string: {e}")
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    try:
        return dict(v)
    except Exception:
        raise ValueError("metadata must be a JSON object or JSON string")


_APPROVAL_PREFIXES = (
    "yes",
    "yep",
    "yeah",
    "confirm",
    "confirmed",
    "approve",
    "approved",
    "looks good",
    "looks right",
    "that looks good",
    "that looks right",
    "sounds good",
    "all good",
    "correct",
    "that's right",
    "thats right",
    "ok",
    "okay",
)
_APPROVAL_DISQUALIFIERS = (
    " add ",
    " change ",
    " remove ",
    " drop ",
    " update ",
    " edit ",
    " revise ",
    " tweak ",
    " except ",
    " instead ",
    " however ",
    " but ",
)


def is_explicit_approval(message: Optional[str]) -> bool:
    s = clean_str(message)
    if not s:
        return False

    normalized = " ".join(str(s).lower().split())
    padded = f" {normalized} "
    if any(token in padded for token in _APPROVAL_DISQUALIFIERS):
        return False

    for prefix in _APPROVAL_PREFIXES:
        if normalized == prefix:
            return True
        if normalized.startswith(prefix + " "):
            return True
        if normalized.startswith(prefix + ","):
            return True
        if normalized.startswith(prefix + "."):
            return True

    return bool(re.fullmatch(r"(yes|ok|okay)[!.]*", normalized))


def enforce_confirm_before_complete(
    proposed_status: Optional[str],
    previous_status: Optional[str],
    user_message: Optional[str],
) -> str:
    status = clean_str(proposed_status) or "ask_followup"
    if status != "complete":
        return status
    if clean_str(previous_status) != "confirm":
        return "confirm"
    if not is_explicit_approval(user_message):
        return "confirm"
    return "complete"


# ---------------------------------------------------------------------------
# merge helpers
# ---------------------------------------------------------------------------


def merge_scalar(old_v: Optional[str], new_v: Optional[str]) -> Optional[str]:
    return clean_str(new_v) or clean_str(old_v)


def merge_list(old_v: Optional[List[str]], new_v: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for source in (old_v or [], new_v or []):
        for item in source:
            s = clean_str(item)
            if not s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
    return out


def apply_removal(current: Optional[List[str]], to_remove: Optional[List[str]]) -> List[str]:
    if not to_remove:
        return list(current or [])
    remove_keys = {s.lower() for s in to_remove if s}
    return [x for x in (current or []) if x and x.lower() not in remove_keys]


def merge_and_remove(
    old_v: Optional[List[str]],
    added: Optional[List[str]],
    removed: Optional[List[str]],
) -> List[str]:
    merged = merge_list(old_v, added)
    return apply_removal(merged, removed)


# ---------------------------------------------------------------------------
# JSONB structured-list helpers (used by part2 matches, etc.)
# ---------------------------------------------------------------------------


def normalize_dict_list(v: Any, allowed_keys: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Normalise a list of structured dict entries (e.g. capability matches).

    If ``allowed_keys`` is provided, only those keys are kept and any missing
    ones are filled with ``None`` so the shape stays predictable.
    """
    if not isinstance(v, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in v:
        if not isinstance(item, dict):
            continue
        if allowed_keys is None:
            cleaned = {k: item.get(k) for k in item.keys()}
        else:
            cleaned = {k: item.get(k) for k in allowed_keys}
        out.append(cleaned)
    return out


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------


def conversation_to_dict(m: PartnerConversation) -> dict:
    """Unified serialiser for the conversation row across all parts."""

    def _val(name: str, default: Any = None) -> Any:
        return getattr(m, name, default)

    def _list(name: str) -> list:
        return _val(name) or []

    def _num(name: str) -> Optional[float]:
        v = _val(name)
        return float(v) if v is not None else None

    return {
        "id": str(m.id),
        "created_by": _val("created_by"),
        "user_id": _val("user_id"),
        "conversation_key": _val("conversation_key"),
        "status": _val("status"),
        "current_part": _val("current_part"),

        # ---- part1: profile / BMC ----
        "partner_name": _val("partner_name"),
        "partner_industry": _val("partner_industry"),
        "partner_job_responsibilities": _list("partner_job_responsibilities"),
        "partner_pains": _list("partner_pains"),
        "partner_wishes": _list("partner_wishes"),
        "partner_customer_segments": _list("partner_customer_segments"),
        "partner_customer_relationships": _list("partner_customer_relationships"),
        "partner_channels": _list("partner_channels"),
        "partner_key_activities": _list("partner_key_activities"),
        "partner_key_resources": _list("partner_key_resources"),
        "partner_key_partners": _list("partner_key_partners"),
        "partner_revenue_streams": _list("partner_revenue_streams"),
        "conversation_summary": _val("conversation_summary"),
        "confidence": _num("confidence"),
        "missing_fields": _list("missing_fields"),
        "last_user_message": _val("last_user_message"),
        "last_assistant_message": _val("last_assistant_message"),
        "assistant_suggested_answers": _val("assistant_suggested_answers") or [],

        # ---- part2: solution overview ----
        "solution_overview_status": _val("solution_overview_status"),
        "solution_overview_summary": _val("solution_overview_summary"),
        "solution_overview_matches": _val("solution_overview_matches") or [],
        "solution_overview_capability_fits": _list("solution_overview_capability_fits"),
        "solution_overview_recommended_focus": _list("solution_overview_recommended_focus"),
        "solution_overview_confidence": _num("solution_overview_confidence"),
        "solution_overview_missing_fields": _list("solution_overview_missing_fields"),
        "solution_overview_last_user_message": _val("solution_overview_last_user_message"),
        "solution_overview_last_assistant_message": _val("solution_overview_last_assistant_message"),
        "solution_overview_assistant_suggested_answers": _val("solution_overview_assistant_suggested_answers") or [],

        # ---- part3: objectives ----
        "objectives_status": _val("objectives_status"),
        "objectives_summary": _val("objectives_summary"),
        "objectives_high_level": _list("objectives_high_level"),
        "objectives_intent": _list("objectives_intent"),
        "objectives_success_criteria": _list("objectives_success_criteria"),
        "objectives_constraints": _list("objectives_constraints"),
        "objectives_confidence": _num("objectives_confidence"),
        "objectives_missing_fields": _list("objectives_missing_fields"),
        "objectives_last_user_message": _val("objectives_last_user_message"),
        "objectives_last_assistant_message": _val("objectives_last_assistant_message"),
        "objectives_assistant_suggested_answers": _val("objectives_assistant_suggested_answers") or [],

        # ---- part4: scope & limitations ----
        "scope_status": _val("scope_status"),
        "scope_summary": _val("scope_summary"),
        "scope_in_scope": _list("scope_in_scope"),
        "scope_out_of_scope": _list("scope_out_of_scope"),
        "scope_assumptions": _list("scope_assumptions"),
        "scope_dependencies": _list("scope_dependencies"),
        "scope_limitations": _list("scope_limitations"),
        "scope_confidence": _num("scope_confidence"),
        "scope_missing_fields": _list("scope_missing_fields"),
        "scope_last_user_message": _val("scope_last_user_message"),
        "scope_last_assistant_message": _val("scope_last_assistant_message"),
        "scope_assistant_suggested_answers": _val("scope_assistant_suggested_answers") or [],

        # ---- part5: actors & roles ----
        "actors_status": _val("actors_status"),
        "actors_summary": _val("actors_summary"),
        "actors": _val("actors") or [],
        "actors_confidence": _num("actors_confidence"),
        "actors_missing_fields": _list("actors_missing_fields"),
        "actors_last_user_message": _val("actors_last_user_message"),
        "actors_last_assistant_message": _val("actors_last_assistant_message"),
        "actors_assistant_suggested_answers": _val("actors_assistant_suggested_answers") or [],

        # ---- part6: entity model ----
        "entity_model_status": _val("entity_model_status"),
        "entity_model_summary": _val("entity_model_summary"),
        "entity_model": _val("entity_model") or [],
        "entity_model_open_questions": _list("entity_model_open_questions"),
        "entity_model_confidence": _num("entity_model_confidence"),
        "entity_model_missing_fields": _list("entity_model_missing_fields"),
        "entity_model_last_user_message": _val("entity_model_last_user_message"),
        "entity_model_last_assistant_message": _val("entity_model_last_assistant_message"),
        "entity_model_assistant_suggested_answers": _val("entity_model_assistant_suggested_answers") or [],

        "metadata": _val("metadata_json") or {},
        "updated_by": _val("updated_by"),
        "created_at": m.created_at.isoformat() if _val("created_at") else None,
        "updated_at": m.updated_at.isoformat() if _val("updated_at") else None,
    }


def history_to_dict(m: PartnerConversationHistory) -> dict:
    """Unified serialiser for a history row across all parts."""

    def _val(name: str, default: Any = None) -> Any:
        return getattr(m, name, default)

    def _list(name: str) -> list:
        return _val(name) or []

    def _num(name: str) -> Optional[float]:
        v = _val(name)
        return float(v) if v is not None else None

    return {
        "id": str(m.id),
        "conversation_id": str(m.conversation_id),
        "sequence_no": _val("sequence_no"),
        "part_no": _val("part_no"),
        "created_by": _val("created_by"),
        "user_id": _val("user_id"),
        "user_message": _val("user_message"),
        "assistant_message": _val("assistant_message"),
        "assistant_suggested_answers": _val("assistant_suggested_answers") or [],

        # part1 snapshot
        "partner_name": _val("partner_name"),
        "partner_industry": _val("partner_industry"),
        "partner_job_responsibilities": _list("partner_job_responsibilities"),
        "partner_pains": _list("partner_pains"),
        "partner_wishes": _list("partner_wishes"),
        "partner_customer_segments": _list("partner_customer_segments"),
        "partner_customer_relationships": _list("partner_customer_relationships"),
        "partner_channels": _list("partner_channels"),
        "partner_key_activities": _list("partner_key_activities"),
        "partner_key_resources": _list("partner_key_resources"),
        "partner_key_partners": _list("partner_key_partners"),
        "partner_revenue_streams": _list("partner_revenue_streams"),
        "conversation_summary": _val("conversation_summary"),
        "confidence": _num("confidence"),
        "missing_fields": _list("missing_fields"),
        "status": _val("status"),
        "reason": _val("reason"),

        # part2 snapshot
        "solution_overview_summary": _val("solution_overview_summary"),
        "solution_overview_matches": _val("solution_overview_matches") or [],
        "solution_overview_capability_fits": _list("solution_overview_capability_fits"),
        "solution_overview_recommended_focus": _list("solution_overview_recommended_focus"),
        "solution_overview_confidence": _num("solution_overview_confidence"),
        "solution_overview_missing_fields": _list("solution_overview_missing_fields"),
        "solution_overview_status": _val("solution_overview_status"),

        # part3 snapshot
        "objectives_summary": _val("objectives_summary"),
        "objectives_high_level": _list("objectives_high_level"),
        "objectives_intent": _list("objectives_intent"),
        "objectives_success_criteria": _list("objectives_success_criteria"),
        "objectives_constraints": _list("objectives_constraints"),
        "objectives_confidence": _num("objectives_confidence"),
        "objectives_missing_fields": _list("objectives_missing_fields"),
        "objectives_status": _val("objectives_status"),

        # part4 snapshot
        "scope_summary": _val("scope_summary"),
        "scope_in_scope": _list("scope_in_scope"),
        "scope_out_of_scope": _list("scope_out_of_scope"),
        "scope_assumptions": _list("scope_assumptions"),
        "scope_dependencies": _list("scope_dependencies"),
        "scope_limitations": _list("scope_limitations"),
        "scope_confidence": _num("scope_confidence"),
        "scope_missing_fields": _list("scope_missing_fields"),
        "scope_status": _val("scope_status"),

        # part5 snapshot
        "actors_summary": _val("actors_summary"),
        "actors": _val("actors") or [],
        "actors_confidence": _num("actors_confidence"),
        "actors_missing_fields": _list("actors_missing_fields"),
        "actors_status": _val("actors_status"),

        # part6 snapshot
        "entity_model_summary": _val("entity_model_summary"),
        "entity_model": _val("entity_model") or [],
        "entity_model_open_questions": _list("entity_model_open_questions"),
        "entity_model_confidence": _num("entity_model_confidence"),
        "entity_model_missing_fields": _list("entity_model_missing_fields"),
        "entity_model_status": _val("entity_model_status"),

        "model_name": _val("model_name"),
        "prompt_version": _val("prompt_version"),
        "metadata": _val("metadata_json") or {},
        "created_at": m.created_at.isoformat() if _val("created_at") else None,
    }


__all__ = [
    "as_int_user_id",
    "clean_str",
    "safe_float",
    "normalize_list",
    "normalize_suggested_answers",
    "normalize_metadata",
    "is_explicit_approval",
    "enforce_confirm_before_complete",
    "merge_scalar",
    "merge_list",
    "apply_removal",
    "merge_and_remove",
    "normalize_dict_list",
    "conversation_to_dict",
    "history_to_dict",
    "STAGE_REGISTRY",
    "LAST_PART_NO",
    "COMPLETE_STAGE",
    "PART_LOCAL_STATUS_COLUMN",
    "get_stage_info",
    "next_part_no",
    "get_local_status",
]
