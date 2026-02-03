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
from models.components.tbl_comp_pages import CompPage
from models.tbl_user_tags import UserTag

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.pages.get")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class PageListQuery(BaseModel):
    search_term: Optional[str] = Field(default=None, description="Search over name/description")
    sort_key: Optional[str] = Field(default="created_at", description="Sort field")
    sort_order: Optional[str] = Field(default="desc", description='"asc" or "desc"')
    current_page: Optional[int] = Field(default=1, ge=1, description="1-based page")
    limit: Optional[int] = Field(default=10, ge=1, le=100, description="page size (<=100)")
    tags: Optional[str] = Field(default=None, description="Comma-separated tag names to filter by (ANY match)")
    id: Optional[str] = Field(default=None, description="Filter by specific page id (UUID)")
    group_id: Optional[str] = Field(default=None, description="Filter by group_id (UUID)")
    group_type: Optional[str] = Field(default=None, description="Filter by group_type")
    sub_type: Optional[str] = Field(default=None, description="Filter by sub_type")

    allowed_sort_keys: ClassVar[Set[str]] = {
        "id",
        "name",
        "created_at",
        "updated_at",
        "group_id",
        "group_type",
        "sub_type",
    }

    @field_validator("id", "group_id")
    @classmethod
    def validate_uuid_str(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        s = str(v).strip()
        if not s:
            return None
        UUID(s)
        return s

    @field_validator("group_type", "sub_type", mode="before")
    @classmethod
    def normalize_optional_str(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("sort_order")
    @classmethod
    def normalize_sort_order(cls, v: Optional[str]) -> str:
        s = (v or "desc").lower().strip()
        return "asc" if s == "asc" else "desc"

    @field_validator("sort_key")
    @classmethod
    def whitelist_sort_key(cls, v: Optional[str]) -> str:
        s = (v or "created_at").lower().strip()
        return s if s in cls.allowed_sort_keys else "created_at"


def apply_search(q, term: Optional[str]):
    if not term:
        return q
    like = f"%{term.strip()}%"
    return q.filter(or_(CompPage.name.ilike(like), CompPage.description.ilike(like)))


def apply_sort(q, sort_key: str, sort_order: str):
    if sort_key == "name":
        col_expr = func.lower(CompPage.name)
    else:
        col_expr = getattr(CompPage, sort_key, CompPage.created_at)
    return q.order_by(asc(col_expr) if sort_order == "asc" else desc(col_expr))


def paginate(q, page: int, limit: int) -> Tuple[List[CompPage], int]:
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


def serialize_page(row: CompPage, gcs) -> Dict[str, Any]:
    images = as_list(getattr(row, "images", []))
    signed_images = [sign_url_maybe(gcs, img) for img in images]
    gid = getattr(row, "group_id", None)

    return {
        "id": str(getattr(row, "id", "")),
        "name": getattr(row, "name", None),
        "description": getattr(row, "description", None),
        "group_id": str(gid) if gid else None,
        "group_type": getattr(row, "group_type", None),
        "sub_type": getattr(row, "sub_type", None),
        "created_at": iso(getattr(row, "created_at", None)),
        "updated_at": iso(getattr(row, "updated_at", None)),
        "created_by": getattr(row, "created_by", None),
        "thumbnail": sign_url_maybe(gcs, getattr(row, "thumbnail", None)),
        "images": signed_images,
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


class PageGetCommand(BaseCommand):
    name = "components/pages/get"
    schema = PageListQuery
    require_auth = True
    method = "GET"
    group = "Page"

    def execute(self, payload: PageListQuery, user_id: str) -> Dict[str, Any]:
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token (missing user_id)")

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

        try:
            uid_int = int(user_id)
        except Exception:
            uid_int = None

        with SessionLocal() as session:
            try:
                q = session.query(CompPage).filter(CompPage.created_by == uid_int)

                if payload.id:
                    q = q.filter(CompPage.id == payload.id)

                q = apply_search(q, payload.search_term)

                tags_list = [t.strip() for t in (payload.tags or "").split(",") if t.strip()]
                if tags_list:
                    typed_array = array(tags_list, type_=ARRAY(TEXT()))
                    q = q.filter(CompPage.tags.op("&&")(typed_array))

                if payload.group_id:
                    q = q.filter(CompPage.group_id == payload.group_id)
                if payload.group_type:
                    q = q.filter(CompPage.group_type == payload.group_type)
                if payload.sub_type:
                    q = q.filter(CompPage.sub_type == payload.sub_type)

                q = apply_sort(q, payload.sort_key, payload.sort_order)
                items, total = paginate(q, current_page, limit)
                resp["total_items"] = total

                gcs = get_gcs()
                resp["data"] = [serialize_page(row, gcs) for row in items]

                if uid_int is not None:
                    user_tags = (
                        session.query(UserTag)
                        .filter(UserTag.user_id == uid_int)
                        .order_by(func.lower(UserTag.name).asc())
                        .all()
                    )
                    resp["tagging"]["tags"] = [serialize_tag(t) for t in user_tags]
                else:
                    resp["tagging"]["tags"] = []

                return resp

            except HTTPException:
                raise
            except Exception as e:
                logger.exception("[pages/get] error: %s", e)
                resp["errors"].append(str(e))
                resp["error_code"] = "UNEXPECTED"
                return resp

    def run(self, payload: PageListQuery, user_id: str = None):
        return self.execute(payload, user_id=user_id or "")
