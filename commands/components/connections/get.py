from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Optional, Set, Tuple

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import asc, desc, func, or_

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.components.tbl_comp_connections import CompConnection

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.connections.get")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class ConnectionsListQuery(BaseModel):
    """List connections for a funnel with optional filtering, search, sorting, and pagination."""

    funnel_id: str = Field(..., description="Funnel ID (uuid) to scope connections")

    id: Optional[str] = Field(default=None, description="Filter by specific connection id")
    from_component_id: Optional[str] = Field(default=None, description="Filter by from component id")
    to_component_id: Optional[str] = Field(default=None, description="Filter by to component id")

    from_node_id: Optional[str] = Field(default=None, description="Filter by from node id (text)")
    to_node_id: Optional[str] = Field(default=None, description="Filter by to node id (text)")

    search_term: Optional[str] = Field(default=None, description="Search over ports/node ids")

    sort_key: str = Field(default="created_at", description="Sort field")
    sort_order: str = Field(default="desc", description='"asc" or "desc"')

    current_page: int = Field(default=1, ge=1, description="1-based page")
    limit: int = Field(default=50, ge=1, le=200, description="Page size (<=200)")

    ALLOWED_SORT_KEYS: ClassVar[Set[str]] = {
        "id",
        "created_at",
        "updated_at",
        "from_port_id",
        "to_port_id",
        "from_node_id",
        "to_node_id",
    }

    @field_validator("funnel_id")
    @classmethod
    def validate_funnel_id(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("funnel_id is required")
        return s

    @field_validator("sort_order")
    @classmethod
    def validate_sort_order(cls, v: Optional[str]) -> str:
        s = (v or "desc").strip().lower()
        return "asc" if s == "asc" else "desc"

    @field_validator("sort_key")
    @classmethod
    def validate_sort_key(cls, v: Optional[str]) -> str:
        s = (v or "created_at").strip().lower()
        return s if s in cls.ALLOWED_SORT_KEYS else "created_at"

    @field_validator(
        "id",
        "from_component_id",
        "to_component_id",
        "from_node_id",
        "to_node_id",
        "search_term",
        mode="before",
    )
    @classmethod
    def trim_optional_strings(cls, v: Any) -> Any:
        if v is None:
            return None
        s = str(v).strip()
        return s or None


def _serialize_connection(row: CompConnection) -> Dict[str, Any]:
    created_at = getattr(row, "created_at", None)
    updated_at = getattr(row, "updated_at", None)
    return {
        "id": str(getattr(row, "id", "")),
        "funnel_id": str(getattr(row, "funnel_id", "")),
        "from_node_id": getattr(row, "from_node_id", None),
        "to_node_id": getattr(row, "to_node_id", None),
        "from_component_id": str(getattr(row, "from_component_id", "")),
        "from_port_id": getattr(row, "from_port_id", None),
        "to_component_id": str(getattr(row, "to_component_id", "")),
        "to_port_id": getattr(row, "to_port_id", None),
        "metadata": getattr(row, "metadata_json", None) or {},
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "created_by": getattr(row, "created_by", None),
        "updated_by": getattr(row, "updated_by", None),
    }


def _apply_filters(q, payload: ConnectionsListQuery):
    if payload.id:
        q = q.filter(CompConnection.id == payload.id)
    if payload.from_component_id:
        q = q.filter(CompConnection.from_component_id == payload.from_component_id)
    if payload.to_component_id:
        q = q.filter(CompConnection.to_component_id == payload.to_component_id)
    if payload.from_node_id:
        q = q.filter(CompConnection.from_node_id == payload.from_node_id)
    if payload.to_node_id:
        q = q.filter(CompConnection.to_node_id == payload.to_node_id)
    return q


def _apply_search(q, term: Optional[str]):
    if not term:
        return q
    like = f"%{term}%"
    return q.filter(
        or_(
            CompConnection.from_port_id.ilike(like),
            CompConnection.to_port_id.ilike(like),
            CompConnection.from_node_id.ilike(like),
            CompConnection.to_node_id.ilike(like),
        )
    )


def _apply_sort(q, sort_key: str, sort_order: str):
    col = getattr(CompConnection, sort_key, CompConnection.created_at)
    if sort_key in ("from_port_id", "to_port_id", "from_node_id", "to_node_id"):
        col = func.lower(col)
    return q.order_by(asc(col) if sort_order == "asc" else desc(col))


def _paginate(q, page: int, limit: int) -> Tuple[List[CompConnection], int]:
    total = q.order_by(None).count()
    items = q.offset((page - 1) * limit).limit(limit).all()
    return items, total


class ConnectionsGetCommand(BaseCommand):
    """List connections scoped to a funnel."""

    name = "components/connections/get"
    schema = ConnectionsListQuery
    require_auth = True
    method = "GET"
    group = "Funnel"

    def execute(self, payload: ConnectionsListQuery, user_id: str):
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token (missing user_id)")

        resp: Dict[str, Any] = {
            "data": [],
            "total_items": 0,
            "current_page": payload.current_page,
            "limit": payload.limit,
            "errors": [],
            "error_code": None,
        }

        with SessionLocal() as session:
            try:
                q = (
                    session.query(CompConnection)
                    .filter(CompConnection.created_by == user_id)
                    .filter(CompConnection.funnel_id == payload.funnel_id)
                )

                q = _apply_filters(q, payload)
                q = _apply_search(q, payload.search_term)
                q = _apply_sort(q, payload.sort_key, payload.sort_order)

                items, total = _paginate(q, payload.current_page, payload.limit)

                resp["total_items"] = total
                resp["data"] = [_serialize_connection(r) for r in items]
                return resp
            except HTTPException:
                raise
            except Exception as e:
                logger.exception("[connections] get failed")
                resp["errors"].append(str(e))
                resp["error_code"] = "UNEXPECTED"
                return resp

    def run(self, payload: ConnectionsListQuery, user_id: str = None):
        return self.execute(payload, user_id=user_id)


__all__ = ["ConnectionsGetCommand"]
