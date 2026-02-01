# testing.py
# Run: python testing.py

import json
from sqlalchemy.orm import Session

from auth.db import SessionLocal
from models.components.tbl_comp_connections import CompConnection

FUNNEL_ID = "70577ad1-d90c-400a-ac91-bd509b020041"
LIMIT = 200


def to_comp_connection_dict(r: CompConnection) -> dict:
    # keep only what you need; adjust keys if your model differs
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


def main():
    with SessionLocal() as session:  # type: Session
        rows = (
            session.query(CompConnection)
            .filter(CompConnection.funnel_id == FUNNEL_ID)
            .order_by(CompConnection.created_at.desc())
            .limit(LIMIT)
            .all()
        )

    comp_connections = [to_comp_connection_dict(r) for r in rows]

    # 1) Build node_ids (dedup, preserve order)
    seen = set()
    node_ids = []
    for c in comp_connections:
        for nid in (c.get("from_node_id"), c.get("to_node_id")):
            if not nid:
                continue
            if nid not in seen:
                seen.add(nid)
                node_ids.append(nid)

    # 2) Enrich: for each node_id, find first matching side then break
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

    out = {
        "comp_connections": comp_connections,
        "node_ids": node_ids,
        "enriched_node_ids": enriched_node_ids,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
