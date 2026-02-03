from __future__ import annotations

from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOLITUD_", case_sensitive=False)

    app_name: str = Field(default="Fast Command API")
    app_description: str = Field(default="Execute dynamic business commands via FastAPI.")
    app_version: str = Field(default="1.0.0")

    cors_origins: List[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    cors_allow_credentials: bool = Field(default=True)

    trusted_hosts: List[str] = Field(default_factory=lambda: ["*"])

    gzip_minimum_size: int = Field(default=1000)

    static_enabled: bool = Field(default=True)


settings = Settings()
