from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Optional, Set, Tuple
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import asc, desc, func, or_
from sqlalchemy.dialects.postgresql import ARRAY, TEXT, array

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_layouts import CompLayout
from models.tbl_user_tags import UserTag

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.layouts.get")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class LayoutListQuery(BaseModel):
    """Query layouts with optional filters, search, tags overlap, sorting, and pagination."""

    id: Optional[str] = Field(default=None, description="Exact layout id (UUID)")
    search_term: Optional[str] = Field(default=None, description="Search over name/description")
    sort_key: Optional[str] = Field(default="created_at", description="Sort field")
    sort_order: Optional[str] = Field(default="desc", description='"asc" or "desc"')
    current_page: Optional[int] = Field(default=1, ge=1, description="1-based page")
    limit: Optional[int] = Field(default=10, ge=1, le=100, description="Page size (<=100)")
    tags: Optional[str] = Field(default=None, description="Comma-separated tags to filter by (ANY match)")

    group_id: Optional[str] = Field(default=None, description="Filter by group_id (UUID)")
    sub_type: Optional[str] = Field(default=None, description="Filter by sub_type")

    allowed_sort_keys: ClassVar[Set[str]] = {
        "id",
        "name",
        "created_at",
        "updated_at",
        "group_id",
        "sub_type",
    }

    @field_validator("id")
    @classmethod
    def norm_id(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        s = v.strip()
        UUID(s)
        return s

    @field_validator("group_id")
    @classmethod
    def norm_group_id(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        s = v.strip()
        UUID(s)
        return s

    @field_validator("sub_type", mode="before")
    @classmethod
    def norm_sub_type(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("sort_order")
    @classmethod
    def norm_order(cls, v: Optional[str]) -> str:
        s = (v or "desc").lower()
        return "asc" if s == "asc" else "desc"

    @field_validator("sort_key")
    @classmethod
    def whitelist_sort(cls, v: Optional[str]) -> str:
        s = (v or "created_at").lower()
        return s if s in cls.allowed_sort_keys else "created_at"


def parse_tags_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [t.strip() for t in value.split(",") if t.strip()]


def apply_search(q, term: Optional[str]):
    if not term:
        return q
    like = f"%{term.strip()}%"
    return q.filter(or_(CompLayout.name.ilike(like), CompLayout.description.ilike(like)))


def apply_sort(q, sort_key: str, sort_order: str):
    col = func.lower(CompLayout.name) if sort_key == "name" else getattr(CompLayout, sort_key, CompLayout.created_at)
    return q.order_by(asc(col) if sort_order == "asc" else desc(col))


def paginate(q, page: int, limit: int) -> Tuple[List[CompLayout], int]:
    total = q.order_by(None).count()
    items = q.offset((page - 1) * limit).limit(limit).all()
    return items, total


def iso(dt: Any) -> Any:
    try:
        return dt.isoformat()
    except Exception:
        return dt


def as_list(val: Any) -> List[Any]:
    return list(val or [])


def sign_url_maybe(gcs, key: Optional[str]) -> Optional[str]:
    if not key:
        return key
    try:
        return gcs.signed_get_url(key, expires_seconds=3600)
    except Exception:
        return key


def serialize_layout(row: CompLayout, gcs) -> Dict[str, Any]:
    images = as_list(getattr(row, "images", []))
    group_id_val = getattr(row, "group_id", None)

    return {
        "id": str(getattr(row, "id", "")),
        "name": getattr(row, "name", None),
        "description": getattr(row, "description", None),
        "group_id": str(group_id_val) if group_id_val else None,
        "sub_type": getattr(row, "sub_type", None),
        "created_at": iso(getattr(row, "created_at", None)),
        "updated_at": iso(getattr(row, "updated_at", None)),
        "created_by": getattr(row, "created_by", None),
        "thumbnail": sign_url_maybe(gcs, getattr(row, "thumbnail", None)),
        "images": [sign_url_maybe(gcs, k) for k in images],
        "file": sign_url_maybe(gcs, getattr(row, "file_link", None)),
        "tags": as_list(getattr(row, "tags", [])),
        "metadata": getattr(row, "metadata_json", None) or {},
    }


def serialize_tag(t: UserTag) -> Dict[str, Any]:
    return {
        "name": t.name,
        "color_hex": t.color_hex,
        "description": t.description,
        "is_active": bool(t.is_active),
        "created_at": iso(t.created_at),
        "updated_at": iso(t.updated_at),
    }


class LayoutGetCommand(BaseCommand):
    """List layouts with filters, search, tags overlap, sorting, pagination, and user tags."""

    name = "components/layouts/get"
    schema = LayoutListQuery
    require_auth = True
    method = "GET"
    group = "Layout"

    def execute(self, payload: LayoutListQuery, user_id: str) -> Dict[str, Any]:
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token (missing user_id)")

        try:
            uid_int = int(user_id)
        except Exception:
            uid_int = None

        current_page = payload.current_page or 1
        limit = payload.limit or 10

        resp: Dict[str, Any] = {
            "data": [],
            "total_items": 0,
            "current_page": current_page,
            "limit": limit,
            "errors": [],
            "error_code": None,
            "tagging": {"tags": []},
        }

        with SessionLocal() as session:
            try:
                gcs = get_gcs()

                if payload.id:
                    q = session.query(CompLayout).filter(CompLayout.created_by == user_id, CompLayout.id == payload.id)
                    if payload.group_id:
                        q = q.filter(CompLayout.group_id == payload.group_id)
                    if payload.sub_type:
                        q = q.filter(CompLayout.sub_type == payload.sub_type)

                    row = q.one_or_none()
                    if not row:
                        return {
                            "data": [],
                            "total_items": 0,
                            "current_page": 1,
                            "limit": 1,
                            "errors": [],
                            "error_code": None,
                            "tagging": {"tags": []},
                        }

                    return {
                        "data": [serialize_layout(row, gcs)],
                        "total_items": 1,
                        "current_page": 1,
                        "limit": 1,
                        "errors": [],
                        "error_code": None,
                        "tagging": {"tags": []},
                    }

                q = session.query(CompLayout).filter(CompLayout.created_by == user_id)

                q = apply_search(q, payload.search_term)

                tags_list = parse_tags_csv(payload.tags)
                if tags_list:
                    typed = array(tags_list, type_=ARRAY(TEXT()))
                    q = q.filter(CompLayout.tags.op("&&")(typed))

                if payload.group_id:
                    q = q.filter(CompLayout.group_id == payload.group_id)
                if payload.sub_type:
                    q = q.filter(CompLayout.sub_type == payload.sub_type)

                q = apply_sort(q, payload.sort_key or "created_at", payload.sort_order or "desc")
                items, total = paginate(q, current_page, limit)

                resp["total_items"] = total
                resp["data"] = [serialize_layout(r, gcs) for r in items]

                if uid_int is not None:
                    tags_q = (
                        session.query(UserTag)
                        .filter(UserTag.user_id == uid_int)
                        .order_by(func.lower(UserTag.name).asc())
                    )
                    resp["tagging"]["tags"] = [serialize_tag(t) for t in tags_q.all()]

                return resp
            except HTTPException:
                raise
            except Exception as e:
                logger.exception("[layouts/get] error")
                resp["errors"].append(str(e))
                resp["error_code"] = "UNEXPECTED"
                return resp

    def run(self, payload: LayoutListQuery, user_id: str = None):
        return self.execute(payload, user_id=user_id)
