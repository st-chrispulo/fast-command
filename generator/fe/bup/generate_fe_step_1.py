# testing.py
# Run: python testing.py

import json
import os
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import UUID

import requests
from sqlalchemy.orm import Session

from auth.db import SessionLocalExternal as SessionLocal
from models.components.tbl_comp_connections import CompConnection
from models.components.tbl_comp_contents import CompContent
from models.components.tbl_comp_layouts import CompLayout
from models.components.tbl_comp_pages import CompPage
from models.components.tbl_comp_authentications import CompAuthentication
from models.components.tbl_comp_navigations import CompNavigation

from integrations.gcs.gcs import get_gcs

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.authentications.update_with_uploads")
except Exception:
    import logging

    logger = logging.getLogger(__name__)

FUNNEL_ID = "b4dc3740-44ba-43ea-86eb-10d55874b28f"
LIMIT = 200

WORKDIR = os.getenv("WORKDIR", "FE_Workbench")


def _json_default(o):
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    return str(o)


def to_comp_connection_dict(r: CompConnection) -> dict:
    return {
        "id": str(r.id),
        "funnel_id": str(r.funnel_id),
        "from_node_id": getattr(r, "from_node_id", None),
        "to_node_id": getattr(r, "to_node_id", None),
        "from_component_id": str(r.from_component_id) if r.from_component_id else None,
        "from_port_id": r.from_port_id,
        "to_component_id": str(r.to_component_id) if r.to_component_id else None,
        "to_port_id": r.to_port_id,
        "metadata": getattr(r, "metadata_json", None) or {},
        "created_at": r.created_at.isoformat() if getattr(r, "created_at", None) else None,
        "updated_at": r.updated_at.isoformat() if getattr(r, "updated_at", None) else None,
    }


