from __future__ import annotations

import json
from typing import Any, Dict, Optional

try:
    from logger import logger as _app_logger
    logger = _app_logger.getChild("integrations.kafka.publisher")
except Exception:
    import logging
    logger = logging.getLogger(__name__)

from app.core.settings import settings

_producer = None


def _delivery_cb(err, msg) -> None:
    if err is not None:
        logger.error(
            "kafka delivery failed topic=%s err=%s",
            getattr(msg, "topic", lambda: "?")(),
            str(err),
        )


def _get_producer():
    global _producer
    if _producer is not None:
        return _producer

    try:
        from confluent_kafka import Producer
    except Exception:
        logger.exception("confluent_kafka not installed")
        return None

    conf = {
        "bootstrap.servers": ",".join(settings.kafka_bootstrap_servers_list),
        "client.id": settings.kafka_client_id,
        "socket.timeout.ms": 2000,
        "message.timeout.ms": 3000,
        "queue.buffering.max.messages": 100000,
        "queue.buffering.max.kbytes": 102400,
    }

    _producer = Producer(conf)
    return _producer


def publish_generate_fe(*, funnel_id: str, metadata: Dict[str, Any]) -> bool:
    if not settings.kafka_enabled:
        return False

    producer = _get_producer()
    if producer is None:
        return False

    payload = {"funnel_id": funnel_id, "metadata": metadata}

    try:
        producer.poll(0)

        producer.produce(
            settings.kafka_generate_fe_topic,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            on_delivery=_delivery_cb,
        )
        return True
    except BufferError:
        logger.warning("kafka queue full, dropping message topic=%s funnel_id=%s", settings.kafka_generate_fe_topic, funnel_id)
        return False
    except Exception:
        logger.exception("kafka produce failed topic=%s funnel_id=%s", settings.kafka_generate_fe_topic, funnel_id)
        return False