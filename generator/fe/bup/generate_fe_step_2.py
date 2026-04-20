import argparse
import json
import os
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import requests

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.authentications.update_with_uploads")
except Exception:
    import logging

    logger = logging.getLogger(__name__)

WORKDIR = os.getenv("WORKDIR", "FE_Workbench")


IMPORT_ALIAS = os.getenv("IMPORT_ALIAS", "@/").strip()

def _alias_join(*parts: str) -> str:
    base = IMPORT_ALIAS
    if base and not base.endswith("/"):
        base += "/"
    p = "/".join([str(x).strip("/").strip() for x in parts if str(x or "").strip()])
    return f"{base}{p}" if base else p


def _json_default(o):
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    return str(o)


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


def _component_dir_for_type(component_type: Any) -> Optional[str]:
    t = _normalize_component_type(component_type)
    if t == "content":
        return "components/contents"
    if t == "layout":
        return "components/layouts"
    if t == "authentication":
        return "components/authentications"
    if t == "page":
        return "components/pages"
    if t == "navigation":
        return "components/navigations"
    return None


def _import_prefix_for_type(component_type: Any) -> Optional[str]:
    t = _normalize_component_type(component_type)
    if t == "content":
        return "contents"
    if t == "layout":
        return "layouts"
    if t == "authentication":
        return "authentications"
    if t == "page":
        return "pages"
    if t == "navigation":
        return "navigations"
    return None


def _safe_filename(s: str) -> str:
    s = str(s or "").strip()
    if not s:
        return "file"
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    s = s.strip("._-")
    return s or "file"


def _read_text(path: str) -> str:
    try:
        with open(path, "rb") as f:
            raw = f.read()
        try:
            return raw.decode("utf-8")
        except Exception:
            return raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _write_text(path: str, text: str) -> Optional[str]:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return None
    except Exception as e:
        return str(e)


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


def _is_blank_line(line: str) -> bool:
    return (line or "").strip() == ""


def _is_comment_line(line: str) -> bool:
    t = (line or "").lstrip()
    return t.startswith("//") or t.startswith("/*") or t.startswith("*") or t.startswith("*/")


def _is_directive_line(line: str) -> bool:
    t = (line or "").strip()
    return t in ('"use client";', "'use client';", '"use server";', "'use server';")


def _split_import_block(text: str) -> Tuple[str, str, str]:
    lines = (text or "").splitlines(True)

    pre: List[str] = []
    imports: List[str] = []
    tail: List[str] = []

    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("import "):
            break
        if _is_directive_line(line) or _is_blank_line(line) or _is_comment_line(line):
            pre.append(line)
            i += 1
            continue
        pre.append(line)
        i += 1

    started = False
    while i < n:
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("import "):
            started = True
            imports.append(line)
            i += 1
            continue
        if started and (_is_blank_line(line) or _is_comment_line(line)):
            imports.append(line)
            i += 1
            continue
        break

    tail = lines[i:]
    return "".join(pre), "".join(imports), "".join(tail)


def _strip_ext(name: str) -> str:
    s = str(name or "").strip()
    if s.lower().endswith(".jsx"):
        return s[:-4]
    if s.lower().endswith(".js"):
        return s[:-3]
    if s.lower().endswith(".tsx"):
        return s[:-4]
    if s.lower().endswith(".ts"):
        return s[:-2]
    return s


def _resolve_utility_name(import_target: str, utilities: List[str]) -> Optional[str]:
    target = _strip_ext(str(import_target or "").strip())
    if not target:
        return None

    opts = [str(u or "").strip() for u in (utilities or []) if str(u or "").strip()]
    if not opts:
        return None

    target_l = target.lower()

    for u in opts:
        if _strip_ext(u).lower() == target_l:
            return _strip_ext(u)

    for u in opts:
        if _strip_ext(u).lower().startswith(target_l):
            return _strip_ext(u)

    return None


