from __future__ import annotations

import json
import signal
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from confluent_kafka import Consumer, KafkaError, KafkaException

try:
    from logger import logger as _app_logger
    logger = _app_logger.getChild("consumers.generate_fe")
except Exception:
    import logging
    logger = logging.getLogger(__name__)

from app.core.settings import settings


@dataclass(slots=True)
class GenerateFEEvent:
    funnel_id: str
    metadata: Dict[str, Any]


def _parse_event(raw: str) -> GenerateFEEvent:
    payload = json.loads(raw)

    if not isinstance(payload, dict):
        raise ValueError("Kafka payload must be a JSON object")

    funnel_id = str(payload.get("funnel_id") or "").strip()
    if not funnel_id:
        raise ValueError("Missing funnel_id")

    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")

    return GenerateFEEvent(
        funnel_id=funnel_id,
        metadata=metadata,
    )


def _run_generate_fe(event: GenerateFEEvent) -> None:
    from generator.fe.consumer_entry import run_generate_fe

    run_generate_fe(
        funnel_id=event.funnel_id,
        metadata=event.metadata,
    )


def _process_with_retry(event: GenerateFEEvent) -> None:
    last_error: Optional[Exception] = None

    for attempt in range(1, settings.kafka_retry_count + 1):
        try:
            _run_generate_fe(event)
            logger.info(
                "generate_fe completed",
                extra={
                    "funnel_id": event.funnel_id,
                    "attempt": attempt,
                },
            )
            return
        except Exception as exc:
            last_error = exc
            logger.exception(
                "generate_fe failed",
                extra={
                    "funnel_id": event.funnel_id,
                    "attempt": attempt,
                },
            )
            if attempt < settings.kafka_retry_count:
                time.sleep(settings.kafka_retry_delay_seconds)

    if last_error:
        raise last_error


class GenerateFEConsumer:
    def __init__(self) -> None:
        self._running = True
        self._consumer = Consumer(
            {
                "bootstrap.servers": ",".join(settings.kafka_bootstrap_servers_list),
                "group.id": settings.kafka_generate_fe_group_id,
                "client.id": settings.kafka_client_id,
                "auto.offset.reset": settings.kafka_auto_offset_reset,
                "enable.auto.commit": False,
            }
        )

    def stop(self, *_args: Any) -> None:
        self._running = False

    def start(self) -> None:
        if not settings.kafka_enabled:
            logger.warning("kafka disabled, generate_fe consumer will not start")
            return

        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        self._consumer.subscribe([settings.kafka_generate_fe_topic])

        logger.info(
            "generate_fe consumer started",
            extra={
                "topic": settings.kafka_generate_fe_topic,
                "group_id": settings.kafka_generate_fe_group_id,
                "bootstrap_servers": settings.kafka_bootstrap_servers_list,
            },
        )

        try:
            while self._running:
                msg = self._consumer.poll(settings.kafka_poll_timeout_seconds)

                if msg is None:
                    continue

                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    raise KafkaException(msg.error())

                raw_value = msg.value()
                if raw_value is None:
                    logger.warning("empty kafka message received")
                    self._consumer.commit(message=msg)
                    continue

                try:
                    event = _parse_event(raw_value.decode("utf-8"))
                    _process_with_retry(event)
                    self._consumer.commit(message=msg)
                except Exception:
                    logger.exception("message processing failed")
        finally:
            self._consumer.close()
            logger.info("generate_fe consumer stopped")


def main() -> None:
    consumer = GenerateFEConsumer()
    consumer.start()


if __name__ == "__main__":
    main()