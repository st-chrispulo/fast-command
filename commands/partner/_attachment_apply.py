"""Apply summarizer-extracted fields to the parent conversation row.

Rules (locked in design discussion):
  * Auto-apply threshold:    confidence >= 0.9
  * Candidate range:         0.5 <= confidence < 0.9
  * Drop:                    confidence < 0.5
  * Scalars never overwrite an already-set value (user_message wins). A
    >= 0.9 scalar that's blocked by an existing value is logged as a
    candidate so the FE can prompt the user.
  * Lists dedupe-append: items >= 0.9 are merged in unique-only.

Side effects on the conversation row:
  * stage scalar/list columns get upserted in place
  * ``field_sources``    -- audit trail; per-column object or per-element
                            array of provenance
  * ``field_candidates`` -- conversation-wide candidate bucket, keyed by
                            stage column

Returns ``ApplyOutcome`` with the per-attachment ``applied_fields`` and
``candidate_fields`` dicts that the caller writes back onto the attachment
row for audit.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from commands.partner._attachment_summarize import LIST_FIELDS, SCALAR_FIELDS
from commands.partner._shared import clean_str
from models.partner.tbl_partner_conversations import PartnerConversation

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("partner.attachment.apply")
except Exception:
    logger = logging.getLogger(__name__)


# Tunable via env in case we want to ratchet thresholds without a code change.
def _f(env: str, default: float) -> float:
    raw = os.environ.get(env)
    if not raw:
        return default
    try:
        v = float(raw)
        if v < 0:
            return 0.0
        if v > 1:
            return 1.0
        return v
    except Exception:
        return default


AUTO_APPLY_THRESHOLD = _f("PARTNER_ATTACH_AUTO_APPLY_THRESHOLD", 0.9)
CANDIDATE_THRESHOLD = _f("PARTNER_ATTACH_CANDIDATE_THRESHOLD", 0.5)


# --- output --------------------------------------------------------------


@dataclass
class ApplyOutcome:
    applied_fields: Dict[str, Any] = field(default_factory=dict)
    candidate_fields: Dict[str, Any] = field(default_factory=dict)
    dropped_fields: Dict[str, Any] = field(default_factory=dict)


# --- helpers -------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conf(entry: Any) -> float:
    if not isinstance(entry, dict):
        return 0.0
    try:
        return max(0.0, min(1.0, float(entry.get("confidence", 0.0))))
    except Exception:
        return 0.0


def _value(entry: Any) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    return clean_str(entry.get("value"))


def _evidence(entry: Any) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    return clean_str(entry.get("evidence"))


def _bucket(conf: float) -> str:
    if conf >= AUTO_APPLY_THRESHOLD:
        return "apply"
    if conf >= CANDIDATE_THRESHOLD:
        return "candidate"
    return "drop"


def _record(
    target: Dict[str, Any],
    field_name: str,
    payload: Any,
) -> None:
    """Append/store a payload under field_name. Lists accumulate; scalars stash an object."""
    if field_name in SCALAR_FIELDS:
        target[field_name] = payload
    else:
        target.setdefault(field_name, []).append(payload)


def _seen_lower(values: List[Any]) -> set:
    out = set()
    for v in values or []:
        s = clean_str(v)
        if s:
            out.add(s.lower())
    return out


# --- the apply pass ------------------------------------------------------


def apply_extracted_fields(
    conversation: PartnerConversation,
    *,
    extracted_fields: Dict[str, Any],
    attachment_id: UUID,
    filename: str,
) -> ApplyOutcome:
    """Apply ``extracted_fields`` to ``conversation`` in place and return the audit dicts.

    Caller is responsible for committing the session.
    """
    out = ApplyOutcome()
    if not extracted_fields:
        return out

    now = _now_iso()
    attach_id_str = str(attachment_id)

    # Mutable views of the JSONB columns so SQLAlchemy can detect changes.
    sources: Dict[str, Any] = dict(conversation.field_sources or {})
    candidates: Dict[str, Any] = dict(conversation.field_candidates or {})

    # ---------------- scalars ----------------
    for fname in SCALAR_FIELDS:
        entry = extracted_fields.get(fname)
        if not isinstance(entry, dict):
            continue
        value = _value(entry)
        conf = _conf(entry)
        evidence = _evidence(entry)

        if value is None:
            continue

        bucket = _bucket(conf)
        if bucket == "drop":
            _record(out.dropped_fields, fname, {
                "value": value, "confidence": conf, "reason": "below_candidate_threshold",
            })
            continue

        provenance = {
            "value": value,
            "confidence": conf,
            "evidence": evidence,
            "attachment_id": attach_id_str,
            "filename": filename,
            "captured_at": now,
        }

        if bucket == "candidate":
            _record(out.candidate_fields, fname, {**provenance, "reason": "below_auto_apply_threshold"})
            candidates.setdefault(fname, []).append({**provenance, "blocked_reason": "low_confidence"})
            continue

        # bucket == "apply"
        current = clean_str(getattr(conversation, fname, None))
        if current:
            # Conflict: never overwrite a user-set scalar.
            blocked = {**provenance, "reason": "scalar_already_set", "current_value": current}
            _record(out.candidate_fields, fname, blocked)
            candidates.setdefault(fname, []).append({**provenance, "blocked_reason": "scalar_already_set"})
            continue

        # Apply.
        setattr(conversation, fname, value)
        sources[fname] = {
            "source": "attachment",
            "attachment_id": attach_id_str,
            "filename": filename,
            "confidence": conf,
            "set_at": now,
        }
        _record(out.applied_fields, fname, provenance)

    # ---------------- lists ----------------
    for fname in LIST_FIELDS:
        entries = extracted_fields.get(fname)
        if not isinstance(entries, list) or not entries:
            continue

        current_list: List[Any] = list(getattr(conversation, fname, []) or [])
        seen = _seen_lower(current_list)

        # field_sources for arrays is a parallel list. Ensure it exists.
        field_src_arr = sources.get(fname)
        if not isinstance(field_src_arr, list):
            field_src_arr = []

        added_any = False
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            value = _value(entry)
            conf = _conf(entry)
            evidence = _evidence(entry)
            if value is None:
                continue

            bucket = _bucket(conf)
            if bucket == "drop":
                _record(out.dropped_fields, fname, {
                    "value": value, "confidence": conf, "reason": "below_candidate_threshold",
                })
                continue

            provenance = {
                "value": value,
                "confidence": conf,
                "evidence": evidence,
                "attachment_id": attach_id_str,
                "filename": filename,
                "captured_at": now,
            }

            if bucket == "candidate":
                _record(out.candidate_fields, fname, {**provenance, "reason": "below_auto_apply_threshold"})
                candidates.setdefault(fname, []).append({**provenance, "blocked_reason": "low_confidence"})
                continue

            # bucket == "apply" -- dedupe-append
            key = value.lower()
            if key in seen:
                _record(out.dropped_fields, fname, {
                    "value": value, "confidence": conf, "reason": "duplicate",
                })
                continue
            seen.add(key)
            current_list.append(value)
            field_src_arr.append({
                "value": value,
                "source": "attachment",
                "attachment_id": attach_id_str,
                "filename": filename,
                "confidence": conf,
                "set_at": now,
            })
            _record(out.applied_fields, fname, provenance)
            added_any = True

        if added_any:
            setattr(conversation, fname, current_list)
            sources[fname] = field_src_arr

    # Write JSONB columns back. SQLAlchemy doesn't track in-place mutation of
    # JSONB by default, so reassign.
    conversation.field_sources = sources
    conversation.field_candidates = candidates

    return out


__all__ = [
    "ApplyOutcome",
    "apply_extracted_fields",
    "AUTO_APPLY_THRESHOLD",
    "CANDIDATE_THRESHOLD",
]