def _normalize_component_type(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    s = s.replace("tbl_", "").replace("comp_", "").replace("components.", "")
    s = s.replace("-", "_").replace(" ", "_")
    if s.endswith("s"):
        s_singular = s[:-1]
    else:
        s_singular = s
    aliases = {
        "content": "content",
        "contents": "content",
        "layout": "layout",
        "layouts": "layout",
        "page": "page",
        "pages": "page",
        "authentication": "authentication",
        "authentications": "authentication",
        "auth": "authentication",
        "navigation": "navigation",
        "navigations": "navigation",
        "nav": "navigation",
    }
    return aliases.get(s, aliases.get(s_singular, s_singular))


def _get_model_for_type(component_type_norm):
    return {
        "content": CompContent,
        "layout": CompLayout,
        "page": CompPage,
        "authentication": CompAuthentication,
        "navigation": CompNavigation,
    }.get(component_type_norm)


def _safe_uuid(s):
    if not s:
        return None
    try:
        return UUID(str(s))
    except Exception:
        return None


def _bulk_fetch_component_meta(session: Session, items):
    by_type = {}
    for it in items:
        cid = it.get("component_id")
        ctype = it.get("component_type")
        ctype_norm = _normalize_component_type(ctype)
        uid = _safe_uuid(cid)
        if not uid or not ctype_norm:
            continue
        by_type.setdefault(ctype_norm, set()).add(uid)

    meta_by_id = {}

    for ctype_norm, ids in by_type.items():
        model = _get_model_for_type(ctype_norm)
        if not model or not ids:
            continue

        file_link_col = getattr(model, "file_link", None)
        group_id_col = getattr(model, "group_id", None)

        q = session.query(model.id, file_link_col, group_id_col).filter(model.id.in_(list(ids)))
        for rid, file_link, group_id in q.all():
            meta_by_id[str(rid)] = {
                "file_link": file_link,
                "group_id": group_id,
            }

    return meta_by_id


def _signed_url_for_key(gcs, file_link, expires_seconds=900):
    if not file_link:
        return None

    s = str(file_link).strip()
    if not s:
        return None

    if s.startswith("http://") or s.startswith("https://"):
        return s

    key = s.lstrip("/")
    try:
        return gcs.signed_get_url(key, expires_seconds=expires_seconds)
    except Exception:
        return None


def _ensure_dirs(workdir: str, funnel_id: str) -> Dict[str, str]:
    base = Path(workdir) / str(funnel_id)
    in_dir = base / "in"
    out_dir = base / "output"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    return {
        "base": str(base),
        "in_dir": str(in_dir),
        "out_dir": str(out_dir),
    }


def _safe_filename(s: str) -> str:
    s = str(s or "").strip()
    if not s:
        return "node"
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    s = s.strip("._-")
    return s or "node"


def _download_to_path(url: str, path: str, timeout_seconds: int = 60) -> Optional[str]:
    if not url:
        return "missing signed_file_link"
    try:
        with requests.get(url, stream=True, timeout=timeout_seconds) as r:
            r.raise_for_status()
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
        return None
    except Exception as e:
        return str(e)


_EXPORT_DEFAULT_FN_RE = re.compile(
    r"export\s+default\s+function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
    re.MULTILINE,
)

_EXPORT_DEFAULT_FN_FIRST_RE = re.compile(
    r"(export\s+default\s+function\s+)([A-Za-z_$][A-Za-z0-9_$]*)(\s*\()",
    re.MULTILINE,
)

_PLACEHOLDER_TAG_RE = re.compile(
    r"<\s*(PlaceHolder-(?:Any|Contents|Array|Map-[A-Za-z0-9_-]+))\s*/\s*>",
    re.MULTILINE,
)


def _read_text_file(path: str) -> str:
    try:
        with open(path, "rb") as f:
            raw = f.read()
        try:
            return raw.decode("utf-8")
        except Exception:
            return raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _write_text_file(path: str, text: str) -> Optional[str]:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return None
    except Exception as e:
        return str(e)


def _scan_ports(text: str) -> Dict[str, Any]:
    ports: Dict[str, Any] = {}
    tags = _PLACEHOLDER_TAG_RE.findall(text or "")
    for tag in tags:
        if tag == "PlaceHolder-Contents":
            ports["in-contents"] = None
        elif tag == "PlaceHolder-Any":
            ports["in-any"] = None
        elif tag == "PlaceHolder-Array":
            ports["in-array"] = None
        elif tag.startswith("PlaceHolder-Map-"):
            key = tag[len("PlaceHolder-Map-") :].strip()
            if key:
                ports[f"in-{key}"] = None
    return ports or None


def _resolve_unique_classnames(items: List[Dict[str, Any]]) -> None:
    used: Dict[str, int] = {}
    for it in items:
        text = it.get("_file_text") or ""
        matches = _EXPORT_DEFAULT_FN_RE.findall(text)
        it["_export_matches"] = matches

        base = matches[0] if matches else None
        it["_base_classname"] = base

        warnings = it.get("warning") if isinstance(it.get("warning"), list) else []
        if len(matches) > 1:
            warnings.append(
                {
                    "loc": "scan file",
                    "message": f"Found {len(matches)} export default function matches",
                }
            )
        if warnings:
            it["warning"] = warnings
        else:
            it.pop("warning", None)

    for it in items:
        base = it.get("_base_classname")
        if not base:
            it["classname"] = None
            continue

        n = used.get(base, 0) + 1
        used[base] = n
        if n == 1:
            it["classname"] = base
        else:
            it["classname"] = f"{base}{n}"

    for it in items:
        base = it.get("_base_classname")
        new = it.get("classname")
        if not base or not new or base == new:
            continue

        text = it.get("_file_text") or ""
        replaced = _EXPORT_DEFAULT_FN_FIRST_RE.sub(rf"\1{new}\3", text, count=1)
        if replaced != text:
            it["_file_text"] = replaced
            it["_rewrote_classname"] = True
        else:
            warnings = it.get("warning") if isinstance(it.get("warning"), list) else []
            warnings.append(
                {"loc": "rewrite", "message": "Failed to rewrite export default function name"}
            )
            it["warning"] = warnings


def _lc(v: Any) -> str:
    return str(v or "").strip().lower()


def _fill_ports_from_connections(
    enriched_node_ids: List[Dict[str, Any]],
    comp_connections: List[Dict[str, Any]],
) -> None:
    meta_by_node_id: Dict[str, Dict[str, Any]] = {}
    for it in enriched_node_ids:
        nid = it.get("node_id")
        if not nid:
            continue
        meta_by_node_id[str(nid)] = {
            "classname": it.get("classname"),
            "component_type": it.get("component_type"),
        }

    by_to: Dict[str, List[Dict[str, Any]]] = {}
    for c in comp_connections:
        to_nid = c.get("to_node_id")
        if not to_nid:
            continue
        by_to.setdefault(str(to_nid), []).append(c)

    for it in enriched_node_ids:
        nid = it.get("node_id")
        ports = it.get("ports")
        if not nid or not isinstance(ports, dict) or not ports:
            continue

        conns = by_to.get(str(nid), [])
        if not conns:
            for k in list(ports.keys()):
                ports[k] = []
            continue

        for port_key in list(ports.keys()):
            want = _lc(port_key)
            matches = []
            for c in conns:
                if _lc(c.get("to_port_id")) != want:
                    continue
                from_nid = c.get("from_node_id")
                if not from_nid:
                    continue
                meta = meta_by_node_id.get(str(from_nid), {})
                matches.append(
                    {
                        "node_id": str(from_nid),
                        "classname": meta.get("classname"),
                        "component_type": meta.get("component_type"),
                    }
                )
            ports[port_key] = matches


def _ensure_unique_filenames(names: List[str]) -> List[str]:
    used: Dict[str, int] = {}
    out: List[str] = []

    for raw in names or []:
        s = str(raw or "").strip()
        if not s:
            s = "utility"

        if "." in s and not s.startswith("."):
            base, ext = s.rsplit(".", 1)
            ext = "." + ext
        else:
            base, ext = s, ""

        base_norm = base.strip()
        if not base_norm:
            base_norm = "utility"

        key = (base_norm.lower() + ext.lower()).strip()
        n = used.get(key, 0) + 1
        used[key] = n

        if n == 1:
            out.append(f"{base_norm}{ext}")
        else:
            out.append(f"{base_norm}{n}{ext}")

    return out


def _bulk_fetch_utilities_lookup(
    session: Session,
    enriched_node_ids: List[Dict[str, Any]],
    gcs,
) -> Dict[str, List[Dict[str, Any]]]:
    by_type_and_group: Dict[str, set] = {}

    for it in enriched_node_ids or []:
        ctype_norm = _normalize_component_type(it.get("component_type"))
        group_id = it.get("group_id")
        if not ctype_norm or group_id is None:
            continue
        by_type_and_group.setdefault(ctype_norm, set()).add(group_id)

    utilities_by_group: Dict[str, List[Dict[str, Any]]] = {}

    for ctype_norm, group_ids in by_type_and_group.items():
        model = _get_model_for_type(ctype_norm)
        if not model or not group_ids:
            continue

        group_col = getattr(model, "group_id", None)
        subtype_col = getattr(model, "sub_type", None)
        name_col = getattr(model, "name", None)
        file_link_col = getattr(model, "file_link", None)

        if group_col is None or subtype_col is None or name_col is None:
            continue

        q = (
            session.query(model.id, name_col, file_link_col, group_col)
            .filter(group_col.in_(list(group_ids)))
            .filter(subtype_col == "utility")
        )

        for component_id, name, file_link, group_id in q.all():
            gid = str(group_id)
            item = {
                "component_id": str(component_id),
                "name": str(name) if name is not None else None,
                "file_link": file_link,
                "group_id": gid,
                "signed_file_link": _signed_url_for_key(gcs, file_link, expires_seconds=900),
            }
            utilities_by_group.setdefault(gid, []).append(item)

    all_items: List[Dict[str, Any]] = []
    for _, lst in utilities_by_group.items():
        all_items.extend(lst)

    original_names = [(it.get("name") or "").strip() for it in all_items]
    unique_names = _ensure_unique_filenames(original_names)

    for it, new_name in zip(all_items, unique_names):
        it["name"] = new_name

    return utilities_by_group


def main():
    dirs = _ensure_dirs(WORKDIR, FUNNEL_ID)
    in_dir = dirs["in_dir"]

    gcs = get_gcs()

    with SessionLocal() as session:
        rows = (
            session.query(CompConnection)
            .filter(CompConnection.funnel_id == FUNNEL_ID)
            .order_by(CompConnection.created_at.desc())
            .limit(LIMIT)
            .all()
        )

        comp_connections = [to_comp_connection_dict(r) for r in rows]

        seen = set()
        node_ids = []
        for c in comp_connections:
            for nid in (c.get("from_node_id"), c.get("to_node_id")):
                if not nid:
                    continue
                if nid not in seen:
                    seen.add(nid)
                    node_ids.append(nid)

        enriched_node_ids: List[Dict[str, Any]] = []
        for nid in node_ids:
            component_id = None
            component_type = None

            for c in comp_connections:
                md = c.get("metadata") or {}

                if c.get("from_node_id") == nid:
                    component_id = c.get("from_component_id")
                    component_type = md.get("fromComponentType")
                    break

                if c.get("to_node_id") == nid:
                    component_id = c.get("to_component_id")
                    component_type = md.get("toComponentType")
                    break

            enriched_node_ids.append(
                {
                    "node_id": nid,
                    "component_id": component_id,
                    "component_type": component_type,
                }
            )

        meta_by_id = _bulk_fetch_component_meta(session, enriched_node_ids)

        for it in enriched_node_ids:
            cid = it.get("component_id")
            meta = meta_by_id.get(str(cid)) if cid else None

            file_link = (meta or {}).get("file_link")
            group_id = (meta or {}).get("group_id")

            it["file_link"] = file_link
            it["group_id"] = group_id
            it["signed_file_link"] = _signed_url_for_key(gcs, file_link, expires_seconds=900)

        for it in enriched_node_ids:
            nid = it.get("node_id")
            signed = it.get("signed_file_link")
            if not nid:
                continue

            filename = _safe_filename(nid)
            local_path = str(Path(in_dir) / filename)
            it["local_path"] = local_path

            err = _download_to_path(signed, local_path)
            if err:
                it["warning"] = [{"loc": "download", "message": err}]
                it["_file_text"] = ""
                continue

            text = _read_text_file(local_path)
            it["_file_text"] = text

        _resolve_unique_classnames(enriched_node_ids)

        for it in enriched_node_ids:
            text = it.get("_file_text") or ""
            it["ports"] = _scan_ports(text)

            if it.get("_rewrote_classname"):
                err = _write_text_file(it["local_path"], it.get("_file_text") or "")
                if err:
                    warnings = it.get("warning") if isinstance(it.get("warning"), list) else []
                    warnings.append({"loc": "write", "message": err})
                    it["warning"] = warnings

        _fill_ports_from_connections(enriched_node_ids, comp_connections)

        utilities_lookup = _bulk_fetch_utilities_lookup(session, enriched_node_ids, gcs)

        for it in enriched_node_ids:
            gid = it.get("group_id")
            it["utilities"] = []
            if gid is None:
                continue
            matches = utilities_lookup.get(str(gid), [])
            it["utilities"] = [u.get("name") for u in matches if u.get("name")]

        for it in enriched_node_ids:
            it.pop("_file_text", None)
            it.pop("_export_matches", None)
            it.pop("_base_classname", None)
            it.pop("_rewrote_classname", None)

    out = {
        "workdir": WORKDIR,
        "funnel_id": FUNNEL_ID,
        "in_dir": dirs["in_dir"],
        "out_dir": dirs["out_dir"],
        "comp_connections": comp_connections,
        "node_ids": node_ids,
        "enriched_node_ids": enriched_node_ids,
        "utilities_lookup": utilities_lookup,
    }

    metadata_path = str(Path(dirs["in_dir"]) / "metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=_json_default)

    print(json.dumps({"ok": True, "metadata_path": metadata_path}, ensure_ascii=False))


if __name__ == "__main__":
    main()


def run_step_1(funnel_id: str):

    dirs = _ensure_dirs(WORKDIR, funnel_id)
    in_dir = dirs["in_dir"]

    gcs = get_gcs()

    with SessionLocal() as session:
        rows = (
            session.query(CompConnection)
            .filter(CompConnection.funnel_id == funnel_id)
            .order_by(CompConnection.created_at.desc())
            .limit(LIMIT)
            .all()
        )

        comp_connections = [to_comp_connection_dict(r) for r in rows]

        seen = set()
        node_ids = []
        for c in comp_connections:
            for nid in (c.get("from_node_id"), c.get("to_node_id")):
                if not nid:
                    continue
                if nid not in seen:
                    seen.add(nid)
                    node_ids.append(nid)

        enriched_node_ids: List[Dict[str, Any]] = []
        for nid in node_ids:
            component_id = None
            component_type = None

            for c in comp_connections:
                md = c.get("metadata") or {}

                if c.get("from_node_id") == nid:
                    component_id = c.get("from_component_id")
                    component_type = md.get("fromComponentType")
                    break

                if c.get("to_node_id") == nid:
                    component_id = c.get("to_component_id")
                    component_type = md.get("toComponentType")
                    break

            enriched_node_ids.append(
                {
                    "node_id": nid,
                    "component_id": component_id,
                    "component_type": component_type,
                }
            )

        meta_by_id = _bulk_fetch_component_meta(session, enriched_node_ids)

        for it in enriched_node_ids:
            cid = it.get("component_id")
            meta = meta_by_id.get(str(cid)) if cid else None

            file_link = (meta or {}).get("file_link")
            group_id = (meta or {}).get("group_id")

            it["file_link"] = file_link
            it["group_id"] = group_id
            it["signed_file_link"] = _signed_url_for_key(gcs, file_link, expires_seconds=900)

        for it in enriched_node_ids:
            nid = it.get("node_id")
            signed = it.get("signed_file_link")
            if not nid:
                continue

            filename = _safe_filename(nid)
            local_path = str(Path(in_dir) / filename)
            it["local_path"] = local_path

            err = _download_to_path(signed, local_path)
            if err:
                it["warning"] = [{"loc": "download", "message": err}]
                it["_file_text"] = ""
                continue

            text = _read_text_file(local_path)
            it["_file_text"] = text

        _resolve_unique_classnames(enriched_node_ids)

        for it in enriched_node_ids:
            text = it.get("_file_text") or ""
            it["ports"] = _scan_ports(text)

            if it.get("_rewrote_classname"):
                err = _write_text_file(it["local_path"], it.get("_file_text") or "")
                if err:
                    warnings = it.get("warning") if isinstance(it.get("warning"), list) else []
                    warnings.append({"loc": "write", "message": err})
                    it["warning"] = warnings

        _fill_ports_from_connections(enriched_node_ids, comp_connections)

        utilities_lookup = _bulk_fetch_utilities_lookup(session, enriched_node_ids, gcs)

        for it in enriched_node_ids:
            gid = it.get("group_id")
            it["utilities"] = []
            if gid is None:
                continue
            matches = utilities_lookup.get(str(gid), [])
            it["utilities"] = [u.get("name") for u in matches if u.get("name")]

        for it in enriched_node_ids:
            it.pop("_file_text", None)
            it.pop("_export_matches", None)
            it.pop("_base_classname", None)
            it.pop("_rewrote_classname", None)

    out = {
        "workdir": WORKDIR,
        "funnel_id": FUNNEL_ID,
        "in_dir": dirs["in_dir"],
        "out_dir": dirs["out_dir"],
        "comp_connections": comp_connections,
        "node_ids": node_ids,
        "enriched_node_ids": enriched_node_ids,
        "utilities_lookup": utilities_lookup,
    }

    metadata_path = str(Path(dirs["in_dir"]) / "metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=_json_default)

    print(json.dumps({"ok": True, "metadata_path": metadata_path}, ensure_ascii=False))

