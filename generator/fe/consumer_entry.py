from __future__ import annotations

from typing import Any, Dict
from generator.fe.generate_fe_step_1 import run_step_1
from generator.fe.generate_fe_step_2 import run_step_2

try:
    from logger import logger as _app_logger
    logger = _app_logger.getChild("generator.fe.consumer_entry")
except Exception:
    import logging
    logger = logging.getLogger(__name__)


def run_generate_fe(*, funnel_id: str, metadata: Dict[str, Any]) -> None:
    logger.info("run_generate_fe invoked", extra={"funnel_id": funnel_id})

    run_step_1(funnel_id=funnel_id)
    run_step_2(funnel_id=funnel_id)