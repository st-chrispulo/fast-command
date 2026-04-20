import os
import secrets
import urllib.parse as up
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, ConfigDict

from commands.base_command import BaseCommand


def _truthy(v: Optional[str], default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


class GoogleStartQuery(BaseModel):
    mode: Optional[str] = Field("signin")
    return_to: Optional[str] = Field(None)
    scope: Optional[str] = Field(None)
    state: Optional[str] = Field(None)  # ✅ FIXED
    code_challenge: Optional[str] = Field(None)
    json: Optional[bool] = Field(default=None)

    @field_validator("json", mode="before")
    @classmethod
    def _coerce_json_flag(cls, v):
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return bool(v)
        return _truthy(str(v), default=False)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"mode": "signin"},
                {"mode": "register"},
                {"code_challenge": "abc123..."},
            ]
        }
    )


class GoogleStartCommand(BaseCommand):
    name = "google/start"
    schema = GoogleStartQuery
    require_auth = False
    method = "GET"

    def run(self, q: GoogleStartQuery):
        auth_url_base = (os.getenv("GOOGLE_AUTH_URL") or "").strip()
        client_id = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
        redirect_uri = (os.getenv("GOOGLE_REDIRECT_URI") or "").strip()

        if not auth_url_base:
            raise HTTPException(status_code=500, detail="Missing GOOGLE_AUTH_URL.")

        if not client_id or not redirect_uri:
            raise HTTPException(
                status_code=500,
                detail="Google OAuth not configured (GOOGLE_CLIENT_ID/GOOGLE_REDIRECT_URI).",
            )

        scopes = (
            q.scope
            or os.getenv("GOOGLE_SCOPES")
            or "openid email profile"
        ).strip()

        mode = (q.mode or "signin").strip().lower()

        # ✅ Generate state only if not provided
        state = q.state or secrets.token_urlsafe(32)

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scopes,
            "state": state,
            "access_type": (os.getenv("GOOGLE_ACCESS_TYPE") or "offline").strip(),
            "prompt": (os.getenv("GOOGLE_PROMPT") or "consent").strip(),
        }

        if q.code_challenge:
            params["code_challenge"] = q.code_challenge
            params["code_challenge_method"] = "S256"

        auth_url = f"{auth_url_base}?{up.urlencode(params)}"

        if q.json:
            return JSONResponse(
                {
                    "authorize_url": auth_url,
                    "state": state,
                    "mode": mode,
                    "scopes": scopes,
                }
            )

        return RedirectResponse(url=auth_url, status_code=302)