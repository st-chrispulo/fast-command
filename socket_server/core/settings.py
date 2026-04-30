from __future__ import annotations

from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from utils.env import bootstrap_environment

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("socket_server.core.settings")
except Exception:
    import logging

    logger = logging.getLogger(__name__)

ENV_FILES = bootstrap_environment()


class Settings(BaseSettings):
    """Socket server settings loaded from .env with optional .env.local overrides."""

    model_config = SettingsConfigDict(
        env_ignore_empty=True,
        extra="ignore",
        env_prefix="SOLITUD_",
        case_sensitive=False
    )

    api_base_url: str = Field(default="http://localhost:8000")
    create_endpoint_path: str = Field(default="/api/v0/socket/servers/create")
    heartbeat_endpoint_path: str = Field(default="/api/v0/socket/servers/heartbeat")
    api_timeout_seconds: int = Field(default=15)

    internal_auth_enabled: bool = Field(default=True)
    internal_auth_token_path: str = Field(default="/api/v0/internal/auth/token")
    internal_client_id: str = Field(default="")
    internal_client_secret: str = Field(default="")
    internal_auth_secret: str = Field(default="")
    internal_scopes: List[str] = Field(
        default_factory=lambda: ["socket:servers:create", "socket:servers:heartbeat"],
        validate_default=True
    )
    internal_token_refresh_skew_seconds: int = Field(default=30)

    startup_retries: int = Field(default=30)
    startup_retry_sleep_seconds: float = Field(default=2.0)

    heartbeat_interval_seconds: float = Field(default=10.0)

    socket_server_key: str = Field(default="")
    socket_server_name: str = Field(default="")
    socket_server_host: str = Field(default="")
    socket_server_port: Optional[int] = Field(default=None)
    socket_server_public_url: str = Field(default="")
    socket_server_region: str = Field(default="")
    socket_server_max_rooms: int = Field(default=0)
    socket_server_is_active: bool = Field(default=True)

    cors_allowed_origins: List[str] = Field(default_factory=lambda: ["*"])

    app_name: str = Field(default="socket-server")
    app_version: str = Field(default="0.1.0")

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_origins(cls, v):
        if v is None:
            return ["*"]
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        s = str(v).strip()
        if not s:
            return ["*"]
        if s == "*":
            return ["*"]
        return [p.strip() for p in s.split(",") if p.strip()]

    @field_validator("internal_scopes", mode="before")
    @classmethod
    def _parse_scopes(cls, v):
        import json

        if v is None:
            return ["socket:servers:create", "socket:servers:heartbeat"]

        if isinstance(v, list):
            return v

        s = str(v).strip()

        if not s:
            return []

        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass

        return [x.strip() for x in s.split(",") if x.strip()]

    @field_validator("socket_server_port", mode="before")
    @classmethod
    def _parse_port(cls, v):
        if v is None:
            return None
        if isinstance(v, int):
            return v if v > 0 else None
        s = str(v).strip()
        if not s:
            return None
        try:
            iv = int(s)
            return iv if iv > 0 else None
        except Exception:
            raise ValueError("socket_server_port must be an integer")


settings = Settings()
logger.info(
    "settings loaded env_files=%s api_base_url=%s internal_auth_enabled=%s token_path=%s client_id_set=%s client_secret_set=%s scopes=%s host=%s port=%s public_url=%s",
    ",".join(ENV_FILES) or "<none>",
    settings.api_base_url,
    settings.internal_auth_enabled,
    settings.internal_auth_token_path,
    bool(settings.internal_client_id),
    bool(settings.internal_client_secret),
    ",".join(settings.internal_scopes or []),
    settings.socket_server_host,
    settings.socket_server_port,
    settings.socket_server_public_url,
)

__all__ = ["Settings", "settings"]
