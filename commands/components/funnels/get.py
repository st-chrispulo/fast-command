from typing import Optional, Any, Dict, List, Tuple, ClassVar, Set

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, asc, desc, func
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import array, ARRAY, TEXT

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs
from models.tbl_funnels import Funnel
from models.tbl_user_tags import UserTag


class FunnelListQuery(BaseModel):
    search_term: Optional[str] = Field(default=None, description="Search over name/description")
    sort_key: Optional[str]    = Field(default="created_at", description="Sort field")
    sort_order: Optional[str]  = Field(default="desc", description='"asc" or "desc"')
    current_page: Optional[int] = Field(default=1, ge=1, description="1-based page")
    limit: Optional[int]         = Field(default=10, ge=1, le=100, description="page size (<=100)")
    tags: Optional[str] = Field(default=None, description="Comma-separated tag names to filter by (ANY match)")

    ALLOWED_SORT_KEYS: ClassVar[Set[str]] = {"id", "name", "created_at", "updated_at"}

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


def _apply_search(q, term: Optional[str]):
    if not term:
        return q
    like = f"%{term.strip()}%"
    return q.filter(
        or_(
            Funnel.name.ilike(like),
            Funnel.description.ilike(like),
        )
    )


def _apply_sort(q, sort_key: str, sort_order: str):
    col_expr = func.lower(Funnel.name) if sort_key == "name" else getattr(Funnel, sort_key, Funnel.created_at)
    return q.order_by(asc(col_expr) if sort_order == "asc" else desc(col_expr))


def _paginate(q, page: int, limit: int) -> Tuple[List[Funnel], int]:
    total = q.order_by(None).count()
    items = q.offset((page - 1) * limit).limit(limit).all()
    return items, total


def _iso(dt):
    try:
        return dt.isoformat()
    except Exception:
        return dt


def _as_list(val) -> List[Any]:
    return list(val or [])


def _sign_url_maybe(gcs, url: Optional[str]) -> Optional[str]:
    if not url:
        return url
    try:
        return gcs.signed_get_url(url, expires_seconds=3600)
    except Exception:
        return url


def _serialize_funnel(row: Funnel, gcs) -> Dict[str, Any]:
    images = _as_list(getattr(row, "images", []))
    signed_images = [_sign_url_maybe(gcs, img) for img in images]
    return {
        "id": str(getattr(row, "id", "")),
        "name": getattr(row, "name", None),
        "description": getattr(row, "description", None),
        "created_at": _iso(getattr(row, "created_at", None)),
        "updated_at": _iso(getattr(row, "updated_at", None)),
        "created_by": getattr(row, "created_by", None),
        "thumbnail": _sign_url_maybe(gcs, getattr(row, "thumbnail", None)),
        "images": signed_images,
        "file": _sign_url_maybe(gcs, getattr(row, "file_link", None)),
        "tags": _as_list(getattr(row, "tags", [])),
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


class FunnelGetCommand(BaseCommand):
    """
    GET /components/funnels/get
    """
    name = "components/funnels/get"
    schema = FunnelListQuery
    require_auth = True
    method = "GET"
    group = "Funnel"

    def execute(self, payload: FunnelListQuery, user_id: str):
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
                q = session.query(Funnel).filter(Funnel.created_by == user_id)

                q = _apply_search(q, payload.search_term)

                raw_tags = payload.tags or ""
                tags_list = [t.strip() for t in raw_tags.split(",") if t.strip()]
                if tags_list:
                    typed_array = array(tags_list, type_=ARRAY(TEXT()))
                    q = q.filter(Funnel.tags.op("&&")(typed_array))
                    # or: q = q.filter(Funnel.tags.overlap(typed_array))

                q = _apply_sort(q, payload.sort_key, payload.sort_order)
                items, total = _paginate(q, current_page, limit)
                resp["total_items"] = total

                gcs = get_gcs()
                resp["data"] = [_serialize_funnel(row, gcs) for row in items]

                try:
                    uid = int(user_id)
                except (TypeError, ValueError):
                    uid = None

                if uid is not None:
                    tag_q = (
                        session.query(UserTag)
                        .filter(UserTag.user_id == uid)
                        .order_by(func.lower(UserTag.name).asc())
                    )
                    user_tags = tag_q.all()
                    resp["tagging"]["tags"] = [_serialize_tag(t) for t in user_tags]
                else:
                    resp["tagging"]["tags"] = []

                return resp

            except HTTPException:
                raise
            except Exception as e:
                resp["errors"].append(str(e))
                resp["error_code"] = "UNEXPECTED"
                return resp

    def run(self, payload: FunnelListQuery, user_id: str = None):
        return self.execute(payload, user_id=user_id)
