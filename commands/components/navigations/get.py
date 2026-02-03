from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Optional, Set, Tuple

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import asc, desc, func, or_
from sqlalchemy.dialects.postgresql import ARRAY, TEXT, array
from sqlalchemy.orm import Session

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_navigations import CompNavigation
from models.tbl_user_tags import UserTag

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.navigations.get")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class NavigationListQuery(BaseModel):
    search_term: Optional[str] = Field(default=None, description="Search over name/description")
    sort_key: Optional[str] = Field(default="created_at", description="Sort field")
    sort_order: Optional[str] = Field(default="desc", description='"asc" or "desc"')
    current_page: Optional[int] = Field(default=1, ge=1, description="1-based page")
    limit: Optional[int] = Field(default=10, ge=1, le=100, description="page size (<=100)")
    tags: Optional[str] = Field(default=None, description="Comma-separated tag names to filter by (ANY match)")
    group_id: Optional[str] = Field(default=None, description="Filter by group_id (uuid)")
    sub_type: Optional[str] = Field(default=None, description="Filter by sub_type")

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
    def norm_order(cls, v: Optional[str]) -> str:
        v = (v or "desc").lower()
        return "asc" if v == "asc" else "desc"

    @field_validator("sort_key")
    @classmethod
    def whitelist_sort(cls, v: Optional[str]) -> str:
        v = (v or "created_at").lower()
        return v if v in cls.ALLOWED_SORT_KEYS else "created_at"

    @field_validator("group_id", mode="before")
    @classmethod
    def group_id_trim(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("sub_type", mode="before")
    @classmethod
    def sub_type_trim(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("tags", mode="before")
    @classmethod
    def tags_trim(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None


def _apply_search(q: Session, term: Optional[str]) -> Session:
    if not term:
        return q
    like = f"%{term.strip()}%"
    return q.filter(or_(CompNavigation.name.ilike(like), CompNavigation.description.ilike(like)))


def _apply_filters(q: Session, group_id: Optional[str], sub_type: Optional[str]) -> Session:
    if group_id:
        gid = str(group_id).strip()
        if not gid:
            raise HTTPException(status_code=400, detail="Invalid group_id (must be UUID)")
        q = q.filter(CompNavigation.group_id == gid)

    if sub_type:
        q = q.filter(CompNavigation.sub_type == sub_type)

    return q


def _apply_tags_any(q: Session, tags_csv: Optional[str]) -> Session:
    raw = (tags_csv or "").strip()
    if not raw:
        return q
    tags_list = [t.strip() for t in raw.split(",") if t.strip()]
    if not tags_list:
        return q
    typed_array = array(tags_list, type_=ARRAY(TEXT()))
    return q.filter(CompNavigation.tags.op("&&")(typed_array))


def _apply_sort(q: Session, sort_key: str, sort_order: str) -> Session:
    if sort_key == "name":
        col_expr = func.lower(CompNavigation.name)
    else:
        col_expr = getattr(CompNavigation, sort_key, CompNavigation.created_at)
    return q.order_by(asc(col_expr) if sort_order == "asc" else desc(col_expr))


def _paginate(q: Session, page: int, limit: int) -> Tuple[List[CompNavigation], int]:
    total = q.order_by(None).count()
    items = q.offset((page - 1) * limit).limit(limit).all()
    return items, total


def _iso(dt: Any) -> Any:
    try:
        return dt.isoformat()
    except Exception:
        return dt


def _as_list(val: Any) -> List[Any]:
    return list(val or [])


def _sign_key_maybe(gcs: Any, key: Optional[str]) -> Optional[str]:
    if not key:
        return key
    try:
        return gcs.signed_get_url(key, expires_seconds=3600)
    except Exception:
        return key


def _serialize_file_link(file_link: Optional[str], gcs: Any) -> Optional[Dict[str, Any]]:
    if not file_link:
        return None
    return {"key": file_link, "url": _sign_key_maybe(gcs, file_link)}


def _serialize_nav(row: CompNavigation, gcs: Any) -> Dict[str, Any]:
    images = _as_list(getattr(row, "images", []))
    return {
        "id": str(getattr(row, "id", "")),
        "name": getattr(row, "name", None),
        "description": getattr(row, "description", None),
        "created_at": _iso(getattr(row, "created_at", None)),
        "updated_at": _iso(getattr(row, "updated_at", None)),
        "created_by": getattr(row, "created_by", None),
        "updated_by": getattr(row, "updated_by", None),
        "thumbnail": _sign_key_maybe(gcs, getattr(row, "thumbnail", None)),
        "images": [_sign_key_maybe(gcs, img) for img in images],
        "template_id": str(getattr(row, "template_id")) if getattr(row, "template_id", None) else None,
        "file_link": _serialize_file_link(getattr(row, "file_link", None), gcs),
        "group_id": str(getattr(row, "group_id", None)) if getattr(row, "group_id", None) else None,
        "sub_type": getattr(row, "sub_type", None),
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


class NavigationGetCommand(BaseCommand):
    """List CompNavigation for the current user with filters and signed URLs."""

    name = "components/navigations/get"
    schema = NavigationListQuery
    require_auth = True
    method = "GET"
    group = "Navigation"

    def execute(self, payload: NavigationListQuery, user_id: str) -> Dict[str, Any]:
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
                q: Session = session.query(CompNavigation).filter(CompNavigation.created_by == user_id)

                q = _apply_search(q, payload.search_term)
                q = _apply_filters(q, payload.group_id, payload.sub_type)
                q = _apply_tags_any(q, payload.tags)
                q = _apply_sort(q, payload.sort_key or "created_at", payload.sort_order or "desc")

                items, total = _paginate(q, current_page, limit)
                resp["total_items"] = total

                gcs = get_gcs()
                resp["data"] = [_serialize_nav(row, gcs) for row in items]

                uid: Optional[int]
                try:
                    uid = int(user_id)
                except (TypeError, ValueError):
                    uid = None

                if uid is not None:
                    user_tags = (
                        session.query(UserTag)
                        .filter(UserTag.user_id == uid)
                        .order_by(func.lower(UserTag.name).asc())
                        .all()
                    )
                    resp["tagging"]["tags"] = [_serialize_tag(t) for t in user_tags]
                else:
                    resp["tagging"]["tags"] = []

                return resp
            except HTTPException:
                raise
            except Exception as e:
                logger.exception("[components/navigations/get] error: %s", e)
                resp["errors"].append(str(e))
                resp["error_code"] = "UNEXPECTED"
                return resp

    def run(self, payload: NavigationListQuery, user_id: str = None) -> Dict[str, Any]:
        return self.execute(payload, user_id=user_id or "")
