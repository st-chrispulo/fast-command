from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Optional, Set, Tuple

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import asc, desc, func, or_
from sqlalchemy.dialects.postgresql import ARRAY, TEXT, array

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.gcs.gcs import get_gcs
from models.tbl_funnels import Funnel
from models.tbl_user_tags import UserTag

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.funnels.get")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class FunnelListQuery(BaseModel):
    search_term: Optional[str] = Field(default=None, description="Search over name/description")
    sort_key: Optional[str] = Field(default="created_at", description="Sort field")
    sort_order: Optional[str] = Field(default="desc", description='"asc" or "desc"')
    current_page: int = Field(default=1, ge=1, description="1-based page")
    limit: int = Field(default=10, ge=1, le=100, description="Page size (<=100)")
    tags: Optional[str] = Field(default=None, description="Comma-separated tag names to filter by (ANY match)")

    ALLOWED_SORT_KEYS: ClassVar[Set[str]] = {"id", "name", "created_at", "updated_at"}

    @field_validator("sort_order")
    @classmethod
    def validate_sort_order(cls, v: Optional[str]) -> str:
        v = (v or "desc").strip().lower()
        return "asc" if v == "asc" else "desc"

    @field_validator("sort_key")
    @classmethod
    def validate_sort_key(cls, v: Optional[str]) -> str:
        v = (v or "created_at").strip().lower()
        return v if v in cls.ALLOWED_SORT_KEYS else "created_at"

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, v: Any) -> Any:
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


def _sign_url_maybe(gcs: Any, key: Optional[str]) -> Optional[str]:
    if not key:
        return key
    try:
        return gcs.signed_get_url(key, expires_seconds=3600)
    except Exception:
        return key


def _serialize_funnel(row: Funnel, gcs: Any) -> Dict[str, Any]:
    images = _as_list(getattr(row, "images", []))
    return {
        "id": str(getattr(row, "id", "")),
        "name": getattr(row, "name", None),
        "description": getattr(row, "description", None),
        "created_at": _iso(getattr(row, "created_at", None)),
        "updated_at": _iso(getattr(row, "updated_at", None)),
        "created_by": getattr(row, "created_by", None),
        "thumbnail": _sign_url_maybe(gcs, getattr(row, "thumbnail", None)),
        "images": [_sign_url_maybe(gcs, k) for k in images],
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


def _parse_tags_csv(tags_csv: Optional[str]) -> List[str]:
    if not tags_csv:
        return []
    return [t.strip() for t in tags_csv.split(",") if t.strip()]


def _apply_search(q, term: Optional[str]):
    if not term:
        return q
    like = f"%{term.strip()}%"
    return q.filter(or_(Funnel.name.ilike(like), Funnel.description.ilike(like)))


def _apply_tags(q, tags_list: List[str]):
    if not tags_list:
        return q
    typed_array = array(tags_list, type_=ARRAY(TEXT()))
    return q.filter(Funnel.tags.op("&&")(typed_array))


def _apply_sort(q, sort_key: str, sort_order: str):
    col = func.lower(Funnel.name) if sort_key == "name" else getattr(Funnel, sort_key, Funnel.created_at)
    return q.order_by(asc(col) if sort_order == "asc" else desc(col))


def _paginate(q, page: int, limit: int) -> Tuple[List[Funnel], int]:
    total = q.order_by(None).count()
    items = q.offset((page - 1) * limit).limit(limit).all()
    return items, total


def _user_id_int(user_id: str) -> Optional[int]:
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


class FunnelGetCommand(BaseCommand):
    name = "components/funnels/get"
    schema = FunnelListQuery
    require_auth = True
    method = "GET"
    group = "Funnel"

    def execute(self, payload: FunnelListQuery, user_id: Optional[str]):
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token (missing user_id)")

        resp: Dict[str, Any] = {
            "data": [],
            "total_items": 0,
            "current_page": payload.current_page,
            "limit": payload.limit,
            "errors": [],
            "error_code": None,
            "tagging": {"tags": []},
        }

        with SessionLocal() as session:
            try:
                q = session.query(Funnel).filter(Funnel.created_by == user_id)
                q = _apply_search(q, payload.search_term)
                q = _apply_tags(q, _parse_tags_csv(payload.tags))
                q = _apply_sort(q, payload.sort_key or "created_at", payload.sort_order or "desc")

                items, total = _paginate(q, payload.current_page, payload.limit)
                resp["total_items"] = total

                gcs = get_gcs()
                resp["data"] = [_serialize_funnel(row, gcs) for row in items]

                uid = _user_id_int(user_id)
                if uid is not None:
                    user_tags = (
                        session.query(UserTag)
                        .filter(UserTag.user_id == uid)
                        .order_by(func.lower(UserTag.name).asc())
                        .all()
                    )
                    resp["tagging"]["tags"] = [_serialize_tag(t) for t in user_tags]

                return resp
            except HTTPException:
                raise
            except Exception as e:
                logger.exception("[funnels] get error")
                resp["errors"].append(str(e))
                resp["error_code"] = "UNEXPECTED"
                return resp

    def run(self, payload: FunnelListQuery, user_id: Optional[str] = None):
        return self.execute(payload, user_id=user_id)
