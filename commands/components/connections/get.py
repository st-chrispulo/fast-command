# commands/components/connections/get.py
from typing import Optional, Any, Dict, List, Tuple, ClassVar, Set

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, asc, desc, func

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from models.components.tbl_comp_connections import CompConnection


class ConnectionsListQuery(BaseModel):
    # Required parent scope
    funnel_id: str = Field(..., description="Funnel ID (uuid) to scope connections")

    # Optional filters
    id: Optional[str] = Field(default=None, description="Filter by specific connection id")

    from_component_id: Optional[str] = Field(default=None, description="Filter by from component id")
    to_component_id: Optional[str] = Field(default=None, description="Filter by to component id")

    # NEW filters
    from_node_id: Optional[str] = Field(default=None, description="Filter by from node id (text)")
    to_node_id: Optional[str] = Field(default=None, description="Filter by to node id (text)")

    # Search over port ids (optional) + (optional) node ids
    search_term: Optional[str] = Field(default=None, description="Search over ports/node ids")

    sort_key: Optional[str] = Field(default="created_at", description="Sort field")
    sort_order: Optional[str] = Field(default="desc", description='"asc" or "desc"')

    current_page: Optional[int] = Field(default=1, ge=1, description="1-based page")
    limit: Optional[int] = Field(default=50, ge=1, le=200, description="page size (<=200)")

    ALLOWED_SORT_KEYS: ClassVar[Set[str]] = {
        "id",
        "created_at",
        "updated_at",
        "from_port_id",
        "to_port_id",
        # NEW
        "from_node_id",
        "to_node_id",
    }

    @field_validator("funnel_id")
    @classmethod
    def _funnel_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("funnel_id is required")
        return v

    @field_validator("sort_order")
    @classmethod
    def _norm_order(cls, v: Optional[str]) -> str:
        v = (v or "desc").lower()
        return "asc" if v == "asc" else "desc"

    @field_validator("sort_key")
    @classmethod
    def _whitelist_sort(cls, v: Optional[str]) -> str:
        v = (v or "created_at").lower()
        return v if v in cls.ALLOWED_SORT_KEYS else "created_at"

    @field_validator("from_node_id", "to_node_id", mode="before")
    @classmethod
    def _trim_node_ids(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None


def _apply_search(q, term: Optional[str]):
    if not term:
        return q
    like = f"%{term.strip()}%"
    return q.filter(
        or_(
            CompConnection.from_port_id.ilike(like),
            CompConnection.to_port_id.ilike(like),
            # NEW: search node ids too
            CompConnection.from_node_id.ilike(like),
            CompConnection.to_node_id.ilike(like),
        )
    )


def _apply_sort(q, sort_key: str, sort_order: str):
    col_expr = getattr(CompConnection, sort_key, CompConnection.created_at)

    # normalize text fields for sorting stability
    if sort_key in ("from_port_id", "to_port_id", "from_node_id", "to_node_id"):
        col_expr = func.lower(col_expr)

    return q.order_by(asc(col_expr) if sort_order == "asc" else desc(col_expr))


def _paginate(q, page: int, limit: int) -> Tuple[List[CompConnection], int]:
    total = q.order_by(None).count()
    items = q.offset((page - 1) * limit).limit(limit).all()
    return items, total


def _iso(dt):
    try:
        return dt.isoformat()
    except Exception:
        return dt


def _serialize_connection(row: CompConnection) -> Dict[str, Any]:
    return {
        "id": str(getattr(row, "id", "")),
        "funnel_id": str(getattr(row, "funnel_id", "")),

        # NEW
        "from_node_id": getattr(row, "from_node_id", None),
        "to_node_id": getattr(row, "to_node_id", None),

        "from_component_id": str(getattr(row, "from_component_id", "")),
        "from_port_id": getattr(row, "from_port_id", None),

        "to_component_id": str(getattr(row, "to_component_id", "")),
        "to_port_id": getattr(row, "to_port_id", None),

        "metadata": getattr(row, "metadata_json", None) or {},

        "created_at": _iso(getattr(row, "created_at", None)),
        "updated_at": _iso(getattr(row, "updated_at", None)),
        "created_by": getattr(row, "created_by", None),
        "updated_by": getattr(row, "updated_by", None),
    }


class ConnectionsGetCommand(BaseCommand):
    """
    GET /components/connections/get
    - Scoped by funnel_id (required)
    """
    name = "components/connections/get"
    schema = ConnectionsListQuery
    require_auth = True
    method = "GET"
    group = "Funnel"

    def execute(self, payload: ConnectionsListQuery, user_id: str):
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token (missing user_id)")

        current_page = payload.current_page or 1
        limit = payload.limit or 50

        resp: Dict[str, Any] = {
            "data": [],
            "total_items": 0,
            "current_page": current_page,
            "limit": limit,
            "errors": [],
            "error_code": None,
        }

        with SessionLocal() as session:
            try:
                # Scope to user + funnel
                q = (
                    session.query(CompConnection)
                    .filter(CompConnection.created_by == user_id)
                    .filter(CompConnection.funnel_id == payload.funnel_id)
                )

                # Optional filters
                if payload.id:
                    q = q.filter(CompConnection.id == payload.id)

                if payload.from_component_id:
                    q = q.filter(CompConnection.from_component_id == payload.from_component_id)
                if payload.to_component_id:
                    q = q.filter(CompConnection.to_component_id == payload.to_component_id)

                # NEW optional filters
                if payload.from_node_id:
                    q = q.filter(CompConnection.from_node_id == payload.from_node_id)
                if payload.to_node_id:
                    q = q.filter(CompConnection.to_node_id == payload.to_node_id)

                # Search + sort + page
                q = _apply_search(q, payload.search_term)
                q = _apply_sort(q, payload.sort_key, payload.sort_order)
                items, total = _paginate(q, current_page, limit)

                resp["total_items"] = total
                resp["data"] = [_serialize_connection(row) for row in items]
                return resp

            except HTTPException:
                raise
            except Exception as e:
                resp["errors"].append(str(e))
                resp["error_code"] = "UNEXPECTED"
                return resp

    def run(self, payload: ConnectionsListQuery, user_id: str = None):
        return self.execute(payload, user_id=user_id)


__all__ = ["ConnectionsGetCommand"]
