# testing.py
# Run: python testing.py

import json
from uuid import UUID

from sqlalchemy.orm import Session

from auth.db import SessionLocal
from models.components.tbl_comp_connections import CompConnection

from models.components.tbl_comp_contents import CompContent
from models.components.tbl_comp_layouts import CompLayout
from models.components.tbl_comp_pages import CompPage
from models.components.tbl_comp_authentications import CompAuthentication
from models.components.tbl_comp_navigations import CompNavigation

from integrations.gcs.gcs import get_gcs

FUNNEL_ID = "d8c5b7a9-0d38-4521-819a-3a24b31cdb52"
LIMIT = 200


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


def _bulk_fetch_file_links(session: Session, items):
    by_type = {}
    for it in items:
        cid = it.get("component_id")
        ctype = it.get("component_type")
        ctype_norm = _normalize_component_type(ctype)
        uid = _safe_uuid(cid)
        if not uid or not ctype_norm:
            continue
        by_type.setdefault(ctype_norm, set()).add(uid)

    file_link_by_id = {}

    for ctype_norm, ids in by_type.items():
        model = _get_model_for_type(ctype_norm)
        if not model or not ids:
            continue

        q = session.query(model.id, getattr(model, "file_link")).filter(model.id.in_(list(ids)))
        for rid, file_link in q.all():
            file_link_by_id[str(rid)] = file_link

    return file_link_by_id


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


def main():
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

        enriched_node_ids = []
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

        file_link_by_id = _bulk_fetch_file_links(session, enriched_node_ids)

        for it in enriched_node_ids:
            cid = it.get("component_id")
            file_link = file_link_by_id.get(str(cid)) if cid else None
            it["file_link"] = file_link
            it["signed_file_link"] = _signed_url_for_key(gcs, file_link, expires_seconds=900)

    out = {
        "comp_connections": comp_connections,
        "node_ids": node_ids,
        "enriched_node_ids": enriched_node_ids,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
