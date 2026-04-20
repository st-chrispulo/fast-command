from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from auth.db import SessionLocal
from commands.base_command import BaseCommand
from models.components.tbl_comp_connections import CompConnection
from integrations.kafka.publisher import publish_generate_fe

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("components.connections.delete")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


def _extract_funnel_ids(rows: List[CompConnection]) -> Set[str]:
    out: Set[str] = set()
    for r in rows:
        fid = getattr(r, "funnel_id", None)
        if fid is not None:
            out.add(str(fid))
    return out


def _build_generate_fe_delete_events(rows: List[CompConnection]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[CompConnection]] = defaultdict(list)

    for row in rows:
        funnel_id = str(getattr(row, "funnel_id", "") or "").strip()
        if not funnel_id:
            continue
        grouped[funnel_id].append(row)

    events: List[Dict[str, Any]] = []

    for funnel_id, group_rows in grouped.items():
        connection_ids = [str(r.id) for r in group_rows if getattr(r, "id", None) is not None]

        events.append(
            {
                "funnel_id": funnel_id,
                "metadata": {
                    "event": "connection_deleted",
                    "connection_ids": connection_ids,
                    "deleted_count": len(connection_ids),
                },
            }
        )

    return events

class DeleteCompConnectionsPayload(BaseModel):
    ids: List[str] = Field(description="Connection ids to delete")

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, v: List[str]) -> List[str]:
        if not isinstance(v, list):
            raise ValueError("ids must be a list")

        cleaned = [str(x).strip() for x in v if str(x or "").strip()]
        if not cleaned:
            raise ValueError("ids cannot be empty")

        seen = set()
        uniq: List[str] = []
        for x in cleaned:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq


class DeleteCompConnectionsCommand(BaseCommand):
    name = "components/connections/delete"
    schema = DeleteCompConnectionsPayload
    require_auth = True
    method = "delete"
    type = "json"
    group = "Funnel"

    async def execute(self, payload: DeleteCompConnectionsPayload, user_id: Optional[str] = None) -> dict:
        t0 = time.monotonic()
        logger.info("[connections] delete start ids=%s user_id=%s", payload.ids, user_id)

        if self.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized (no user context).")

        db = SessionLocal()
        try:
            rows = db.query(CompConnection).filter(CompConnection.id.in_(payload.ids)).all()
            if not rows:
                raise HTTPException(status_code=404, detail="No matching connections found")

            found_ids = {str(r.id) for r in rows}
            not_found = [i for i in payload.ids if i not in found_ids]
            funnel_ids = sorted(_extract_funnel_ids(rows))
            kafka_messages = _build_generate_fe_delete_events(rows)

            for r in rows:
                db.delete(r)
            db.commit()

            kafka_published = 0
            for funnel_id, ids in grouped_ids_by_funnel.items():
                ok = publish_generate_fe(
                    funnel_id=funnel_id,
                    metadata={
                        "event": "connection_deleted",
                        "connection_ids": ids,
                        "deleted_count": len(ids),
                    },
                )
                if ok:
                    kafka_published += 1
            return {
                "status": "ok",
                "deleted": True,
                "deleted_count": len(found_ids),
                "ids_deleted": ids_deleted,
                "not_found": not_found,
                "funnel_ids": funnel_ids,
                "kafka_queued": kafka_published > 0,
                "kafka_published_count": kafka_published,
            }
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("[connections] delete failed")
            raise HTTPException(status_code=500, detail="Failed to delete connections")
        finally:
            db.close()


__all__ = ["DeleteCompConnectionsCommand"]