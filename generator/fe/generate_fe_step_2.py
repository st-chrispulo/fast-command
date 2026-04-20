import argparse
import json
import os
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from logger import logger as _app_logger
    logger = _app_logger.getChild("components.authentications.update_with_uploads")
except Exception:
    import logging
    logger = logging.getLogger(__name__)

WORKDIR = os.getenv("WORKDIR", "FE_Workbench")
IMPORT_ALIAS = os.getenv("IMPORT_ALIAS", "@/").strip()

_COMPONENT_TYPE_DIR_MAP = {
    "content": "components/contents",
    "layout": "components/layouts",
    "authentication": "components/authentications",
    "page": "components/pages",
    "navigation": "components/navigations",
}

_COMPONENT_TYPE_IMPORT_MAP = {
    "content": "contents",
    "layout": "layouts",
    "authentication": "authentications",
    "page": "pages",
    "navigation": "navigations",
}

_COMPONENT_TYPE_ALIAS_MAP = {
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

_IMPORT_FROM_REL_RE = re.compile(r'(from\s+["\'])\./([^"\']+)(["\'])', re.MULTILINE)
_IMPORT_SIDE_EFFECT_REL_RE = re.compile(r'^(import\s+)(["\'])\./([^"\']+)(\2\s*;?\s*)$', re.MULTILINE)
_PLACEHOLDER_TAG_RE = re.compile(r"<\s*(PlaceHolder-[A-Za-z0-9_-]+)\s*/\s*>", re.IGNORECASE)
_PLACEHOLDER_BLOCK_RE = re.compile(
    r"<\s*(PlaceHolder-[A-Za-z0-9_-]+)\s*>\s*</\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)

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


def _alias_join(*parts: str) -> str:
    base = IMPORT_ALIAS
    if base and not base.endswith("/"):
        base += "/"
    joined = "/".join(str(x).strip("/").strip() for x in parts if str(x or "").strip())
    return f"{base}{joined}" if base else joined


def _json_default(o: Any) -> Any:
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    return str(o)


def _normalize_component_type(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().lower()
    s = s.replace("tbl_", "").replace("comp_", "").replace("components.", "")
    s = s.replace("-", "_").replace(" ", "_")
    if s.endswith("s"):
        s = s[:-1]
    return _COMPONENT_TYPE_ALIAS_MAP.get(s, s)


def _component_dir_for_type(component_type: Any) -> Optional[str]:
    return _COMPONENT_TYPE_DIR_MAP.get(_normalize_component_type(component_type))


def _import_prefix_for_type(component_type: Any) -> Optional[str]:
    return _COMPONENT_TYPE_IMPORT_MAP.get(_normalize_component_type(component_type))


def _safe_filename(s: str) -> str:
    value = str(s or "").strip()
    if not value:
        return "file"
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return value or "file"


def _read_text(path: str) -> str:
    try:
        raw = Path(path).read_bytes()
        try:
            return raw.decode("utf-8")
        except Exception:
            return raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _write_text(path: str, text: str) -> Optional[str]:
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")
        return None
    except Exception as e:
        return str(e)


def _build_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.75,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _download_to_path(
    session: requests.Session,
    url: str,
    path: str,
    timeout_seconds: int = 60,
) -> Optional[str]:
    if not url:
        return "missing signed_file_link"

    try:
        with session.get(url, stream=True, timeout=timeout_seconds) as response:
            response.raise_for_status()
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
        return None
    except Exception as e:
        return str(e)


def _is_blank_line(line: str) -> bool:
    return not (line or "").strip()


def _is_comment_line(line: str) -> bool:
    stripped = (line or "").lstrip()
    return (
        stripped.startswith("//")
        or stripped.startswith("/*")
        or stripped.startswith("*")
        or stripped.startswith("*/")
    )


def _is_directive_line(line: str) -> bool:
    stripped = (line or "").strip()
    return stripped in ('"use client";', "'use client';", '"use server";', "'use server';")


def _split_import_block(text: str) -> Tuple[str, str, str]:
    lines = (text or "").splitlines(True)
    pre: List[str] = []
    imports: List[str] = []

    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]
        if line.lstrip().startswith("import "):
            break
        pre.append(line)
        index += 1

    started = False
    while index < total:
        line = lines[index]
        stripped = line.lstrip()
        if stripped.startswith("import "):
            started = True
            imports.append(line)
            index += 1
            continue
        if started and (_is_blank_line(line) or _is_comment_line(line)):
            imports.append(line)
            index += 1
            continue
        break

    tail = lines[index:]
    return "".join(pre), "".join(imports), "".join(tail)


def _strip_ext(name: str) -> str:
    s = str(name or "").strip()
    for ext in (".jsx", ".js", ".tsx", ".ts"):
        if s.lower().endswith(ext):
            return s[: -len(ext)]
    return s


def _resolve_utility_name(import_target: str, utilities: List[str]) -> Optional[str]:
    target = _strip_ext(import_target)
    if not target:
        return None

    normalized = [_strip_ext(str(x or "").strip()) for x in utilities if str(x or "").strip()]
    if not normalized:
        return None

    target_lower = target.lower()

    for item in normalized:
        if item.lower() == target_lower:
            return item

    for item in normalized:
        if item.lower().startswith(target_lower):
            return item

    return None


def _build_port_classname_type_map(ports: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for arr in (ports or {}).values():
        if not isinstance(arr, list):
            continue
        for item in arr:
            if not isinstance(item, dict):
                continue
            classname = str(item.get("classname") or "").strip()
            if classname and classname not in result:
                result[classname] = item.get("component_type")
    return result


def _has_asset_ext(path: str) -> bool:
    return Path(str(path or "").strip()).suffix.lower() in _ASSET_EXTS


def _rewrite_relative_imports(
    import_block: str,
    utilities: List[str],
    ports: Dict[str, Any],
    warnings: List[Dict[str, Any]],
) -> str:
    port_map = _build_port_classname_type_map(ports)

    def resolve_new_path(rel_path: str) -> Optional[str]:
        if not rel_path or _has_asset_ext(rel_path):
            return None

        base_name = _strip_ext(rel_path.split("/")[-1])

        utility_name = _resolve_utility_name(base_name, utilities)
        if utility_name:
            return _alias_join("utilities", utility_name)

        component_type = port_map.get(base_name)
        if base_name in port_map:
            prefix = _import_prefix_for_type(component_type)
            if prefix:
                return _alias_join(prefix, base_name)
            warnings.append(
                {"loc": "fe_generation", "message": f"unknown component_type for port import: {base_name}"}
            )

        return None

    def replace_from(match: re.Match) -> str:
        new_path = resolve_new_path(match.group(2))
        if not new_path:
            return match.group(0)
        return f'{match.group(1)}{new_path}{match.group(3)}'

    def replace_side_effect(match: re.Match) -> str:
        new_path = resolve_new_path(match.group(3))
        if not new_path:
            return match.group(0)
        return f'{match.group(1)}{match.group(2)}{new_path}{match.group(2)}{match.group(4)}'

    updated = _IMPORT_FROM_REL_RE.sub(replace_from, import_block or "")
    updated = _IMPORT_SIDE_EFFECT_REL_RE.sub(replace_side_effect, updated)
    return updated


def _existing_import_lines(import_block: str) -> Set[str]:
    return {
        line.strip()
        for line in (import_block or "").splitlines()
        if line.strip().startswith("import ")
    }


def _append_port_imports(import_block: str, required: List[str]) -> str:
    existing = _existing_import_lines(import_block)
    additions = [line for line in required if line.strip() and line.strip() not in existing]
    if not additions:
        return import_block

    output = import_block or ""
    if output and not output.endswith("\n"):
        output += "\n"
    if output and not output.endswith("\n\n"):
        output += "\n"
    output += "\n".join(additions) + "\n"
    return output


def _normalize_port_suffix(port_key: str) -> str:
    suffix = str(port_key or "").strip()
    if suffix.lower().startswith("in-"):
        suffix = suffix[3:]
    return suffix.strip()


def _build_port_replacement_specs(
    ports: Dict[str, Any],
) -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []

    for port_key, arr in (ports or {}).items():
        if not isinstance(arr, list):
            continue

        suffix = _normalize_port_suffix(port_key)
        if not suffix:
            continue

        classnames: List[str] = []
        seen: Set[str] = set()

        for item in arr:
            if not isinstance(item, dict):
                continue
            classname = str(item.get("classname") or "").strip()
            if classname and classname not in seen:
                seen.add(classname)
                classnames.append(classname)

        if not classnames:
            continue

        specs.append(
            {
                "suffix": suffix,
                "classnames": classnames,
                "markup": "".join(f"<{classname}/>" for classname in classnames),
            }
        )

    specs.sort(key=lambda item: len(item["suffix"]), reverse=True)
    return specs


def _replace_placeholders_and_collect_used(
    text: str,
    ports: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    specs = _build_port_replacement_specs(ports)
    used_components: Dict[str, Any] = {}

    if not text:
        return "", used_components

    def replace_self_closing(match: re.Match) -> str:
        tag_name = str(match.group(1) or "").strip()
        suffix_match = None

        for spec in specs:
            if tag_name.lower().endswith(spec["suffix"].lower()):
                suffix_match = spec
                break

        if not suffix_match:
            return ""

        for classname in suffix_match["classnames"]:
            used_components[classname] = True

        return suffix_match["markup"]

    updated = _PLACEHOLDER_TAG_RE.sub(replace_self_closing, text)
    updated = _PLACEHOLDER_BLOCK_RE.sub("", updated)
    updated = _PLACEHOLDER_TAG_RE.sub("", updated)

    return updated, used_components


def _build_required_port_imports(
    used_classnames: Set[str],
    ports: Dict[str, Any],
) -> List[str]:
    if not used_classnames:
        return []

    port_map = _build_port_classname_type_map(ports)
    required: List[str] = []

    for classname in sorted(used_classnames):
        prefix = _import_prefix_for_type(port_map.get(classname))
        if not prefix:
            continue
        required.append(f'import {classname} from "{_alias_join(prefix, classname)}";')

    return required


def _ensure_out_dirs(out_dir: str) -> Dict[str, str]:
    base = Path(out_dir)
    paths = {
        "utilities": base / "utilities",
        "components/contents": base / "components" / "contents",
        "components/layouts": base / "components" / "layouts",
        "components/authentications": base / "components" / "authentications",
        "components/pages": base / "components" / "pages",
        "components/navigations": base / "components" / "navigations",
    }

    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)

    return {key: str(value) for key, value in paths.items()}


def _default_metadata_path(workdir: str, funnel_id: str) -> Path:
    return Path(workdir) / str(funnel_id) / "in" / "metadata.json"


def _load_metadata(workdir: str, funnel_id: str, metadata_path: Optional[str]) -> Dict[str, Any]:
    path = Path(metadata_path) if metadata_path else _default_metadata_path(workdir, funnel_id)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _find_preview_from_node_id(comp_connections: Any) -> Optional[str]:
    target_comp_id = "00000000-0000-0000-0000-000000000001"
    target_to_node_id = "preview-node"

    if not isinstance(comp_connections, list):
        return None

    for item in comp_connections:
        if not isinstance(item, dict):
            continue
        to_comp = str(item.get("to_component_id") or "").strip().lower()
        to_node = str(item.get("to_node_id") or "").strip().lower()
        if to_comp == target_comp_id and to_node == target_to_node_id:
            from_node = str(item.get("from_node_id") or "").strip()
            if from_node:
                return from_node

    return None


def _find_node_by_id(enriched_node_ids: Any, node_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(enriched_node_ids, list) or not node_id:
        return None

    for node in enriched_node_ids:
        if not isinstance(node, dict):
            continue
        if str(node.get("node_id") or "").strip() == node_id:
            return node

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
    return _write_text(str(Path(out_dir) / "app" / "page.jsx"), text)


def _flatten_utilities_lookup(utilities_lookup: Any) -> List[Dict[str, Any]]:
    if isinstance(utilities_lookup, list):
        return [item for item in utilities_lookup if isinstance(item, dict)]

    items: List[Dict[str, Any]] = []
    if isinstance(utilities_lookup, dict):
        for value in utilities_lookup.values():
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
            elif isinstance(value, dict):
                items.append(value)

    return items


def _dedupe_warning_list(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Set[str] = set()
    result: List[Dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        key = json.dumps(item, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)

    return result


def _generate_frontend(
    funnel_id: str,
    workdir: str,
    metadata_path: Optional[str] = None,
) -> Dict[str, Any]:
    if not funnel_id:
        raise ValueError("Missing funnel_id")

    meta = _load_metadata(workdir, funnel_id, metadata_path)

    in_dir = meta.get("in_dir") or str(Path(workdir) / str(funnel_id) / "in")
    out_dir = meta.get("out_dir") or str(Path(workdir) / str(funnel_id) / "output")
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

    with _build_http_session() as session:
        for utility in utilities_items:
            name = str(utility.get("name") or "").strip()
            url = utility.get("signed_file_link") or utility.get("file_link")

            if not name:
                gen_report["warnings"].append({"loc": "fe_generation", "message": "utility missing name"})
                continue

            if not url:
                gen_report["warnings"].append(
                    {"loc": "fe_generation", "message": f"utility missing signed_file_link: {name}"}
                )
                continue

            out_path = str(Path(dirs["utilities"]) / _safe_filename(name))
            error = _download_to_path(session, str(url), out_path)

            if error:
                gen_report["warnings"].append(
                    {"loc": "fe_generation", "message": f"failed to download utility {name}: {error}"}
                )
                continue

            gen_report["generated"]["utilities"].append({"name": name, "path": out_path})

        for node in enriched_node_ids:
            if not isinstance(node, dict):
                continue

            node_warnings = list(node.get("warning") or []) if isinstance(node.get("warning"), list) else []

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
                node_warnings.append(
                    {"loc": "fe_generation", "message": f"unknown component_type for {classname}"}
                )
                gen_report["warnings"].extend(node_warnings)
                continue

            source_text = _read_text(local_path)
            if not source_text:
                node_warnings.append(
                    {"loc": "fe_generation", "message": f"missing source file for {classname}: {local_path}"}
                )
                gen_report["warnings"].extend(node_warnings)
                continue

            pre, import_block, tail = _split_import_block(source_text)
            import_block = _rewrite_relative_imports(import_block, utilities, ports, node_warnings)

            replaced_tail, used_components = _replace_placeholders_and_collect_used(tail, ports)
            required_imports = _build_required_port_imports(set(used_components.keys()), ports)
            import_block = _append_port_imports(import_block, required_imports)

            updated_text = pre + import_block + replaced_tail
            out_path = str(Path(out_dir) / rel_dir / f"{classname}.jsx")

            error = _write_text(out_path, updated_text)
            if error:
                node_warnings.append({"loc": "fe_generation", "message": f"failed to write {out_path}: {error}"})
                gen_report["warnings"].extend(node_warnings)
                continue

            gen_report["generated"]["components"].append(
                {
                    "classname": classname,
                    "component_type": _normalize_component_type(component_type),
                    "path": out_path,
                }
            )
            gen_report["warnings"].extend(node_warnings)

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

            error = _write_app_page(out_dir, preview_classname, preview_component_type)
            if error:
                gen_report["warnings"].append(
                    {"loc": "fe_generation", "message": f"failed to write app/page.jsx: {error}"}
                )
            else:
                gen_report["generated"]["app"].append(
                    {
                        "path": str(Path(out_dir) / "app" / "page.jsx"),
                        "classname": preview_classname,
                        "component_type": _normalize_component_type(preview_component_type),
                    }
                )

    gen_report["warnings"] = _dedupe_warning_list(gen_report["warnings"])

    report_path = str(Path(out_dir) / "generation_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(gen_report, f, indent=2, ensure_ascii=False, default=_json_default)

    return {"ok": True, "report_path": report_path}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--funnel-id", required=False)
    ap.add_argument("--workdir", required=False, default=WORKDIR)
    ap.add_argument("--metadata-path", required=False)
    args = ap.parse_args()

    funnel_id = args.funnel_id or os.getenv("FUNNEL_ID") or "b4dc3740-44ba-43ea-86eb-10d55874b28f"
    if not funnel_id:
        raise SystemExit("Missing --funnel-id or FUNNEL_ID env var")

    result = _generate_frontend(
        funnel_id=str(funnel_id),
        workdir=str(args.workdir),
        metadata_path=args.metadata_path,
    )
    print(json.dumps(result, ensure_ascii=False))


def run_step_2(funnel_id: str) -> None:
    result = _generate_frontend(
        funnel_id=str(funnel_id),
        workdir=WORKDIR,
        metadata_path=None,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()