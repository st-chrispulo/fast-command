# commands/components/connections/create.py

import json
import time
from typing import Optional, Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, field_validator

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from models.components.tbl_comp_connections import CompConnection

# Prefer your app logger if available; fallback to stdlib
try:
    from logger import logger
except Exception:
    import logging as _logging

    logger = _logging.getLogger("connections_create")
    if not logger.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)


# ---------------- helpers ----------------

def _as_int_user_id(v) -> Optional[int]:
    if v is None:
        return None
    try:
        iv = int(v)
        return iv if iv > 0 else None
    except Exception:
        return None


def _normalize_metadata(v: Any) -> Optional[dict]:
    """
    Accept dict, None, or JSON string and normalize to dict/None.
    If JSON parses to non-object, wrap it: {"value": parsed}.
    """
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
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        except Exception as e:
            raise ValueError(f"metadata must be valid JSON if provided as string: {e}")
    # best-effort cast
    try:
        return dict(v)
    except Exception:
        raise ValueError("metadata must be a JSON object or JSON string")


def _extract_node_id(md: Optional[dict], key: str) -> Optional[str]:
    """
    Pull node id from metadata.{fromNodeId|toNodeId} (or snake_case fallback),
    normalize to trimmed string, return None if empty.
    """
    if not isinstance(md, dict) or not md:
        return None

    # preferred camelCase keys (your frontend)
    v = md.get(key)

    # safe fallbacks (optional)
    if v is None and key == "fromNodeId":
        v = md.get("from_node_id") or md.get("from_node")
    if v is None and key == "toNodeId":
        v = md.get("to_node_id") or md.get("to_node")

    if v is None:
        return None

    s = str(v).strip()
    return s or None


# ---------------- payload ----------------

class CreateCompConnectionsPayload(BaseModel):
    funnel_id: UUID

    from_component_id: UUID
    from_port_id: str

    to_component_id: UUID
    to_port_id: str

    # maps to CompConnection.metadata_json (db column name "metadata")
    metadata: Optional[Any] = None

    @field_validator("from_port_id", "to_port_id")
    @classmethod
    def _port_trim(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("port_id is required")
        if len(v) > 255:
            raise ValueError("port_id must be <= 255 characters")
        return v

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata_in(cls, v):
        return _normalize_metadata(v)


# ---------------- command ----------------

class CreateCompConnectionsCommand(BaseCommand):
    """
    Create a single connection record for a funnel.

    Endpoint: /components/connections/create
    Body (JSON):
      - funnel_id (uuid)
      - from_component_id (uuid)
      - from_port_id (string)
      - to_component_id (uuid)
      - to_port_id (string)
      - metadata (optional json OR json-string)
        - expects metadata.fromNodeId / metadata.toNodeId (optional)
    """
    name = "components/connections/create"
    schema = CreateCompConnectionsPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Funnel"

    async def execute(
        self,
        payload: CreateCompConnectionsPayload,
        user_id: Optional[str] = None,
    ):
        t0 = time.monotonic()
        logger.info(
            "[connections] create start funnel_id=%s user_id=%s",
            str(payload.funnel_id),
            user_id,
        )

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        uid_int = _as_int_user_id(user_id)

        # metadata is already normalized to dict/None by the schema validator
        md = payload.metadata or None
        from_node_id = _extract_node_id(md, "fromNodeId")
        to_node_id = _extract_node_id(md, "toNodeId")

        db = SessionLocal()
        try:
            row = CompConnection(
                funnel_id=payload.funnel_id,

                # NEW COLUMNS (text)
                from_node_id=from_node_id,
                to_node_id=to_node_id,

                from_component_id=payload.from_component_id,
                from_port_id=payload.from_port_id,

                to_component_id=payload.to_component_id,
                to_port_id=payload.to_port_id,

                metadata_json=md,

                created_by=uid_int,
                updated_by=uid_int,
            )

            db.add(row)
            db.commit()
            db.refresh(row)

            return {
                "status": "ok",
                "data": _comp_connection_to_dict(row),
                "perf_ms": round((time.monotonic() - t0) * 1000, 2),
            }
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("[connections] DB error")
            raise
        finally:
            db.close()


# ---------------- serializer ----------------

def _comp_connection_to_dict(m: CompConnection) -> dict:
    return {
        "id": str(m.id) if getattr(m, "id", None) is not None else None,
        "funnel_id": str(m.funnel_id) if getattr(m, "funnel_id", None) is not None else None,

        # NEW FIELDS
        "from_node_id": getattr(m, "from_node_id", None),
        "to_node_id": getattr(m, "to_node_id", None),

        "from_component_id": str(m.from_component_id) if getattr(m, "from_component_id", None) is not None else None,
        "from_port_id": getattr(m, "from_port_id", None),

        "to_component_id": str(m.to_component_id) if getattr(m, "to_component_id", None) is not None else None,
        "to_port_id": getattr(m, "to_port_id", None),

        "metadata": getattr(m, "metadata_json", None) or {},

        "created_by": getattr(m, "created_by", None),
        "updated_by": getattr(m, "updated_by", None),
        "created_at": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
        "updated_at": m.updated_at.isoformat() if getattr(m, "updated_at", None) else None,
    }


__all__ = ["CreateCompConnectionsCommand"]