def _build_port_classname_type_map(ports: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for _, arr in (ports or {}).items():
        if not isinstance(arr, list):
            continue
        for it in arr:
            if not isinstance(it, dict):
                continue
            cn = str(it.get("classname") or "").strip()
            ct = it.get("component_type")
            if not cn:
                continue
            if cn not in out:
                out[cn] = ct
    return out


_IMPORT_FROM_REL_RE = re.compile(r'(from\s+["\'])\./([^"\']+)(["\'])', re.MULTILINE)
_IMPORT_SIDE_EFFECT_REL_RE = re.compile(r'^(import\s+)(["\'])\./([^"\']+)(\2\s*;?\s*)$', re.MULTILINE)

_ASSET_EXTS = {
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".styl",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp3",
    ".mp4",
    ".webm",
    ".pdf",
}


def _has_asset_ext(path: str) -> bool:
    p = str(path or "").strip()
    ext = Path(p).suffix.lower()
    return ext in _ASSET_EXTS


def _rewrite_relative_imports(
    import_block: str,
    utilities: List[str],
    ports: Dict[str, Any],
    warnings: List[Dict[str, Any]],
) -> str:
    port_map = _build_port_classname_type_map(ports)

    def resolve_new_path(rel_path: str) -> Optional[str]:
        if not rel_path:
            return None
        if _has_asset_ext(rel_path):
            return None

        base = rel_path.split("/")[-1]
        base_no_ext = _strip_ext(base)

        resolved_util = _resolve_utility_name(base_no_ext, utilities)
        if resolved_util:
            return _alias_join("utilities", resolved_util)

        if base_no_ext in port_map:
            prefix = _import_prefix_for_type(port_map.get(base_no_ext))
            if prefix:
                return _alias_join(prefix, base_no_ext)
            warnings.append({"loc": "fe_generation", "message": f"unknown component_type for port import: {base_no_ext}"})
            return None

        return None

    def repl_from(m):
        before = m.group(1)
        rel_path = m.group(2)
        after = m.group(3)

        new_path = resolve_new_path(rel_path)
        if not new_path:
            return m.group(0)

        return f"{before}{new_path}{after}"

    def repl_side_effect(m):
        before = m.group(1)
        quote = m.group(2)
        rel_path = m.group(3)
        after = m.group(4)

        new_path = resolve_new_path(rel_path)
        if not new_path:
            return m.group(0)

        return f"{before}{quote}{new_path}{quote}{after}"

    out = _IMPORT_FROM_REL_RE.sub(repl_from, import_block or "")
    out = _IMPORT_SIDE_EFFECT_REL_RE.sub(repl_side_effect, out)
    return out


def _existing_import_lines(import_block: str) -> set:
    s = set()
    for line in (import_block or "").splitlines():
        t = line.strip()
        if t.startswith("import "):
            s.add(t)
    return s


def _append_port_imports(import_block: str, required: List[str]) -> str:
    have = _existing_import_lines(import_block)
    to_add = [ln for ln in required if ln.strip() and ln.strip() not in have]
    if not to_add:
        return import_block

    out = import_block or ""
    if out and not out.endswith("\n"):
        out += "\n"
    if out and not out.endswith("\n\n"):
        out += "\n"
    out += "\n".join(to_add) + "\n"
    return out


def _build_required_port_imports(
    node_classname: Optional[str],
    ports: Dict[str, Any],
) -> List[str]:
    req: List[str] = []
    seen: set = set()

    for _, arr in (ports or {}).items():
        if not isinstance(arr, list):
            continue
        for it in arr:
            if not isinstance(it, dict):
                continue
            cn = str(it.get("classname") or "").strip()
            ct = it.get("component_type")
            if not cn:
                continue
            if node_classname and cn == node_classname:
                continue

            prefix = _import_prefix_for_type(ct)
            if not prefix:
                continue

            key = (cn, prefix)
            if key in seen:
                continue
            seen.add(key)

            req.append(f'import {cn} from "{_alias_join(prefix, cn)}";')

    return req


def _apply_port_replacements(
    text: str,
    ports: Dict[str, Any],
) -> str:
    out = text or ""

    for port_key, arr in (ports or {}).items():
        if not isinstance(arr, list):
            continue

        classnames = []
        for it in arr:
            if not isinstance(it, dict):
                continue
            cn = str(it.get("classname") or "").strip()
            if cn:
                classnames.append(cn)

        if not classnames:
            continue

        suffix = str(port_key or "")
        if suffix.lower().startswith("in-"):
            suffix = suffix[3:]
        suffix = suffix.strip()
        if not suffix:
            continue

        replacement = "".join([f"<{cn}/>" for cn in classnames])

        pattern = re.compile(
            r"<\s*PlaceHolder-[^>]*" + re.escape(suffix) + r"\s*/\s*>",
            re.IGNORECASE,
        )
        out = pattern.sub(replacement, out)

    return out


def _ensure_out_dirs(out_dir: str) -> Dict[str, str]:
    base = Path(out_dir)
    utilities_dir = base / "utilities"
    contents_dir = base / "components" / "contents"
    layouts_dir = base / "components" / "layouts"
    auth_dir = base / "components" / "authentications"
    pages_dir = base / "components" / "pages"
    navs_dir = base / "components" / "navigations"

    utilities_dir.mkdir(parents=True, exist_ok=True)
    contents_dir.mkdir(parents=True, exist_ok=True)
    layouts_dir.mkdir(parents=True, exist_ok=True)
    auth_dir.mkdir(parents=True, exist_ok=True)
    pages_dir.mkdir(parents=True, exist_ok=True)
    navs_dir.mkdir(parents=True, exist_ok=True)

    return {
        "utilities": str(utilities_dir),
        "components/contents": str(contents_dir),
        "components/layouts": str(layouts_dir),
        "components/authentications": str(auth_dir),
        "components/pages": str(pages_dir),
        "components/navigations": str(navs_dir),
    }


def _default_metadata_path(workdir: str, funnel_id: str) -> Path:
    return Path(workdir) / str(funnel_id) / "in" / "metadata.json"


def _load_metadata(workdir: str, funnel_id: str, metadata_path: Optional[str]) -> Dict[str, Any]:
    p = Path(metadata_path) if metadata_path else _default_metadata_path(workdir, funnel_id)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_preview_from_node_id(comp_connections: Any) -> Optional[str]:
    target_comp_id = "00000000-0000-0000-0000-000000000001"
    target_to_node_id = "preview-node"

    if not isinstance(comp_connections, list):
        return None

    for c in comp_connections:
        if not isinstance(c, dict):
            continue
        to_comp = str(c.get("to_component_id") or "").strip().lower()
        to_node = str(c.get("to_node_id") or "").strip().lower()
        if to_comp == target_comp_id and to_node == target_to_node_id:
            from_node = str(c.get("from_node_id") or "").strip()
            return from_node or None

    return None


def _find_node_by_id(enriched_node_ids: Any, node_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(enriched_node_ids, list) or not node_id:
        return None
    for n in enriched_node_ids:
        if not isinstance(n, dict):
            continue
        if str(n.get("node_id") or "").strip() == node_id:
            return n
    return None


def _write_app_page(out_dir: str, classname: str, component_type: Any) -> Optional[str]:
    prefix = _import_prefix_for_type(component_type)
    if not prefix:
        return "unknown component_type for preview node"

    text = (
        f'import {classname} from "{_alias_join(prefix, classname)}";\n\n'
        "export default function Index() {\n"
        f"  return (<{classname}/>);\n"
        "}\n"
    )

    out_path = str(Path(out_dir) / "app" / "page.jsx")
    return _write_text(out_path, text)


def _flatten_utilities_lookup(utilities_lookup: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if isinstance(utilities_lookup, list):
        items.extend([x for x in utilities_lookup if isinstance(x, dict)])
        return items

    if isinstance(utilities_lookup, dict):
        for v in utilities_lookup.values():
            if isinstance(v, list):
                items.extend([x for x in v if isinstance(x, dict)])
            elif isinstance(v, dict):
                items.append(v)
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--funnel-id", required=False)
    ap.add_argument("--workdir", required=False, default=WORKDIR)
    ap.add_argument("--metadata-path", required=False)
    args = ap.parse_args()

    funnel_id = args.funnel_id or os.getenv("FUNNEL_ID") or "b4dc3740-44ba-43ea-86eb-10d55874b28f"

    if not funnel_id:
        raise SystemExit("Missing --funnel-id or FUNNEL_ID env var")

    meta = _load_metadata(args.workdir, funnel_id, args.metadata_path)

    in_dir = meta.get("in_dir") or str(Path(args.workdir) / str(funnel_id) / "in")
    out_dir = meta.get("out_dir") or str(Path(args.workdir) / str(funnel_id) / "output")

    dirs = _ensure_out_dirs(out_dir)

    utilities_lookup = meta.get("utilities_lookup") or {}
    enriched_node_ids = meta.get("enriched_node_ids") or []
    comp_connections = meta.get("comp_connections") or []

    gen_report: Dict[str, Any] = {
        "funnel_id": str(funnel_id),
        "in_dir": str(in_dir),
        "out_dir": str(out_dir),
        "generated": {"utilities": [], "components": [], "app": []},
        "warnings": [],
    }

    utilities_items = _flatten_utilities_lookup(utilities_lookup)

    for u in utilities_items:
        name = str(u.get("name") or "").strip()
        url = u.get("signed_file_link") or u.get("file_link")
        if not name:
            gen_report["warnings"].append({"loc": "fe_generation", "message": "utility missing name"})
            continue
        if not url:
            gen_report["warnings"].append(
                {"loc": "fe_generation", "message": f"utility missing signed_file_link: {name}"}
            )
            continue

        out_path = str(Path(dirs["utilities"]) / _safe_filename(name))
        err = _download_to_path(str(url), out_path)
        if err:
            gen_report["warnings"].append(
                {"loc": "fe_generation", "message": f"failed to download utility {name}: {err}"}
            )
            continue

        gen_report["generated"]["utilities"].append({"name": name, "path": out_path})

    for node in enriched_node_ids:
        if not isinstance(node, dict):
            continue

        warnings = node.get("warning") if isinstance(node.get("warning"), list) else []
        node["warning"] = warnings

        classname = str(node.get("classname") or "").strip()
        node_id = str(node.get("node_id") or "").strip()
        local_path = str(node.get("local_path") or "").strip()
        component_type = node.get("component_type")
        ports = node.get("ports") or {}
        utilities = node.get("utilities") or []

        if not classname:
            classname = _safe_filename(node_id) or "Component"

        rel_dir = _component_dir_for_type(component_type)
        if not rel_dir:
            warnings.append({"loc": "fe_generation", "message": f"unknown component_type for {classname}"})
            gen_report["warnings"].append(warnings[-1])
            continue

        out_path = str(Path(out_dir) / rel_dir / f"{classname}.jsx")

        src_text = _read_text(local_path)
        if not src_text:
            warnings.append(
                {"loc": "fe_generation", "message": f"missing source file for {classname}: {local_path}"}
            )
            gen_report["warnings"].append(warnings[-1])
            continue

        pre, import_block, tail = _split_import_block(src_text)

        import_block = _rewrite_relative_imports(import_block, utilities, ports, warnings)

        required_imports = _build_required_port_imports(classname, ports)
        import_block = _append_port_imports(import_block, required_imports)

        updated = pre + import_block + tail
        updated = _apply_port_replacements(updated, ports)

        err = _write_text(out_path, updated)
        if err:
            warnings.append({"loc": "fe_generation", "message": f"failed to write {out_path}: {err}"})
            gen_report["warnings"].append(warnings[-1])
            continue

        gen_report["generated"]["components"].append(
            {
                "classname": classname,
                "component_type": _normalize_component_type(component_type),
                "path": out_path,
            }
        )

        for w in warnings:
            gen_report["warnings"].append(w)

    preview_from_node_id = _find_preview_from_node_id(comp_connections)
    if not preview_from_node_id:
        gen_report["warnings"].append({"loc": "fe_generation", "message": "missing preview-node comp_connection"})
    else:
        preview_node = _find_node_by_id(enriched_node_ids, preview_from_node_id)
        if not preview_node:
            gen_report["warnings"].append(
                {
                    "loc": "fe_generation",
                    "message": f"preview from_node_id not found in enriched_node_ids: {preview_from_node_id}",
                }
            )
        else:
            preview_classname = str(preview_node.get("classname") or "").strip() or _safe_filename(preview_from_node_id)
            preview_component_type = preview_node.get("component_type")

            err = _write_app_page(out_dir, preview_classname, preview_component_type)
            if err:
                gen_report["warnings"].append({"loc": "fe_generation", "message": f"failed to write app/page.jsx: {err}"})
            else:
                gen_report["generated"]["app"].append(
                    {
                        "path": str(Path(out_dir) / "app" / "page.jsx"),
                        "classname": preview_classname,
                        "component_type": _normalize_component_type(preview_component_type),
                    }
                )

    report_path = str(Path(out_dir) / "generation_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(gen_report, f, indent=2, ensure_ascii=False, default=_json_default)

    print(json.dumps({"ok": True, "report_path": report_path}, ensure_ascii=False))


if __name__ == "__main__":
    main()

def run_step_2(funnel_id: str):

    meta = _load_metadata(WORKDIR, funnel_id, None)

    in_dir = meta.get("in_dir") or str(Path(WORKDIR) / funnel_id / "in")
    out_dir = meta.get("out_dir") or str(Path(WORKDIR) / funnel_id / "output")

    dirs = _ensure_out_dirs(out_dir)

    utilities_lookup = meta.get("utilities_lookup") or {}
    enriched_node_ids = meta.get("enriched_node_ids") or []
    comp_connections = meta.get("comp_connections") or []

    gen_report: Dict[str, Any] = {
        "funnel_id": str(funnel_id),
        "in_dir": str(in_dir),
        "out_dir": str(out_dir),
        "generated": {"utilities": [], "components": [], "app": []},
        "warnings": [],
    }

    utilities_items = _flatten_utilities_lookup(utilities_lookup)

    for u in utilities_items:
        name = str(u.get("name") or "").strip()
        url = u.get("signed_file_link") or u.get("file_link")
        if not name:
            gen_report["warnings"].append({"loc": "fe_generation", "message": "utility missing name"})
            continue
        if not url:
            gen_report["warnings"].append(
                {"loc": "fe_generation", "message": f"utility missing signed_file_link: {name}"}
            )
            continue

        out_path = str(Path(dirs["utilities"]) / _safe_filename(name))
        err = _download_to_path(str(url), out_path)
        if err:
            gen_report["warnings"].append(
                {"loc": "fe_generation", "message": f"failed to download utility {name}: {err}"}
            )
            continue

        gen_report["generated"]["utilities"].append({"name": name, "path": out_path})

    for node in enriched_node_ids:
        if not isinstance(node, dict):
            continue

        warnings = node.get("warning") if isinstance(node.get("warning"), list) else []
        node["warning"] = warnings

        classname = str(node.get("classname") or "").strip()
        node_id = str(node.get("node_id") or "").strip()
        local_path = str(node.get("local_path") or "").strip()
        component_type = node.get("component_type")
        ports = node.get("ports") or {}
        utilities = node.get("utilities") or []

        if not classname:
            classname = _safe_filename(node_id) or "Component"

        rel_dir = _component_dir_for_type(component_type)
        if not rel_dir:
            warnings.append({"loc": "fe_generation", "message": f"unknown component_type for {classname}"})
            gen_report["warnings"].append(warnings[-1])
            continue

        out_path = str(Path(out_dir) / rel_dir / f"{classname}.jsx")

        src_text = _read_text(local_path)
        if not src_text:
            warnings.append(
                {"loc": "fe_generation", "message": f"missing source file for {classname}: {local_path}"}
            )
            gen_report["warnings"].append(warnings[-1])
            continue

        pre, import_block, tail = _split_import_block(src_text)

        import_block = _rewrite_relative_imports(import_block, utilities, ports, warnings)

        required_imports = _build_required_port_imports(classname, ports)
        import_block = _append_port_imports(import_block, required_imports)

        updated = pre + import_block + tail
        updated = _apply_port_replacements(updated, ports)

        err = _write_text(out_path, updated)
        if err:
            warnings.append({"loc": "fe_generation", "message": f"failed to write {out_path}: {err}"})
            gen_report["warnings"].append(warnings[-1])
            continue

        gen_report["generated"]["components"].append(
            {
                "classname": classname,
                "component_type": _normalize_component_type(component_type),
                "path": out_path,
            }
        )

        for w in warnings:
            gen_report["warnings"].append(w)

    preview_from_node_id = _find_preview_from_node_id(comp_connections)
    if not preview_from_node_id:
        gen_report["warnings"].append({"loc": "fe_generation", "message": "missing preview-node comp_connection"})
    else:
        preview_node = _find_node_by_id(enriched_node_ids, preview_from_node_id)
        if not preview_node:
            gen_report["warnings"].append(
                {
                    "loc": "fe_generation",
                    "message": f"preview from_node_id not found in enriched_node_ids: {preview_from_node_id}",
                }
            )
        else:
            preview_classname = str(preview_node.get("classname") or "").strip() or _safe_filename(preview_from_node_id)
            preview_component_type = preview_node.get("component_type")

            err = _write_app_page(out_dir, preview_classname, preview_component_type)
            if err:
                gen_report["warnings"].append(
                    {"loc": "fe_generation", "message": f"failed to write app/page.jsx: {err}"})
            else:
                gen_report["generated"]["app"].append(
                    {
                        "path": str(Path(out_dir) / "app" / "page.jsx"),
                        "classname": preview_classname,
                        "component_type": _normalize_component_type(preview_component_type),
                    }
                )

    report_path = str(Path(out_dir) / "generation_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(gen_report, f, indent=2, ensure_ascii=False, default=_json_default)

    print(json.dumps({"ok": True, "report_path": report_path}, ensure_ascii=False))

