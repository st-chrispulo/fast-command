from __future__ import annotations

from typing import Optional, Any, Dict, List, Tuple, ClassVar, Set

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, asc, desc, func
from sqlalchemy.dialects.postgresql import array, ARRAY, TEXT

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_contents import CompContent
from models.tbl_user_tags import UserTag

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.contents.get")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class ContentListQuery(BaseModel):
    search_term: Optional[str] = Field(default=None, description="Search over name/description")
    sort_key: Optional[str] = Field(default="created_at", description="Sort field")
    sort_order: Optional[str] = Field(default="asc", description='"asc" or "desc"')
    current_page: Optional[int] = Field(default=1, ge=1, description="1-based page")
    limit: Optional[int] = Field(default=10, ge=1, le=100, description="page size (<=100)")
    tags: Optional[str] = Field(default=None, description="Comma-separated tag names to filter by (ANY match)")
    id: Optional[str] = Field(default=None, description="Filter by specific content id (UUID)")
    group_id: Optional[str] = Field(default=None, description="Filter by group_id (UUID)")
    sub_type: Optional[str] = Field(default=None, description="Filter by sub_type")

    ALLOWED_SORT_KEYS: ClassVar[Set[str]] = {"id", "name", "created_at", "updated_at", "group_id", "sub_type"}

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        s = str(v).strip()
        if not s:
            return None
        import uuid

        uuid.UUID(s)
        return s

    @field_validator("group_id")
    @classmethod
    def validate_group_id(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        s = str(v).strip()
        if not s:
            return None
        import uuid

        uuid.UUID(s)
        return s

    @field_validator("sub_type", mode="before")
    @classmethod
    def normalize_sub_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("sort_order")
    @classmethod
    def normalize_sort_order(cls, v: Optional[str]) -> str:
        s = (v or "desc").strip().lower()
        return "asc" if s == "asc" else "desc"

    @field_validator("sort_key")
    @classmethod
    def whitelist_sort_key(cls, v: Optional[str]) -> str:
        s = (v or "created_at").strip().lower()
        return s if s in cls.ALLOWED_SORT_KEYS else "created_at"


def _apply_search(q, term: Optional[str]):
    if not term:
        return q
    like = f"%{term.strip()}%"
    return q.filter(or_(CompContent.name.ilike(like), CompContent.description.ilike(like)))


def _apply_tags_any(q, raw_tags: Optional[str]):
    tags_list = [t.strip() for t in (raw_tags or "").split(",") if t.strip()]
    if not tags_list:
        return q
    typed_array = array(tags_list, type_=ARRAY(TEXT()))
    return q.filter(CompContent.tags.op("&&")(typed_array))


def _apply_filters(q, payload: ContentListQuery):
    if payload.id:
        q = q.filter(CompContent.id == payload.id)
    if payload.group_id:
        q = q.filter(CompContent.group_id == payload.group_id)
    if payload.sub_type:
        q = q.filter(CompContent.sub_type == payload.sub_type)
    return q


def _apply_sort(q, sort_key: str, sort_order: str):
    col_expr = func.lower(CompContent.name) if sort_key == "name" else getattr(CompContent, sort_key, CompContent.created_at)
    return q.order_by(asc(col_expr) if sort_order == "asc" else desc(col_expr))


def _paginate(q, page: int, limit: int) -> Tuple[List[CompContent], int]:
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


def _sign_url_maybe(gcs, url: Optional[str]) -> Optional[str]:
    if not url:
        return url
    try:
        return gcs.signed_get_url(url, expires_seconds=3600)
    except Exception:
        return url


def _serialize_content(row: CompContent, gcs) -> Dict[str, Any]:
    images = _as_list(getattr(row, "images", []))
    group_id_val = getattr(row, "group_id", None)

    return {
        "id": str(getattr(row, "id", "")),
        "name": getattr(row, "name", None),
        "description": getattr(row, "description", None),
        "group_id": str(group_id_val) if group_id_val else None,
        "sub_type": getattr(row, "sub_type", None),
        "created_at": _iso(getattr(row, "created_at", None)),
        "updated_at": _iso(getattr(row, "updated_at", None)),
        "created_by": getattr(row, "created_by", None),
        "thumbnail": _sign_url_maybe(gcs, getattr(row, "thumbnail", None)),
        "images": [_sign_url_maybe(gcs, img) for img in images],
        "file": _sign_url_maybe(gcs, getattr(row, "file_link", None)),
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


def _load_user_tags(session, user_id: str) -> List[Dict[str, Any]]:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return []

    tag_q = session.query(UserTag).filter(UserTag.user_id == uid).order_by(func.lower(UserTag.name).asc())
    return [_serialize_tag(t) for t in tag_q.all()]


class ContentGetCommand(BaseCommand):
    name = "components/contents/get"
    schema = ContentListQuery
    require_auth = True
    method = "GET"
    group = "Content"

    def execute(self, payload: ContentListQuery, user_id: str):
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token (missing user_id)")

        start_t = time.monotonic() if "time" in globals() else None
        logger.info("[components.contents.get] start user_id=%s page=%s limit=%s", user_id, payload.current_page, payload.limit)

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

                q = session.query(CompContent).filter(CompContent.created_by == user_id)
                q = _apply_filters(q, payload)
                q = _apply_search(q, payload.search_term)
                q = _apply_tags_any(q, payload.tags)
                q = _apply_sort(q, payload.sort_key or "created_at", payload.sort_order or "desc")

                items, total = _paginate(q, current_page, limit)
                resp["total_items"] = total
                resp["data"] = [_serialize_content(row, gcs) for row in items]
                resp["tagging"]["tags"] = _load_user_tags(session, user_id)

                if start_t is not None:
                    import time as _time

                    logger.info(
                        "[components.contents.get] ok items=%d total=%d elapsed=%.3fs",
                        len(items),
                        total,
                        _time.monotonic() - start_t,
                    )

                return resp

            except HTTPException:
                raise
            except Exception as e:
                logger.exception("[components.contents.get] error=%s", e)
                resp["errors"].append(str(e))
                resp["error_code"] = "UNEXPECTED"
                return resp

    def run(self, payload: ContentListQuery, user_id: str = None):
        return self.execute(payload, user_id=user_id)
