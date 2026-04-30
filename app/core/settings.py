from __future__ import annotations

import json
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from utils.env import bootstrap_environment

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("app.core.settings")
except Exception:
    import logging

    logger = logging.getLogger(__name__)

ENV_FILES = bootstrap_environment()


def _split_csv(value: str) -> List[str]:
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def _parse_list(value: str, default: List[str]) -> List[str]:
    s = str(value or "").strip()
    if not s:
        return default
    if s == "*":
        return ["*"]
    if s.startswith("["):
        try:
            parsed = json.loads(s)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            out = [str(x).strip() for x in parsed if str(x).strip()]
            return out or default
    out = _split_csv(s)
    return out or default


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SOLITUD_",
        case_sensitive=False,
        env_ignore_empty=True,
        extra="ignore",
    )

    app_name: str = Field(default="Fast Command API")
    app_description: str = Field(default="Execute dynamic business commands via FastAPI.")
    app_version: str = Field(default="1.0.0")

    cors_origins: str = Field(default="http://localhost:3000")
    cors_allow_credentials: bool = Field(default=True)
    cors_allow_methods: str = Field(default="*")
    cors_allow_headers: str = Field(default="*")

    trusted_hosts: str = Field(default="localhost,127.0.0.1,*.solitud.dev")

    gzip_minimum_size: int = Field(default=1000)
    static_enabled: bool = Field(default=True)

    internal_auth_enabled: bool = Field(default=True)
    internal_client_id: str = Field(default="")
    internal_client_secret: str = Field(default="")
    internal_auth_secret: str = Field(default="")
    internal_token_expire_seconds: int = Field(default=300)

    socket_room_join_token_secret: str = Field(default="")
    socket_server_stale_seconds: int = Field(default=60)

    kafka_enabled: bool = Field(default=True)
    kafka_bootstrap_servers: str = Field(default="kafka:9092")
    kafka_client_id: str = Field(default="solitud-api")
    kafka_generate_fe_topic: str = Field(default="generate_fe")
    kafka_generate_fe_group_id: str = Field(default="generate_fe_consumer")
    kafka_auto_offset_reset: str = Field(default="earliest")
    kafka_poll_timeout_seconds: float = Field(default=1.0)
    kafka_retry_count: int = Field(default=3)
    kafka_retry_delay_seconds: float = Field(default=2.0)

    @property
    def cors_origins_list(self) -> List[str]:
        return _parse_list(self.cors_origins, ["http://localhost:3000"])

    @property
    def cors_allow_methods_list(self) -> List[str]:
        return _parse_list(self.cors_allow_methods, ["*"])

    @property
    def cors_allow_headers_list(self) -> List[str]:
        return _parse_list(self.cors_allow_headers, ["*"])

    @property
    def trusted_hosts_list(self) -> List[str]:
        return _parse_list(self.trusted_hosts, ["localhost", "127.0.0.1", "*.solitud.dev"])

    @property
    def kafka_bootstrap_servers_list(self) -> List[str]:
        return _parse_list(self.kafka_bootstrap_servers, ["kafka:9092"])


settings = Settings()

logger.info(
    "settings loaded env_files=%s cors_origins=%s trusted_hosts=%s kafka_bootstrap_servers=%s kafka_generate_fe_topic=%s",
    ",".join(ENV_FILES) or "<none>",
    settings.cors_origins_list,
    settings.trusted_hosts_list,
    settings.kafka_bootstrap_servers_list,
    settings.kafka_generate_fe_topic,
)

__all__ = ["Settings", "settings"]
