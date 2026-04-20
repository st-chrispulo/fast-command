# testing.py
# Run: python testing.py

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Tuple
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
DOWNLOAD_CONCURRENCY = 5
DOWNLOAD_RETRIES = 3
DOWNLOAD_TIMEOUT_SECONDS = 60
SIGNED_URL_EXPIRES_SECONDS = 900
DOWNLOAD_MANIFEST_FILENAME = "download_manifest.json"


def _json_default(o):
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    return str(o)


def _to_iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def _append_warning(item: Dict[str, Any], loc: str, message: str) -> None:
    warnings = item.get("warning")
    if not isinstance(warnings, list):
        warnings = []
    warnings.append({"loc": loc, "message": str(message)})
    item["warning"] = warnings


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
        "created_at": _to_iso(getattr(r, "created_at", None)),
        "updated_at": _to_iso(getattr(r, "updated_at", None)),
    }


def _normalize_component_type(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    s = s.replace("tbl_", "").replace("comp_", "").replace("components.", "")
    s = s.replace("-", "_").replace(" ", "_")
    s_singular = s[:-1] if s.endswith("s") else s
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
    by_type: Dict[str, set] = {}

    for it in items:
        cid = it.get("component_id")
        ctype_norm = _normalize_component_type(it.get("component_type"))
        uid = _safe_uuid(cid)
        if not uid or not ctype_norm:
            continue
        by_type.setdefault(ctype_norm, set()).add(uid)

    meta_by_id: Dict[str, Dict[str, Any]] = {}

    for ctype_norm, ids in by_type.items():
        model = _get_model_for_type(ctype_norm)
        if not model or not ids:
            continue

        file_link_col = getattr(model, "file_link", None)
        group_id_col = getattr(model, "group_id", None)
        updated_at_col = getattr(model, "updated_at", None)

        q = session.query(model.id, file_link_col, group_id_col, updated_at_col).filter(
            model.id.in_(list(ids))
        )

        for rid, file_link, group_id, updated_at in q.all():
            meta_by_id[str(rid)] = {
                "file_link": file_link,
                "group_id": group_id,
                "updated_at": _to_iso(updated_at),
            }

    return meta_by_id


def _signed_url_for_key(gcs, file_link, expires_seconds=SIGNED_URL_EXPIRES_SECONDS):
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
    except Exception as e:
        logger.warning("Failed to sign url for key=%s: %s", key, e)
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


def _load_download_manifest(in_dir: str) -> Dict[str, Any]:
    path = Path(in_dir) / DOWNLOAD_MANIFEST_FILENAME
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Failed to load download manifest: %s", e)
        return {}


def _save_download_manifest(in_dir: str, manifest: Dict[str, Any]) -> None:
    path = Path(in_dir) / DOWNLOAD_MANIFEST_FILENAME
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False, default=_json_default)
    os.replace(tmp_path, path)


def _build_manifest_key(item: Dict[str, Any]) -> str:
    node_id = str(item.get("node_id") or "")
    component_id = str(item.get("component_id") or "")
    return f"{node_id}:{component_id}"


def _is_download_unchanged(item: Dict[str, Any], manifest: Dict[str, Any]) -> bool:
    local_path = item.get("local_path")
    if not local_path or not Path(local_path).exists():
        return False

    manifest_key = _build_manifest_key(item)
    existing = manifest.get(manifest_key)
    if not isinstance(existing, dict):
        return False

    current_updated_at = _to_iso(item.get("source_updated_at"))
    current_file_link = str(item.get("file_link") or "")
    current_component_id = str(item.get("component_id") or "")

    return (
        existing.get("updated_at") == current_updated_at
        and existing.get("file_link") == current_file_link
        and existing.get("component_id") == current_component_id
    )


def _download_to_path_with_retry(
    url: str,
    path: str,
    timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS,
    retries: int = DOWNLOAD_RETRIES,
) -> Optional[str]:
    if not url:
        return "missing signed_file_link"

    Path(path).parent.mkdir(parents=True, exist_ok=True)

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout_seconds) as r:
                r.raise_for_status()

                with NamedTemporaryFile(delete=False, dir=str(Path(path).parent)) as tmp:
                    tmp_path = tmp.name
                    try:
                        for chunk in r.iter_content(chunk_size=1024 * 256):
                            if chunk:
                                tmp.write(chunk)
                        tmp.flush()
                        os.fsync(tmp.fileno())
                    except Exception:
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass
                        raise

            os.replace(tmp_path, path)
            return None

        except Exception as e:
            last_error = e
            logger.warning(
                "Download failed attempt=%s/%s path=%s error=%s",
                attempt,
                retries,
                path,
                e,
            )
            if attempt < retries:
                time.sleep(0.5 * attempt)

    return str(last_error) if last_error else "download failed"


