from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("app.core.settings")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


def _env_file() -> str:
    root = Path(__file__).resolve().parents[2]
    return str(root / ".env")


def _split_csv(value: str) -> List[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SOLITUD_",
        case_sensitive=False,
        env_file=_env_file(),
        env_ignore_empty=True,
        extra="ignore",
    )

    app_name: str = Field(default="Fast Command API")
    app_description: str = Field(default="Execute dynamic business commands via FastAPI.")
    app_version: str = Field(default="1.0.0")

    cors_origins: List[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    cors_allow_credentials: bool = Field(default=True)
    cors_allow_methods: List[str] = Field(default_factory=lambda: ["*"])
    cors_allow_headers: List[str] = Field(default_factory=lambda: ["*"])

    trusted_hosts: List[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])

    gzip_minimum_size: int = Field(default=1000)
    static_enabled: bool = Field(default=True)

    internal_auth_enabled: bool = Field(default=True)
    internal_client_id: str = Field(default="")
    internal_client_secret: str = Field(default="")
    internal_auth_secret: str = Field(default="")
    internal_token_expire_seconds: int = Field(default=300)

    socket_room_join_token_secret: str = Field(default="")
    socket_server_stale_seconds: int = Field(default=60)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v):
        if v is None:
            return ["http://localhost:3000"]
        if isinstance(v, list):
            out = [str(x).strip() for x in v if str(x).strip()]
            return out or ["http://localhost:3000"]
        s = str(v).strip()
        if not s:
            return ["http://localhost:3000"]
        if s == "*":
            return ["*"]
        return _split_csv(s)

    @field_validator("cors_allow_methods", "cors_allow_headers", "trusted_hosts", mode="before")
    @classmethod
    def _parse_csv_lists(cls, v):
        if v is None:
            return v
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        s = str(v).strip()
        if not s:
            return []
        if s == "*":
            return ["*"]
        return _split_csv(s)


settings = Settings()

logger.info(
    "settings loaded env_file=%s internal_auth_enabled=%s internal_id_set=%s internal_secret_set=%s jwt_secret_set=%s exp_seconds=%s join_secret_set=%s stale_seconds=%s",
    _env_file(),
    settings.internal_auth_enabled,
    bool(settings.internal_client_id),
    bool(settings.internal_client_secret),
    bool(settings.internal_auth_secret),
    settings.internal_token_expire_seconds,
    bool((settings.socket_room_join_token_secret or "").strip()),
    settings.socket_server_stale_seconds,
)

__all__ = ["Settings", "settings"]
