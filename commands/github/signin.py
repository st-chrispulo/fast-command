# commands/github/signin.py

import os
import requests
from datetime import datetime, timedelta
from typing import Optional, Tuple
from pydantic import BaseModel, field_validator
from fastapi import HTTPException
from sqlalchemy import text

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from auth.token import (
    create_access_token,
    create_refresh_token,
    TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
)


def _exchange_code_for_token(code: str, redirect_uri: Optional[str]) -> Tuple[str, Optional[str], Optional[str]]:
    client_id = os.getenv("GITHUB_CLIENT_ID", "").strip()
    client_secret = os.getenv("GITHUB_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured.")
    payload = {"client_id": client_id, "client_secret": client_secret, "code": code}
    if redirect_uri:
        payload["redirect_uri"] = redirect_uri
    try:
        r = requests.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"GitHub token exchange failed: {e}")
    access_token = data.get("access_token")
    token_type = data.get("token_type")
    scope = data.get("scope")
    if not access_token:
        raise HTTPException(status_code=502, detail=f"No access_token from GitHub: {data}")
    return access_token, token_type, scope


def _fetch_github_identity(access_token: str) -> dict:
    try:
        resp = requests.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        resp.raise_for_status()
        profile = resp.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"GitHub API error: {e}")
    gid = profile.get("id")
    login = profile.get("login")
    if not gid or not login:
        raise HTTPException(status_code=502, detail="GitHub profile missing 'id' or 'login'.")
    return {"github_user_id": int(gid), "login": login, "name": profile.get("name"), "avatar_url": profile.get("avatar_url")}

class GithubSignInPayload(BaseModel):
    code: Optional[str] = None
    access_token: Optional[str] = None
    @field_validator("code", "access_token", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v

class GithubSignInCommand(BaseCommand):
    """Sign in with GitHub (no registration/creation)."""
    name = "github/user/signin"   # ← this becomes POST /github/user/signin
    schema = GithubSignInPayload
    require_auth = False

    def run(self, payload: GithubSignInPayload):
        if not payload.code and not payload.access_token:
            raise HTTPException(status_code=400, detail="Provide 'code' or 'access_token'.")
        if payload.code:
            redirect_uri = os.getenv("GITHUB_REDIRECT_URI", "").strip() or None
            gh_token, _tok_type, _scope = _exchange_code_for_token(payload.code, redirect_uri)
        else:
            gh_token = payload.access_token
        gh = _fetch_github_identity(gh_token)
        github_user_id = gh["github_user_id"]
        github_login = gh["login"]

        db = SessionLocal()
        try:
            row = db.execute(
                text("""
                    SELECT ug.user_id, u.email, ug.login
                    FROM tbl_user_github ug
                    JOIN tbl_users u ON u.id = ug.user_id
                    WHERE ug.github_user_id = :gid
                    LIMIT 1
                """),
                {"gid": github_user_id},
            ).fetchone()

            if not row:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "ACCOUNT_NOT_LINKED",
                        "message": "This GitHub account is not linked to any user.",
                        "github": {"id": github_user_id, "login": github_login, "name": gh.get("name"), "avatar_url": gh.get("avatar_url")},
                    },
                )

            user_id, email, saved_login = row

            access_tok = create_access_token({"user_id": user_id})
            refresh_tok = create_refresh_token({"user_id": user_id})
            access_exp = datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
            refresh_exp = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

            db.execute(
                text("""
                    INSERT INTO tbl_tokens (user_id, access_token, refresh_token, scope, expires_at, refresh_token_expires_at)
                    VALUES (:user_id, :at, :rt, :scope, :exp, :rexp)
                """),
                {"user_id": user_id, "at": access_tok, "rt": refresh_tok, "scope": "github", "exp": access_exp, "rexp": refresh_exp},
            )
            db.commit()

            return {
                "status": "ok",
                "user_id": user_id,
                "github_user_id": github_user_id,
                "github_login": saved_login or github_login,
                "email": email,
                "token_type": "bearer",
                "access_token": access_tok,
                "refresh_token": refresh_tok,
                "expires_in": TOKEN_EXPIRE_MINUTES * 60,
            }
        finally:
            db.close()