def _download_item(item: Dict[str, Any]) -> Tuple[str, Optional[str], bool]:
    local_path = str(item.get("local_path") or "")
    signed = item.get("signed_file_link")
    err = _download_to_path_with_retry(signed, local_path)
    return local_path, err, err is None


def _download_files_concurrently(
    items: List[Dict[str, Any]],
    manifest: Dict[str, Any],
    max_workers: int = DOWNLOAD_CONCURRENCY,
) -> None:
    to_download: List[Dict[str, Any]] = []

    for item in items:
        if not item.get("node_id"):
            item["_file_text"] = ""
            continue

        if _is_download_unchanged(item, manifest):
            item["download_status"] = "skipped_unchanged"
            item["_file_text"] = _read_text_file(item["local_path"])
            continue

        to_download.append(item)

    if not to_download:
        return

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_download_item, item): item for item in to_download}

        for future in as_completed(future_map):
            item = future_map[future]
            try:
                _, err, ok = future.result()
            except Exception as e:
                err = str(e)
                ok = False

            if not ok:
                item["download_status"] = "failed"
                item["_file_text"] = ""
                _append_warning(item, "download", err or "download failed")
                continue

            item["download_status"] = "downloaded"
            item["_file_text"] = _read_text_file(item["local_path"])


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


def _scan_ports(text: str) -> Optional[Dict[str, Any]]:
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

        if len(matches) > 1:
            _append_warning(
                it,
                "scan file",
                f"Found {len(matches)} export default function matches",
            )

    for it in items:
        base = it.get("_base_classname")
        if not base:
            it["classname"] = None
            continue

        used[base] = used.get(base, 0) + 1
        n = used[base]
        it["classname"] = base if n == 1 else f"{base}{n}"

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
            _append_warning(it, "rewrite", "Failed to rewrite export default function name")


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
        s = str(raw or "").strip() or "utility"

        if "." in s and not s.startswith("."):
            base, ext = s.rsplit(".", 1)
            ext = "." + ext
        else:
            base, ext = s, ""

        base_norm = base.strip() or "utility"
        key = f"{base_norm.lower()}{ext.lower()}"

        used[key] = used.get(key, 0) + 1
        n = used[key]

        out.append(f"{base_norm}{ext}" if n == 1 else f"{base_norm}{n}{ext}")

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
                "signed_file_link": _signed_url_for_key(
                    gcs,
                    file_link,
                    expires_seconds=SIGNED_URL_EXPIRES_SECONDS,
                ),
            }
            utilities_by_group.setdefault(gid, []).append(item)

    all_items: List[Dict[str, Any]] = []
    for lst in utilities_by_group.values():
        all_items.extend(lst)

    unique_names = _ensure_unique_filenames([(it.get("name") or "").strip() for it in all_items])
    for it, new_name in zip(all_items, unique_names):
        it["name"] = new_name

    return utilities_by_group


def _collect_node_descriptors(comp_connections: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, Any]]]:
    seen = set()
    node_ids: List[str] = []

    for c in comp_connections:
        for nid in (c.get("from_node_id"), c.get("to_node_id")):
            if not nid or nid in seen:
                continue
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

    return node_ids, enriched_node_ids


def _enrich_component_meta(
    enriched_node_ids: List[Dict[str, Any]],
    meta_by_id: Dict[str, Dict[str, Any]],
    gcs,
    in_dir: str,
) -> None:
    for it in enriched_node_ids:
        cid = it.get("component_id")
        meta = meta_by_id.get(str(cid)) if cid else None

        file_link = (meta or {}).get("file_link")
        group_id = (meta or {}).get("group_id")
        updated_at = (meta or {}).get("updated_at")

        it["file_link"] = file_link
        it["group_id"] = group_id
        it["source_updated_at"] = updated_at
        it["signed_file_link"] = _signed_url_for_key(
            gcs,
            file_link,
            expires_seconds=SIGNED_URL_EXPIRES_SECONDS,
        )

        nid = it.get("node_id")
        if nid:
            it["local_path"] = str(Path(in_dir) / _safe_filename(nid))


