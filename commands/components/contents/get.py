# commands/components/content/get.py
from typing import Optional, Any, Dict, List, Tuple, ClassVar, Set

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, asc, desc
from sqlalchemy.orm import Session

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_contents import CompContent


class ContentListQuery(BaseModel):
    search_term: Optional[str] = Field(default=None, description="Search over name/description")
    sort_key: Optional[str]   = Field(default="created_at", description="Sort field")
    sort_order: Optional[str] = Field(default="desc", description='"asc" or "desc"')
    current_page: Optional[int] = Field(default=1, ge=1, description="1-based page")
    limit: Optional[int]        = Field(default=10, ge=1, le=100, description="page size (<=100)")

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
    return q.filter(or_(CompContent.name.ilike(like),
                        CompContent.description.ilike(like)))


def _apply_sort(q, sort_key: str, sort_order: str):
    col = getattr(CompContent, sort_key, CompContent.created_at)
    return q.order_by(asc(col) if sort_order == "asc" else desc(col))


def _paginate(q, page: int, limit: int) -> Tuple[List[CompContent], int]:
    total = q.count()
    items = q.offset((page - 1) * limit).limit(limit).all()
    return items, total


def _serialize(row: CompContent, gcs) -> Dict[str, Any]:
    data = {
        "id": str(getattr(row, "id", "")),
        "name": getattr(row, "name", None),
        "description": getattr(row, "description", None),
        "created_at": getattr(row, "created_at", None),
        "updated_at": getattr(row, "updated_at", None),
        "created_by": getattr(row, "created_by", None),
        "thumbnail": getattr(row, "thumbnail", None),
        "images": list(getattr(row, "images", []) or []),
        "file": getattr(row, "file_link", None),  # map to "file" in output
    }

    # Sign if present using GCS helper; on failure, keep original value.
    if data["thumbnail"]:
        try:
            data["thumbnail"] = gcs.signed_get_url(data["thumbnail"], expires_seconds=3600)
        except Exception:
            pass

    if data["file"]:
        try:
            data["file"] = gcs.signed_get_url(data["file"], expires_seconds=3600)
        except Exception:
            pass

    if data["images"]:
        signed_images: List[str] = []
        for img in data["images"]:
            if not img:
                signed_images.append(img)
                continue
            try:
                signed_images.append(gcs.signed_get_url(img, expires_seconds=3600))
            except Exception:
                signed_images.append(img)
        data["images"] = signed_images

    return data


class ContentGetCommand(BaseCommand):
    """
    GET /components/content/get
    - Schema comes from query params (Depends())
    - user_id injected by router: execute(payload, user_id=...)
    """
    name = "components/content/get"
    schema = ContentListQuery
    require_auth = True
    method = "GET"
    group = "Content"

    def execute(self, payload: ContentListQuery, user_id: str):
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
        }

        session: Optional[Session] = None
        try:
            session = SessionLocal()

            q = session.query(CompContent).filter(CompContent.created_by == user_id)
            q = _apply_search(q, payload.search_term)
            q = _apply_sort(q, payload.sort_key, payload.sort_order)

            items, total = _paginate(q, current_page, limit)
            resp["total_items"] = total

            gcs = get_gcs()
            resp["data"] = [_serialize(row, gcs) for row in items]
            return resp

        except HTTPException:
            raise
        except Exception as e:
            resp["errors"].append(str(e))
            resp["error_code"] = "UNEXPECTED"
            return resp
        finally:
            if session:
                session.close()

    # Optional: backward-compat for callers using `.run(...)`
    def run(self, payload: ContentListQuery, user_id: str = None):
        return self.execute(payload, user_id=user_id)
