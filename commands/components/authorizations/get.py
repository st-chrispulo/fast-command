from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Optional, Set, Tuple
from uuid import UUID as PyUUID

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import asc, desc, func, or_
from sqlalchemy.dialects.postgresql import ARRAY, TEXT, array

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_authentications import CompAuthentication
from models.tbl_user_tags import UserTag


class AuthenticationListQuery(BaseModel):
    """Query params for listing component authentications."""

    search_term: Optional[str] = Field(default=None, description="Search over name/description")
    sort_key: Optional[str] = Field(default="created_at", description="Sort field")
    sort_order: Optional[str] = Field(default="desc", description='"asc" or "desc"')
    current_page: Optional[int] = Field(default=1, ge=1, description="1-based page")
    limit: Optional[int] = Field(default=10, ge=1, le=100, description="page size (<=100)")
    tags: Optional[str] = Field(default=None, description="Comma-separated tag names to filter by (ANY match)")
    group_id: Optional[PyUUID] = Field(default=None, description="Filter by group_id")
    sub_type: Optional[str] = Field(default=None, description="Filter by sub_type (exact)")

    ALLOWED_SORT_KEYS: ClassVar[Set[str]] = {
        "id",
        "name",
        "created_at",
        "updated_at",
        "group_id",
        "sub_type",
    }

    @field_validator("sort_order")
    @classmethod
    def normalize_sort_order(cls, v: Optional[str]) -> str:
        v = (v or "desc").strip().lower()
        return "asc" if v == "asc" else "desc"

    @field_validator("sort_key")
    @classmethod
    def whitelist_sort_key(cls, v: Optional[str]) -> str:
        v = (v or "created_at").strip().lower()
        return v if v in cls.ALLOWED_SORT_KEYS else "created_at"

    @field_validator("sub_type", mode="before")
    @classmethod
    def trim_sub_type(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None


def _iso(dt: Any) -> Any:
    try:
        return dt.isoformat()
    except Exception:
        return dt


def _as_list(val: Any) -> List[Any]:
    return list(val or [])


def _parse_tags(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _sign_key_maybe(gcs: Any, key: Optional[str]) -> Optional[str]:
    if not key:
        return key
    try:
        return gcs.signed_get_url(key, expires_seconds=3600)
    except Exception:
        return key


def _apply_search(q: Any, term: Optional[str]) -> Any:
    if not term:
        return q
    like = f"%{term.strip()}%"
    return q.filter(or_(CompAuthentication.name.ilike(like), CompAuthentication.description.ilike(like)))


def _apply_filters(q: Any, payload: AuthenticationListQuery) -> Any:
    if payload.group_id:
        q = q.filter(CompAuthentication.group_id == payload.group_id)
    if payload.sub_type:
        q = q.filter(func.lower(CompAuthentication.sub_type) == payload.sub_type.lower())

    tags_list = _parse_tags(payload.tags)
    if tags_list:
        typed_array = array(tags_list, type_=ARRAY(TEXT()))
        q = q.filter(CompAuthentication.tags.op("&&")(typed_array))

    return q


def _sort_expr(sort_key: str) -> Any:
    if sort_key == "name":
        return func.lower(CompAuthentication.name)
    if sort_key == "sub_type":
        return func.lower(CompAuthentication.sub_type)
    return getattr(CompAuthentication, sort_key, CompAuthentication.created_at)


def _apply_sort(q: Any, sort_key: str, sort_order: str) -> Any:
    expr = _sort_expr(sort_key)
    return q.order_by(asc(expr) if sort_order == "asc" else desc(expr))


def _paginate(q: Any, page: int, limit: int) -> Tuple[List[CompAuthentication], int]:
    total = q.order_by(None).count()
    items = q.offset((page - 1) * limit).limit(limit).all()
    return items, total


def _serialize_auth(row: CompAuthentication, gcs: Any) -> Dict[str, Any]:
    images = _as_list(getattr(row, "images", []))
    signed_images = [_sign_key_maybe(gcs, img) for img in images]
    file_key = getattr(row, "file_link", None)

    template_id = getattr(row, "template_id", None)
    group_id = getattr(row, "group_id", None)

    return {
        "id": str(getattr(row, "id", "")),
        "name": getattr(row, "name", None),
        "description": getattr(row, "description", None),
        "created_at": _iso(getattr(row, "created_at", None)),
        "updated_at": _iso(getattr(row, "updated_at", None)),
        "created_by": getattr(row, "created_by", None),
        "updated_by": getattr(row, "updated_by", None),
        "thumbnail": _sign_key_maybe(gcs, getattr(row, "thumbnail", None)),
        "images": signed_images,
        "template_id": str(template_id) if template_id else None,
        "group_id": str(group_id) if group_id else None,
        "sub_type": getattr(row, "sub_type", None),
        "file_link": _sign_key_maybe(gcs, file_key),
        "tags": _as_list(getattr(row, "tags", [])),
        "metadata": getattr(row, "metadata_json", None) or {},
    }


def _serialize_tag(t: UserTag) -> Dict[str, Any]:
    return {
        "name": t.name,
        "color_hex": t.color_hex,
        "description": t.description,
        "is_active": bool(t.is_active),
        "created_at": _iso(t.created_at),
        "updated_at": _iso(t.updated_at),
    }


def _safe_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None


class AuthenticationGetCommand(BaseCommand):
    """List component authentications for the current user."""

    name = "components/authentications/get"
    schema = AuthenticationListQuery
    require_auth = True
    method = "GET"
    group = "Authentication"

    def execute(self, payload: AuthenticationListQuery, user_id: str) -> Dict[str, Any]:
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

        with SessionLocal() as session:
            try:
                q = session.query(CompAuthentication).filter(CompAuthentication.created_by == user_id)
                q = _apply_search(q, payload.search_term)
                q = _apply_filters(q, payload)
                q = _apply_sort(q, payload.sort_key or "created_at", payload.sort_order or "desc")

                items, total = _paginate(q, current_page, limit)
                resp["total_items"] = total

                gcs = get_gcs()
                resp["data"] = [_serialize_auth(row, gcs) for row in items]

                uid = _safe_int(user_id)
                if uid is not None:
                    tags = (
                        session.query(UserTag)
                        .filter(UserTag.user_id == uid)
                        .order_by(func.lower(UserTag.name).asc())
                        .all()
                    )
                    resp["tagging"]["tags"] = [_serialize_tag(t) for t in tags]

                return resp
            except HTTPException:
                raise
            except Exception as e:
                resp["errors"].append(str(e))
                resp["error_code"] = "UNEXPECTED"
                return resp

    def run(self, payload: AuthenticationListQuery, user_id: Optional[str] = None) -> Dict[str, Any]:
        return self.execute(payload, user_id=user_id or "")
