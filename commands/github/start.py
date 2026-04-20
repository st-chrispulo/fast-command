# commands/github/start.py

import os
import base64
import hashlib
import secrets
import urllib.parse as up
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from pydantic import ConfigDict

from commands.base_command import BaseCommand
import logger

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _truthy(s: Optional[str], default: bool = False) -> bool:
    if s is None:
        return default
    s = s.strip().lower()
    return s in {"1", "true", "t", "yes", "y", "on"}


class GithubStartQuery(BaseModel):
    mode: Optional[str] = Field(
        "signin",
        description='Flow mode: "signin" or "register".',
        examples=["signin", "register"],
    )
    return_to: Optional[str] = Field(
        None,
        description="Optional relative path to redirect after successful login.",
        examples=["/dashboard", "/settings"],
    )
    scope: Optional[str] = Field(
        None,
        description='Override scopes (space-separated).',
        examples=["read:user user:email", "read:user user:email repo"],
    )
    allow_signup: Optional[str] = Field(
        None,
        description='Whether GitHub shows the “Sign up” option ("true" | "false").',
        examples=["true", "false"],
    )
    # NEW: when truthy, return JSON instead of 302 redirect
    json: Optional[bool] = Field(
        default=None,
        description="When true, return the authorize URL as JSON instead of redirecting.",
        examples=[True, False, 1, 0, "true", "false", "1", "0"],
    )

    @field_validator("json", mode="before")
    @classmethod
    def _coerce_json_flag(cls, v):
        # Accept bool/int/str; map to bool or None
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return bool(v)
        # strings like "1", "true", "yes"
        return _truthy(str(v), default=False)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"mode": "signin"},
                {"mode": "register", "return_to": "/dashboard", "allow_signup": "false"},
                {"scope": "read:user user:email repo"},
                {"json": True},
            ]
        }
    )


class GithubStartCommand(BaseCommand):
    """
    GET /github/start
    - Generates state (nonce::mode)
    - Optional PKCE if GITHUB_USE_PKCE=true
    - Sets HttpOnly cookies for state (+ pkce verifier) and optional return_to
    - Redirects to GitHub authorize URL by default
    - If ?json=1, returns {"authorize_url": "..."} instead of redirect
    """
    name = "github/start"       # → GET /github/start
    schema = GithubStartQuery
    require_auth = False
    method = "GET"

    def run(self, q: GithubStartQuery):
        client_id = (os.getenv("GITHUB_CLIENT_ID") or "").strip()
        redirect_uri = (os.getenv("GITHUB_REDIRECT_URI") or "").strip()
        if not client_id or not redirect_uri:
            raise HTTPException(status_code=500, detail="GitHub OAuth not configured (client_id/redirect_uri).")

        # Scopes: query override > env > default
        scopes = (q.scope or os.getenv("GITHUB_SCOPES") or "read:user user:email").strip()
        allow_signup_env = os.getenv("GITHUB_ALLOW_SIGNUP")
        allow_signup = q.allow_signup if q.allow_signup is not None else allow_signup_env
        use_pkce = _truthy(os.getenv("GITHUB_USE_PKCE"), default=False)

        # CSRF state with mode (nonce::mode)
        nonce = secrets.token_urlsafe(16)
        mode = q.mode or "signin"
        state = f"{nonce}::{mode}"

        # PKCE (optional)
        code_verifier = None
        code_challenge = None
        if use_pkce:
            code_verifier = _b64url(secrets.token_bytes(32))
            code_challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
        }

        if allow_signup is not None:
            params["allow_signup"] = allow_signup
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"

        auth_url = "{}?{}".format(GITHUB_AUTH_URL, up.urlencode(params))

        # Common cookie settings
        cookie_secure = _truthy(os.getenv("COOKIE_SECURE"), default=True)
        cookie_common = dict(httponly=True, samesite="lax", secure=cookie_secure, max_age=600)

        # Decide response type
        if q.json:  # return JSON payload (still set cookies to maintain flow)
            resp = JSONResponse(
                {
                    "authorize_url": auth_url,
                    "state": state,
                    "mode": mode,
                    "scopes": scopes,
                    "allow_signup": allow_signup,
                    "use_pkce": bool(use_pkce),
                    "code_verifier": code_verifier,
                }
            )
        else:
            resp = RedirectResponse(url=auth_url, status_code=302)

        # Set cookies for both paths to keep the flow consistent
        resp.set_cookie("gh_state", state, **cookie_common)
        if code_verifier:
            resp.set_cookie("gh_pkce_verifier", code_verifier, **cookie_common)
        if q.return_to:
            resp.set_cookie("gh_return_to", q.return_to, **cookie_common)

        return resp
