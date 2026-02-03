from __future__ import annotations

import json
import time
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from models.components.tbl_comp_connections import CompConnection

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.connections.create")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


def _as_int_user_id(v: Optional[str]) -> Optional[int]:
    if not v:
        return None
    try:
        iv = int(v)
        return iv if iv > 0 else None
    except Exception:
        return None


def _normalize_metadata(v: Any) -> Optional[dict]:
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


def _extract_node_id(md: Optional[dict], key: str) -> Optional[str]:
    if not md:
        return None

    v = md.get(key)
    if v is None and key == "fromNodeId":
        v = md.get("from_node_id") or md.get("from_node")
    if v is None and key == "toNodeId":
        v = md.get("to_node_id") or md.get("to_node")

    if v is None:
        return None

    s = str(v).strip()
    return s or None


def _comp_connection_to_dict(m: CompConnection) -> dict:
    return {
        "id": str(m.id) if getattr(m, "id", None) is not None else None,
        "funnel_id": str(m.funnel_id) if getattr(m, "funnel_id", None) is not None else None,
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


class CreateCompConnectionsPayload(BaseModel):
    """Create a connection record for a funnel."""

    funnel_id: UUID = Field(description="Target funnel id")

    from_component_id: UUID = Field(description="Source component id")
    from_port_id: str = Field(description="Source port id")

    to_component_id: UUID = Field(description="Target component id")
    to_port_id: str = Field(description="Target port id")

    metadata: Optional[Any] = Field(default=None, description="JSON object or JSON string")

    @field_validator("from_port_id", "to_port_id")
    @classmethod
    def validate_port_id(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("port_id is required")
        if len(s) > 255:
            raise ValueError("port_id must be <= 255 characters")
        return s

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, v: Any) -> Optional[dict]:
        return _normalize_metadata(v)


class CreateCompConnectionsCommand(BaseCommand):
    """Create a single connection record for a funnel."""

    name = "components/connections/create"
    schema = CreateCompConnectionsPayload
    require_auth = True
    method = "post"
    type = "json"
    group = "Funnel"

    async def execute(self, payload: CreateCompConnectionsPayload, user_id: Optional[str] = None) -> dict:
        t0 = time.monotonic()

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        uid_int = _as_int_user_id(user_id)
        md = payload.metadata or None
        from_node_id = _extract_node_id(md, "fromNodeId")
        to_node_id = _extract_node_id(md, "toNodeId")

        logger.info(
            "[connections] create start funnel_id=%s user_id=%s",
            str(payload.funnel_id),
            user_id,
        )

        db = SessionLocal()
        try:
            row = CompConnection(
                funnel_id=payload.funnel_id,
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
            logger.exception("[connections] create failed")
            raise
        finally:
            db.close()


__all__ = ["CreateCompConnectionsCommand"]
