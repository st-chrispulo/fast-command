# commands/components/connections/preview.py

from __future__ import annotations

import re
from typing import Optional, Any, Dict, List, Set, Tuple
from collections import defaultdict
from urllib.parse import urlparse

import requests
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, desc

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from models.components.tbl_comp_connections import CompConnection

# ✅ component tables
from models.components.tbl_comp_contents import CompContent
from models.components.tbl_comp_layouts import CompLayout
from models.components.tbl_comp_pages import CompPage

# ✅ gcs singleton (we only use it for signing)
from integrations.gcs.gcs import get_gcs


PREVIEW_ROOT_ID = "00000000-0000-0000-0000-000000000001"
BUCKET_NAME = "solitud-upload-bucket"


class ConnectionsPreviewQuery(BaseModel):
    funnel_id: str = Field(..., description="Funnel ID (uuid)")

    @field_validator("funnel_id")
    @classmethod
    def _required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("funnel_id is required")
        return v.strip()


def _safe(v) -> str:
    return str(v) if v is not None else ""


def _iso(dt):
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


_COMPONENT_SOURCES = [
    ("contents", CompContent),
    ("layouts", CompLayout),
    ("pages", CompPage),
]


def _bulk_fetch_components(session, ids: Set[str]) -> Dict[str, Dict[str, Any]]:
    """
    component_id -> { id, component_type, name, file_link }
    We keep file_link internally for signing/fetching, but we won't return it.
    """
    out: Dict[str, Dict[str, Any]] = {}
    remaining = set(ids)

    for component_type, Model in _COMPONENT_SOURCES:
        if not remaining:
            break

        rows = (
            session.query(Model.id, Model.name, Model.file_link)
            .filter(Model.id.in_(list(remaining)))
            .all()
        )

        for rid, name, file_link in rows:
            rid = _safe(rid)
            if not rid or rid in out:
                continue

            out[rid] = {
                "id": rid,
                "component_type": component_type,
                "name": (name or "").strip(),
                "file_link": (file_link or "").strip() if file_link else None,
            }
            remaining.discard(rid)

    return out


def _object_key_from_db_value(file_link: Optional[str]) -> Optional[str]:
    """
    DB file_link should ideally be an object key like:
      uploads/.../files/Foo.js

    This tolerates:
      - https://storage.googleapis.com/<bucket>/<object>
      - https://<bucket>.storage.googleapis.com/<object>
      - gs://<bucket>/<object>
      - "<bucket>/<object>"

    Returns:
      object key (no leading slash), or None
    """
    if not file_link:
        return None

    s = str(file_link).strip()
    if not s:
        return None

    # bucket/object stored
    if s.startswith(f"{BUCKET_NAME}/"):
        s = s[len(BUCKET_NAME) + 1 :].lstrip("/")

    # reject "bucket only"
    if s == BUCKET_NAME:
        return None

    # gs://bucket/object
    if s.startswith("gs://"):
        rest = s[5:]
        parts = rest.split("/", 1)
        if len(parts) != 2:
            return None
        _, obj = parts[0], parts[1]
        obj = (obj or "").lstrip("/")
        if not obj:
            return None
        if obj.startswith(f"{BUCKET_NAME}/"):
            obj = obj[len(BUCKET_NAME) + 1 :].lstrip("/")
        return obj or None

    # http(s) URL
    if s.startswith("http://") or s.startswith("https://"):
        u = urlparse(s)
        host = (u.netloc or "").lower()
        path = (u.path or "").lstrip("/")

        # https://storage.googleapis.com/bucket/object
        if "storage.googleapis.com" in host:
            if path.startswith(f"{BUCKET_NAME}/"):
                return path[len(BUCKET_NAME) + 1 :].lstrip("/")
            parts = path.split("/", 1)
            if len(parts) == 2:
                return parts[1].lstrip("/")
            return None

        # https://bucket.storage.googleapis.com/object
        if host.startswith(f"{BUCKET_NAME}."):
            return path.lstrip("/") or None

        # unknown host: best-effort treat path as object
        return path.lstrip("/") or None

    # otherwise: assume it's already an object key
    return s.lstrip("/") or None


