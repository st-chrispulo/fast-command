# commands/components/authentications/get.py
from typing import Optional, Any, Dict, List, Tuple, ClassVar, Set

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, asc, desc, func
from sqlalchemy.dialects.postgresql import array, ARRAY, TEXT  # IMPORTANT
from uuid import UUID as PyUUID

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_authentications import CompAuthentication
from models.tbl_user_tags import UserTag


class AuthenticationListQuery(BaseModel):
    search_term: Optional[str] = Field(default=None, description="Search over name/description")
    sort_key: Optional[str] = Field(default="created_at", description="Sort field")
    sort_order: Optional[str] = Field(default="desc", description='"asc" or "desc"')
    current_page: Optional[int] = Field(default=1, ge=1, description="1-based page")
    limit: Optional[int] = Field(default=10, ge=1, le=100, description="page size (<=100)")
    # Comma-separated tags to filter by (ANY match)
    tags: Optional[str] = Field(default=None, description="Comma-separated tag names to filter by (ANY match)")

    # NEW (optional filters; safe if client ignores)
    group_id: Optional[PyUUID] = Field(default=None, description="Filter by group_id")
    sub_type: Optional[str] = Field(default=None, description="Filter by sub_type (exact)")

    ALLOWED_SORT_KEYS: ClassVar[Set[str]] = {
        "id", "name", "created_at", "updated_at",
        "group_id", "sub_type",  # NEW (optional)
    }

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

    @field_validator("sub_type", mode="before")
    @classmethod
    def _sub_type_trim(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None


def _apply_search(q, term: Optional[str]):
    if not term:
        return q
    like = f"%{term.strip()}%"
    return q.filter(
        or_(
            CompAuthentication.name.ilike(like),
            CompAuthentication.description.ilike(like),
        )
    )


def _apply_sort(q, sort_key: str, sort_order: str):
    # case-insensitive sort for name/sub_type
    if sort_key == "name":
        col_expr = func.lower(CompAuthentication.name)
    elif sort_key == "sub_type":
        col_expr = func.lower(CompAuthentication.sub_type)
    else:
        col_expr = getattr(CompAuthentication, sort_key, CompAuthentication.created_at)

    return q.order_by(asc(col_expr) if sort_order == "asc" else desc(col_expr))


def _paginate(q, page: int, limit: int) -> Tuple[List[CompAuthentication], int]:
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


def _sign_key_maybe(gcs, key: Optional[str]) -> Optional[str]:
    if not key:
        return key
    try:
        return gcs.signed_get_url(key, expires_seconds=3600)
    except Exception:
        # fall back to returning the original key if signing fails
        return key


def _serialize_auth(row: CompAuthentication, gcs) -> Dict[str, Any]:
    images = _as_list(getattr(row, "images", []))
    signed_images = [_sign_key_maybe(gcs, img) for img in images]

    file_key = getattr(row, "file_link", None)

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

        "template_id": str(getattr(row, "template_id")) if getattr(row, "template_id", None) else None,

        # NEW model fields
        "group_id": str(getattr(row, "group_id")) if getattr(row, "group_id", None) else None,
        "sub_type": getattr(row, "sub_type", None),

        # CHANGED: single file_link (signed)
        "file_link": _sign_key_maybe(gcs, file_key),

        "tags": _as_list(getattr(row, "tags", [])),

        # expose metadata_json as "metadata"
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


class AuthenticationGetCommand(BaseCommand):
    """
    GET /components/authentications/get
    - Schema comes from query params (Depends())
    - user_id injected by router: execute(payload, user_id=...)
    """
    name = "components/authentications/get"
    schema = AuthenticationListQuery
    require_auth = True
    method = "GET"
    group = "Authentication"

    def execute(self, payload: AuthenticationListQuery, user_id: str):
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

                # Search
                q = _apply_search(q, payload.search_term)

                # Optional filters
                if payload.group_id:
                    q = q.filter(CompAuthentication.group_id == payload.group_id)
                if payload.sub_type:
                    q = q.filter(func.lower(CompAuthentication.sub_type) == payload.sub_type.lower())

                # Tags filter (ANY overlap) with proper Postgres text[] typing
                raw_tags = payload.tags or ""
                tags_list = [t.strip() for t in raw_tags.split(",") if t.strip()]
                if tags_list:
                    typed_array = array(tags_list, type_=ARRAY(TEXT()))  # text[] literal
                    q = q.filter(CompAuthentication.tags.op("&&")(typed_array))

                # Sort and page
                q = _apply_sort(q, payload.sort_key, payload.sort_order)
                items, total = _paginate(q, current_page, limit)
                resp["total_items"] = total

                # Serialize
                gcs = get_gcs()
                resp["data"] = [_serialize_auth(row, gcs) for row in items]

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

    def run(self, payload: AuthenticationListQuery, user_id: str = None):
        return self.execute(payload, user_id=user_id)
