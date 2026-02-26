from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import jwt
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from commands.base_command import BaseCommand
from app.core.settings import settings

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("internal.auth.token")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class InternalTokenPayload(BaseModel):
    client_id: str = Field(...)
    client_secret: str = Field(...)
    scopes: List[str] = Field(default_factory=list)

    @field_validator("client_id", "client_secret")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("required")
        return v

    @field_validator("scopes", mode="before")
    @classmethod
    def _parse_scopes(cls, v):
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        s = str(v).strip()
        if not s:
            return []
        return [p.strip() for p in s.split(",") if p.strip()]


class InternalAuthTokenCommand(BaseCommand):
    name = "internal/auth/token"
    schema = InternalTokenPayload
    require_auth = False
    method = "post"
    type = "json"
    group = "Internal"

    async def execute(self, payload: InternalTokenPayload, user_id: Optional[str] = None) -> Dict[str, Any]:
        if not settings.internal_auth_enabled:
            raise HTTPException(status_code=403, detail="Internal auth disabled")

        cfg_id = (settings.internal_client_id or "").strip()
        cfg_secret = (settings.internal_client_secret or "").strip()
        jwt_secret = (settings.internal_auth_secret or "").strip()
        exp_seconds = int(settings.internal_token_expire_seconds or 300)

        if not (cfg_id and cfg_secret and jwt_secret):
            raise HTTPException(status_code=500, detail="Internal auth not configured")

        ok_id = hmac.compare_digest(payload.client_id.encode(), cfg_id.encode())
        ok_secret = hmac.compare_digest(payload.client_secret.encode(), cfg_secret.encode())
        if not (ok_id and ok_secret):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        scopes = [s.strip() for s in (payload.scopes or []) if s.strip()]
        if not scopes:
            raise HTTPException(status_code=400, detail="Missing scopes")

        now = datetime.now(timezone.utc)
        exp = now + timedelta(seconds=exp_seconds)

        token_payload = {
            "sub": payload.client_id,
            "typ": "internal",
            "scp": scopes,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        }

        token = jwt.encode(token_payload, jwt_secret, algorithm="HS256")

        return {
            "status": "ok",
            "data": {"access_token": token, "token_type": "bearer", "expires_in": exp_seconds},
        }


__all__ = ["InternalAuthTokenCommand"]
