from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, func

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from integrations.gcs.gcs import get_gcs
from models.components.tbl_comp_connections import CompConnection
from models.components.tbl_comp_contents import CompContent
from models.components.tbl_comp_layouts import CompLayout
from models.components.tbl_comp_pages import CompPage

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.connections.preview")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


PREVIEW_ROOT_ID = "00000000-0000-0000-0000-000000000001"
BUCKET_NAME = "solitud-upload-bucket"


class ConnectionsPreviewQuery(BaseModel):
    """Preview connections by enriching components and producing transformed layout code."""

    funnel_id: str = Field(..., description="Funnel ID (uuid)")

    @field_validator("funnel_id")
    @classmethod
    def validate_funnel_id(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("funnel_id is required")
        return s


def _safe(v: Any) -> str:
    return str(v) if v is not None else ""


def _iso(dt: Any) -> Any:
    try:
        return dt.isoformat()
    except Exception:
        return dt


def _get_metadata(row: Any) -> Dict[str, Any]:
    md = getattr(row, "metadata_json", None)
    if md is None:
        md = getattr(row, "metadata", None)
    return md or {}


def _serialize_connection(row: CompConnection) -> Dict[str, Any]:
    return {
        "id": _safe(getattr(row, "id", "")),
        "funnel_id": _safe(getattr(row, "funnel_id", "")),
        "from_component_id": _safe(getattr(row, "from_component_id", "")),
        "from_port_id": getattr(row, "from_port_id", None),
        "to_component_id": _safe(getattr(row, "to_component_id", "")),
        "to_port_id": getattr(row, "to_port_id", None),
        "metadata": _get_metadata(row),
        "created_at": _iso(getattr(row, "created_at", None)),
        "updated_at": _iso(getattr(row, "updated_at", None)),
        "created_by": getattr(row, "created_by", None),
        "updated_by": getattr(row, "updated_by", None),
    }


_COMPONENT_SOURCES: List[Tuple[str, Any]] = [
    ("contents", CompContent),
    ("layouts", CompLayout),
    ("pages", CompPage),
]


def _bulk_fetch_components(session, ids: Set[str]) -> Dict[str, Dict[str, Any]]:
    """
    Fetch component metadata across component tables.

    Args:
        session: SQLAlchemy session.
        ids: Component ids to fetch.

    Returns:
        Map of component_id -> metadata dict.
    """
    out: Dict[str, Dict[str, Any]] = {}
    remaining = set(ids)

    for component_type, model in _COMPONENT_SOURCES:
        if not remaining:
            break

        rows = session.query(model.id, model.name, model.file_link).filter(model.id.in_(list(remaining))).all()
        for rid, name, file_link in rows:
            cid = _safe(rid)
            if not cid or cid in out:
                continue

            out[cid] = {
                "id": cid,
                "component_type": component_type,
                "name": (name or "").strip(),
                "file_link": (file_link or "").strip() if file_link else None,
            }
            remaining.discard(cid)

    return out


def _object_key_from_db_value(file_link: Optional[str]) -> Optional[str]:
    """
    Normalize various GCS representations into an object key.

    Args:
        file_link: DB file link value.

    Returns:
        Object key (no leading slash) or None.
    """
    if not file_link:
        return None

    s = str(file_link).strip()
    if not s:
        return None

    if s.startswith(f"{BUCKET_NAME}/"):
        s = s[len(BUCKET_NAME) + 1 :].lstrip("/")

    if s == BUCKET_NAME:
        return None

    if s.startswith("gs://"):
        rest = s[5:]
        parts = rest.split("/", 1)
        if len(parts) != 2:
            return None
        obj = (parts[1] or "").lstrip("/")
        if not obj:
            return None
        if obj.startswith(f"{BUCKET_NAME}/"):
            obj = obj[len(BUCKET_NAME) + 1 :].lstrip("/")
        return obj or None

    if s.startswith("http://") or s.startswith("https://"):
        u = urlparse(s)
        host = (u.netloc or "").lower()
        path = (u.path or "").lstrip("/")

        if "storage.googleapis.com" in host:
            if path.startswith(f"{BUCKET_NAME}/"):
                return path[len(BUCKET_NAME) + 1 :].lstrip("/")
            parts = path.split("/", 1)
            return parts[1].lstrip("/") if len(parts) == 2 else None

        if host.startswith(f"{BUCKET_NAME}."):
            return path.lstrip("/") or None

        return path.lstrip("/") or None

    return s.lstrip("/") or None


def _split_dir_and_name(obj_key: str) -> Tuple[str, str]:
    s = (obj_key or "").strip().strip("/")
    if not s:
        return "", ""
    if "/" not in s:
        return "", s
    d, n = s.rsplit("/", 1)
    return d, n


def _signed_get_url(obj_key: str) -> str:
    gcs = get_gcs()
    try:
        return gcs.signed_get_url(obj_key, expires_seconds=3600)
    except Exception:
        return obj_key


def _fetch_text_signed(file_link: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    """
    Fetch file contents using a signed URL derived from the stored file_link.

    Args:
        file_link: DB file link.

    Returns:
        Tuple of (text, info).
    """
    obj_key = _object_key_from_db_value(file_link)
    if not obj_key:
        raise ValueError(f"Missing/invalid file_link: {file_link}")

    d, n = _split_dir_and_name(obj_key)
    signed_url = _signed_get_url(obj_key)
    if not signed_url:
        raise ValueError("Unable to sign file_link")

    r = requests.get(signed_url, timeout=30)
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text, {"dir": d, "name": n, "url": signed_url}


def _to_placeholder_key(to_port_id: Optional[str]) -> Optional[str]:
    s = (to_port_id or "").strip()
    if not s:
        return None
    return s[3:].strip() or None if s.lower().startswith("in-") else None


def _component_ident(filename: str) -> str:
    base = re.sub(r"\.(jsx|js|tsx|ts)$", "", (filename or ""), flags=re.I).strip()
    if not base:
        return "UnknownComponent"

    parts = [p for p in re.split(r"[^a-zA-Z0-9]+", base) if p]
    if not parts:
        return "UnknownComponent"

    ident = "".join(p[:1].upper() + p[1:] for p in parts)
    if not re.match(r"^[A-Za-z_]", ident):
        ident = f"Comp{ident}"
    return ident


def _import_path(component_type: str, filename: str) -> str:
    return f"./components/{component_type}/{filename}"


def _insert_imports(code: str, imports: List[str]) -> str:
    if not code or not imports:
        return code

    existing = set(re.findall(r"^\s*import .*?;\s*$", code, flags=re.M))
    new_imports = [i.strip() for i in imports if i and i.strip() and i.strip() not in existing]
    if not new_imports:
        return code

    lines = code.splitlines(True)
    idx = 0
    for i, l in enumerate(lines):
        if l.strip().startswith("import "):
            idx = i + 1

    block = "\n".join(new_imports) + "\n"
    if idx > 0 and lines[idx - 1].strip() != "":
        block = "\n" + block

    lines.insert(idx, block)
    return "".join(lines)


def _replace_placeholder(code: str, key: str, replacement: str) -> Tuple[str, bool]:
    if not code or not key:
        return code, False
    pat = re.compile(rf"<\s*placeholder-{re.escape(key)}\s*/\s*>", re.IGNORECASE)
    new_code, n = pat.subn(replacement, code)
    return new_code, n > 0


def _is_eligible(session, funnel_id: str) -> bool:
    cnt = (
        session.query(func.count(CompConnection.id))
        .filter(CompConnection.funnel_id == funnel_id)
        .filter(CompConnection.to_component_id == PREVIEW_ROOT_ID)
        .scalar()
        or 0
    )
    return cnt > 0


def _collect_component_ids(rows: List[CompConnection]) -> Set[str]:
    ids: Set[str] = {PREVIEW_ROOT_ID}
    for r in rows:
        ids.add(_safe(getattr(r, "from_component_id", "")))
        ids.add(_safe(getattr(r, "to_component_id", "")))
    return ids


def _enrich_connections(
    rows: List[CompConnection],
    comp_map: Dict[str, Dict[str, Any]],
    resp: Dict[str, Any],
) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []

    for r in rows:
        c = _serialize_connection(r)

        from_comp = comp_map.get(c["from_component_id"])
        to_comp = comp_map.get(c["to_component_id"])

        c["from_component"] = {k: v for k, v in (from_comp or {}).items() if k != "file_link"} if from_comp else None
        c["to_component"] = {k: v for k, v in (to_comp or {}).items() if k != "file_link"} if to_comp else None

        c["from_file"] = None
        c["to_file"] = None
        c["file_raw"] = None
        c["file_transformed"] = None

        try:
            if from_comp and from_comp.get("file_link"):
                text, info = _fetch_text_signed(from_comp.get("file_link"))
                c["from_file"] = {"dir": info.get("dir"), "name": info.get("name"), "file": text}
        except Exception as e:
            resp["warnings"].append(
                f"Failed to fetch from_component code for {(from_comp or {}).get('name')} ({c['from_component_id']}): {e}"
            )

        try:
            if to_comp and to_comp.get("file_link"):
                text, info = _fetch_text_signed(to_comp.get("file_link"))
                c["to_file"] = {"dir": info.get("dir"), "name": info.get("name"), "file": text}
        except Exception as e:
            resp["warnings"].append(
                f"Failed to fetch to_component code for {(to_comp or {}).get('name')} ({c['to_component_id']}): {e}"
            )

        enriched.append(c)

    return enriched


def _build_incoming(enriched: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    incoming: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in enriched:
        incoming[c["to_component_id"]].append(c)
    return incoming


def _transform_layouts(
    incoming: Dict[str, List[Dict[str, Any]]],
    comp_map: Dict[str, Dict[str, Any]],
    resp: Dict[str, Any],
) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}

    for parent_id, edges in incoming.items():
        parent = comp_map.get(parent_id)
        if not parent or parent.get("component_type") != "layouts":
            continue

        try:
            raw, _info = _fetch_text_signed(parent.get("file_link"))
        except Exception as e:
            resp["warnings"].append(f"Failed to fetch layout code for {parent.get('name')} ({parent_id}): {e}")
            continue

        updated = raw
        imports: List[str] = []

        for e in edges:
            child = comp_map.get(e.get("from_component_id", "")) or {}
            child_type = (child.get("component_type") or "").strip()
            child_name = (child.get("name") or "").strip()
            key = _to_placeholder_key(e.get("to_port_id"))

            if not child_type or not child_name or not key:
                continue

            ident = _component_ident(child_name)
            imports.append(f'import {ident} from "{_import_path(child_type, child_name)}";')
            updated, _ = _replace_placeholder(updated, key, f"<{ident}/>")

        updated = _insert_imports(updated, imports)
        out[parent_id] = {"raw": raw, "transformed": updated}

    return out


class CompConnectionPreview(BaseCommand):
    """Preview connections with component metadata and transformed layout code."""

    name = "components/connections/preview"
    schema = ConnectionsPreviewQuery
    method = "GET"
    require_auth = True
    group = "Funnel"

    def execute(self, payload: ConnectionsPreviewQuery, user_id: str):
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token (missing user_id)")

        resp: Dict[str, Any] = {
            "eligible": False,
            "data": [],
            "total_items": 0,
            "warnings": [],
            "errors": [],
            "error_code": None,
        }

        with SessionLocal() as session:
            try:
                if not _is_eligible(session, payload.funnel_id):
                    return resp

                resp["eligible"] = True

                rows: List[CompConnection] = (
                    session.query(CompConnection)
                    .filter(CompConnection.funnel_id == payload.funnel_id)
                    .order_by(desc(getattr(CompConnection, "created_at", func.now())))
                    .all()
                )
                if not rows:
                    return resp

                comp_ids = _collect_component_ids(rows)
                comp_map = _bulk_fetch_components(session, comp_ids)

                enriched = _enrich_connections(rows, comp_map, resp)
                incoming = _build_incoming(enriched)
                parent_transform = _transform_layouts(incoming, comp_map, resp)

                for c in enriched:
                    parent_id = c["to_component_id"]
                    t = parent_transform.get(parent_id)
                    if t:
                        c["file_raw"] = t["raw"]
                        c["file_transformed"] = t["transformed"]

                resp["data"] = enriched
                resp["total_items"] = len(enriched)
                return resp
            except HTTPException:
                raise
            except Exception as e:
                logger.exception("[connections] preview failed")
                resp["errors"].append(str(e))
                resp["error_code"] = "UNEXPECTED"
                return resp

    def run(self, payload: ConnectionsPreviewQuery, user_id: str = None):
        return self.execute(payload, user_id=user_id)


__all__ = ["CompConnectionPreview"]