def _rewrite_and_scan_files(enriched_node_ids: List[Dict[str, Any]]) -> None:
    _resolve_unique_classnames(enriched_node_ids)

    for it in enriched_node_ids:
        text = it.get("_file_text") or ""
        it["ports"] = _scan_ports(text)

        if it.get("_rewrote_classname"):
            err = _write_text_file(it["local_path"], it.get("_file_text") or "")
            if err:
                _append_warning(it, "write", err)


def _apply_utilities(
    enriched_node_ids: List[Dict[str, Any]],
    utilities_lookup: Dict[str, List[Dict[str, Any]]],
) -> None:
    for it in enriched_node_ids:
        gid = it.get("group_id")
        if gid is None:
            it["utilities"] = []
            continue

        matches = utilities_lookup.get(str(gid), [])
        it["utilities"] = [u.get("name") for u in matches if u.get("name")]


def _cleanup_runtime_fields(enriched_node_ids: List[Dict[str, Any]]) -> None:
    for it in enriched_node_ids:
        it.pop("_file_text", None)
        it.pop("_export_matches", None)
        it.pop("_base_classname", None)
        it.pop("_rewrote_classname", None)


def process_funnel(funnel_id: str) -> Dict[str, Any]:
    dirs = _ensure_dirs(WORKDIR, funnel_id)
    in_dir = dirs["in_dir"]

    gcs = get_gcs()
    manifest = _load_download_manifest(in_dir)

    with SessionLocal() as session:
        rows = (
            session.query(CompConnection)
            .filter(CompConnection.funnel_id == funnel_id)
            .order_by(CompConnection.created_at.desc())
            .limit(LIMIT)
            .all()
        )

        comp_connections = [to_comp_connection_dict(r) for r in rows]
        node_ids, enriched_node_ids = _collect_node_descriptors(comp_connections)

        meta_by_id = _bulk_fetch_component_meta(session, enriched_node_ids)
        _enrich_component_meta(enriched_node_ids, meta_by_id, gcs, in_dir)

        _download_files_concurrently(
            enriched_node_ids,
            manifest=manifest,
            max_workers=DOWNLOAD_CONCURRENCY,
        )

        _rewrite_and_scan_files(enriched_node_ids)
        _fill_ports_from_connections(enriched_node_ids, comp_connections)

        utilities_lookup = _bulk_fetch_utilities_lookup(session, enriched_node_ids, gcs)
        _apply_utilities(enriched_node_ids, utilities_lookup)

        updated_manifest = dict(manifest)
        for it in enriched_node_ids:
            if not it.get("node_id"):
                continue
            if not Path(str(it.get("local_path") or "")).exists():
                continue
            if it.get("download_status") not in {"downloaded", "skipped_unchanged"}:
                continue

            updated_manifest[_build_manifest_key(it)] = {
                "node_id": str(it.get("node_id") or ""),
                "component_id": str(it.get("component_id") or ""),
                "file_link": str(it.get("file_link") or ""),
                "updated_at": _to_iso(it.get("source_updated_at")),
                "local_path": str(it.get("local_path") or ""),
                "saved_at": datetime.utcnow().isoformat(),
            }

        _save_download_manifest(in_dir, updated_manifest)
        _cleanup_runtime_fields(enriched_node_ids)

    out = {
        "workdir": WORKDIR,
        "funnel_id": funnel_id,
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

    return {
        "ok": True,
        "metadata_path": metadata_path,
        "funnel_id": funnel_id,
        "downloaded": sum(1 for x in enriched_node_ids if x.get("download_status") == "downloaded"),
        "skipped_unchanged": sum(
            1 for x in enriched_node_ids if x.get("download_status") == "skipped_unchanged"
        ),
        "failed": sum(1 for x in enriched_node_ids if x.get("download_status") == "failed"),
    }


def main():
    result = process_funnel(FUNNEL_ID)
    print(json.dumps(result, ensure_ascii=False))


def run_step_1(funnel_id: str):
    result = process_funnel(funnel_id)
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    main()