def _split_dir_and_name(obj_key: str) -> Tuple[str, str]:
    s = (obj_key or "").strip().strip("/")
    if not s:
        return "", ""
    if "/" not in s:
        return "", s
    d, n = s.rsplit("/", 1)
    return d, n


def _sign_url_maybe(gcs, url: Optional[str]) -> Optional[str]:
    if not url:
        return url
    try:
        # Note: `url` is actually a GCS object key here; we return a signed URL.
        return gcs.signed_get_url(url, expires_seconds=3600)
    except Exception:
        return url


def _fetch_text_signed(file_link: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    """
    Fetch file contents from file_link via signed URL.

    Returns:
      (text, info)
      info = { dir, name, url }  # url is signed (or original if signing failed)
    """
    obj_key = _object_key_from_db_value(file_link)
    if not obj_key:
        raise ValueError(f"Missing/invalid file_link: {file_link}")

    d, n = _split_dir_and_name(obj_key)

    gcs = get_gcs()
    signed_url = _sign_url_maybe(gcs, obj_key)
    if not signed_url:
        raise ValueError("Unable to sign file_link")

    r = requests.get(signed_url, timeout=30)
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"

    info = {
        "dir": d,
        "name": n,
        "url": signed_url,  # optional; remove if you don't want to expose it
    }
    return r.text, info


def _to_placeholder_key(to_port_id: Optional[str]) -> Optional[str]:
    """
    "in-footer" -> "footer"
    """
    if not to_port_id:
        return None
    s = str(to_port_id).strip()
    if s.lower().startswith("in-"):
        return s[3:].strip() or None
    return None


def _component_ident(filename: str) -> str:
    """
    "MappedLandingLayout.js" -> "MappedLandingLayout"
    "Footer.jsx" -> "Footer"
    "why-choose.jsx" -> "WhyChoose"
    """
    base = re.sub(r"\.(jsx|js|tsx|ts)$", "", (filename or ""), flags=re.I).strip()
    if not base:
        return "UnknownComponent"

    parts = re.split(r"[^a-zA-Z0-9]+", base)
    parts = [p for p in parts if p]
    if not parts:
        return "UnknownComponent"

    ident = "".join(p[:1].upper() + p[1:] for p in parts)
    if not re.match(r"^[A-Za-z_]", ident):
        ident = f"Comp{ident}"
    return ident


def _import_path(component_type: str, filename: str) -> str:
    return f"./components/{component_type}/{filename}"


def _insert_imports(code: str, imports: List[str]) -> str:
    """
    Insert new imports after existing imports, deduping exact lines.
    """
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
    """
    Replace <PlaceHolder-key/> (case-insensitive) with replacement.
    Returns (new_code, replaced_bool).
    """
    if not code or not key:
        return code, False

    pat = re.compile(rf"<\s*placeholder-{re.escape(key)}\s*/\s*>", re.IGNORECASE)
    new_code, n = pat.subn(replacement, code)
    return new_code, (n > 0)


class CompConnectionPreview(BaseCommand):
    """
    GET /components/connections/preview

    - Input: funnel_id only
    - Eligibility: exists connection where to_component_id == PREVIEW_ROOT_ID
    - Return: ALL connections enriched (contents/layouts/pages only)
    - Fetch raw files using SIGNED URLs (private bucket safe)
    - Text processing:
        For each parent layout:
          - fetch parent layout text from file_link (signed URL)
          - for each incoming edge into that layout:
              - import child
              - replace <PlaceHolder-<key>/> where key from to_port_id
          - store original + transformed
        Then, inject into each connection:
          - file_raw (parent layout raw text)
          - file_transformed (parent layout transformed text)
    """

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
                eligible = (
                    (session.query(func.count(CompConnection.id))
                     .filter(CompConnection.funnel_id == payload.funnel_id)
                     .filter(CompConnection.to_component_id == PREVIEW_ROOT_ID)
                     .scalar()
                     or 0) > 0
                )
                if not eligible:
                    return resp

                resp["eligible"] = True

                # Fetch ALL connections
                rows: List[CompConnection] = (
                    session.query(CompConnection)
                    .filter(CompConnection.funnel_id == payload.funnel_id)
                    .order_by(desc(getattr(CompConnection, "created_at", func.now())))
                    .all()
                )
                if not rows:
                    return resp

                # Collect ids
                ids: Set[str] = {PREVIEW_ROOT_ID}
                for r in rows:
                    ids.add(_safe(getattr(r, "from_component_id", "")))
                    ids.add(_safe(getattr(r, "to_component_id", "")))

                comp_map = _bulk_fetch_components(session, ids)

                # Enrich connections
                enriched: List[Dict[str, Any]] = []
                for r in rows:
                    c = _serialize_connection(r)

                    from_comp = comp_map.get(c["from_component_id"])
                    to_comp = comp_map.get(c["to_component_id"])

                    # Return component metadata WITHOUT file_link
                    c["from_component"] = (
                        {k: v for k, v in (from_comp or {}).items() if k != "file_link"}
                        if from_comp
                        else None
                    )
                    c["to_component"] = (
                        {k: v for k, v in (to_comp or {}).items() if k != "file_link"}
                        if to_comp
                        else None
                    )

                    # Per-record fetched file info (raw code) — useful for UI/debug
                    # These are independent of the parent layout transform.
                    c["from_file"] = None
                    c["to_file"] = None

                    try:
                        if from_comp and from_comp.get("file_link"):
                            text, info = _fetch_text_signed(from_comp.get("file_link"))
                            # You said "file values is raw file code"
                            c["from_file"] = {
                                "dir": info.get("dir"),
                                "name": info.get("name"),
                                "file": text,
                                # "url": info.get("url"),  # uncomment if you want signed URL visible
                            }
                    except Exception as e:
                        resp["warnings"].append(
                            f"Failed to fetch from_component code for {(from_comp or {}).get('name')} ({c['from_component_id']}): {e}"
                        )

                    try:
                        if to_comp and to_comp.get("file_link"):
                            text, info = _fetch_text_signed(to_comp.get("file_link"))
                            c["to_file"] = {
                                "dir": info.get("dir"),
                                "name": info.get("name"),
                                "file": text,
                                # "url": info.get("url"),
                            }
                    except Exception as e:
                        # Not always required (e.g., PREVIEW_ROOT has no component file)
                        resp["warnings"].append(
                            f"Failed to fetch to_component code for {(to_comp or {}).get('name')} ({c['to_component_id']}): {e}"
                        )

                    # Parent-layout transform outputs (based on to_component_id)
                    c["file_raw"] = None
                    c["file_transformed"] = None

                    enriched.append(c)

                # Incoming edges: parent_id -> edges
                incoming = defaultdict(list)
                for c in enriched:
                    incoming[c["to_component_id"]].append(c)

                # Transform parent layouts
                parent_transform: Dict[str, Dict[str, Any]] = {}  # parent_layout_id -> {raw, transformed}
                for parent_id, edges in incoming.items():
                    parent = comp_map.get(parent_id)
                    if not parent or parent.get("component_type") != "layouts":
                        continue

                    try:
                        raw, _info = _fetch_text_signed(parent.get("file_link"))
                    except Exception as e:
                        resp["warnings"].append(
                            f"Failed to fetch layout code for {parent.get('name')} ({parent_id}): {e}"
                        )
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

                    parent_transform[parent_id] = {
                        "raw": raw,
                        "transformed": updated,
                    }

                # Attach raw/transformed to each connection based on its parent (to_component_id)
                for c in enriched:
                    parent_id = c["to_component_id"]
                    if parent_id in parent_transform:
                        c["file_raw"] = parent_transform[parent_id]["raw"]
                        c["file_transformed"] = parent_transform[parent_id]["transformed"]

                resp["data"] = enriched
                resp["total_items"] = len(enriched)
                return resp

            except HTTPException:
                raise
            except Exception as e:
                resp["errors"].append(str(e))
                resp["error_code"] = "UNEXPECTED"
                return resp

    def run(self, payload: ConnectionsPreviewQuery, user_id: str = None):
        return self.execute(payload, user_id=user_id)


__all__ = ["CompConnectionPreview"]
