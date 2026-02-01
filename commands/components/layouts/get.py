# commands/components/layouts/get.py

from typing import Optional, Any, Dict, List, Tuple, ClassVar, Set
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, asc, desc, func
from sqlalchemy.dialects.postgresql import array, ARRAY, TEXT

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_layouts import CompLayout
from models.tbl_user_tags import UserTag


# -----------------------------
# Query schema
# -----------------------------
class LayoutListQuery(BaseModel):
    id: Optional[str] = Field(default=None, description="Exact layout id (UUID). If provided, returns only that record.")
    search_term: Optional[str] = Field(default=None, description="Search over name/description")
    sort_key: Optional[str] = Field(default="created_at", description="Sort field")
    sort_order: Optional[str] = Field(default="desc", description='"asc" or "desc"')
    current_page: Optional[int] = Field(default=1, ge=1, description="1-based page")
    limit: Optional[int] = Field(default=10, ge=1, le=100, description="page size (<=100)")
    tags: Optional[str] = Field(default=None, description="Comma-separated tag names to filter by (ANY match)")

    # ✅ NEW: filters
    group_id: Optional[str] = Field(default=None, description="Filter by group_id (UUID)")
    group_type: Optional[str] = Field(default=None, description="Filter by group_type")
    sub_type: Optional[str] = Field(default=None, description="Filter by sub_type")

    ALLOWED_SORT_KEYS: ClassVar[Set[str]] = {
        "id",
        "name",
        "created_at",
        "updated_at",
        # ✅ NEW
        "group_id",
        "group_type",
        "sub_type",
    }

    @field_validator("id")
    @classmethod
    def _norm_id(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v = v.strip()
        UUID(v)  # validate UUID format
        return v

    @field_validator("group_id")
    @classmethod
    def _norm_group_id(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v = v.strip()
        UUID(v)  # validate UUID format
        return v

    @field_validator("group_type", mode="before")
    @classmethod
    def _norm_group_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("sub_type", mode="before")
    @classmethod
    def _norm_sub_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

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


# -----------------------------
# Helpers
# -----------------------------
def _apply_search(q, term: Optional[str]):
    if not term:
        return q
    like = f"%{term.strip()}%"
    return q.filter(
        or_(
            CompLayout.name.ilike(like),
            CompLayout.description.ilike(like),
        )
    )


def _apply_sort(q, sort_key: str, sort_order: str):
    col_expr = func.lower(CompLayout.name) if sort_key == "name" else getattr(CompLayout, sort_key, CompLayout.created_at)
    return q.order_by(asc(col_expr) if sort_order == "asc" else desc(col_expr))


def _paginate(q, page: int, limit: int) -> Tuple[List[CompLayout], int]:
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


def _serialize_layout(row: CompLayout, gcs) -> Dict[str, Any]:
    images = _as_list(getattr(row, "images", []))
    signed_images = [_sign_url_maybe(gcs, img) for img in images]

    group_id_val = getattr(row, "group_id", None)

    return {
        "id": str(getattr(row, "id", "")),
        "name": getattr(row, "name", None),
        "description": getattr(row, "description", None),

        # ✅ NEW
        "group_id": str(group_id_val) if group_id_val else None,
        "group_type": getattr(row, "group_type", None),
        "sub_type": getattr(row, "sub_type", None),

        "created_at": _iso(getattr(row, "created_at", None)),
        "updated_at": _iso(getattr(row, "updated_at", None)),
        "created_by": getattr(row, "created_by", None),
        "thumbnail": _sign_url_maybe(gcs, getattr(row, "thumbnail", None)),
        "images": signed_images,
        "file": _sign_url_maybe(gcs, getattr(row, "file_link", None)),  # file_link -> file
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


# -----------------------------
# Command
# -----------------------------
class LayoutGetCommand(BaseCommand):
    """
    GET /components/layouts/get

    Supports:
      - id=<uuid> (returns only that record, or empty list)
      - group_id=<uuid>
      - group_type=<string>
      - sub_type=<string>
      - tags overlap, search, sort, pagination
    """

    name = "components/layouts/get"
    schema = LayoutListQuery
    require_auth = True
    method = "GET"
    group = "Layout"

    def execute(self, payload: LayoutListQuery, user_id: str):
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
                gcs = get_gcs()

                # ✅ If id is provided, return ONLY that record (respecting group_id/group_type/sub_type too)
                if payload.id:
                    filters = [
                        CompLayout.created_by == user_id,
                        CompLayout.id == payload.id,
                    ]
                    if payload.group_id:
                        filters.append(CompLayout.group_id == payload.group_id)
                    if payload.group_type:
                        filters.append(CompLayout.group_type == payload.group_type)
                    if payload.sub_type:
                        filters.append(CompLayout.sub_type == payload.sub_type)

                    row = session.query(CompLayout).filter(*filters).one_or_none()

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
                        "data": [_serialize_layout(row, gcs)],
                        "total_items": 1,
                        "current_page": 1,
                        "limit": 1,
                        "errors": [],
                        "error_code": None,
                        "tagging": {"tags": []},
                    }

                # -----------------------------
                # list behavior
                # -----------------------------
                q = session.query(CompLayout).filter(CompLayout.created_by == user_id)

                # search
                q = _apply_search(q, payload.search_term)

                # tags overlap
                raw_tags = payload.tags or ""
                tags_list = [t.strip() for t in raw_tags.split(",") if t.strip()]
                if tags_list:
                    typed_array = array(tags_list, type_=ARRAY(TEXT()))
                    q = q.filter(CompLayout.tags.op("&&")(typed_array))

                # ✅ NEW: group_id/group_type/sub_type filters
                if payload.group_id:
                    q = q.filter(CompLayout.group_id == payload.group_id)
                if payload.group_type:
                    q = q.filter(CompLayout.group_type == payload.group_type)
                if payload.sub_type:
                    q = q.filter(CompLayout.sub_type == payload.sub_type)

                # sort + paginate
                q = _apply_sort(q, payload.sort_key, payload.sort_order)
                items, total = _paginate(q, current_page, limit)

                resp["total_items"] = total
                resp["data"] = [_serialize_layout(row, gcs) for row in items]

                # Tagging block (per-user tags)
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

    def run(self, payload: LayoutListQuery, user_id: str = None):
        return self.execute(payload, user_id=user_id)
