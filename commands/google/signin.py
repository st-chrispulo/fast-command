import os
import requests
from datetime import datetime, timedelta
from typing import Optional, Tuple

from pydantic import BaseModel, field_validator
from fastapi import HTTPException, Request
from sqlalchemy import text

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from auth.token import (
    create_access_token,
    create_refresh_token,
    TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
)

try:
    from logger import logger as _app_logger
    logger = _app_logger.getChild("commands.google.signin")
except Exception:
    import logging
    logger = logging.getLogger(__name__)


TEMP_TOKEN_EXPIRE_MINUTES = 10


def _create_google_register_temp_token(data: dict) -> str:
    return create_access_token(
        {
            "type": "google_register",
            "sub": data["sub"],
            "email": data.get("email"),
            "name": data.get("name"),
            "picture": data.get("picture"),
        },
        expires_delta=timedelta(minutes=TEMP_TOKEN_EXPIRE_MINUTES),
    )


def _exchange_code_for_token(code: str, code_verifier: Optional[str]) -> Tuple[str, Optional[str]]:
    token_url = os.getenv("GOOGLE_TOKEN_URL")
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI")

    if not token_url or not client_id or not client_secret or not redirect_uri:
        raise HTTPException(status_code=500, detail="Google OAuth not configured.")

    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }

    if code_verifier:
        payload["code_verifier"] = code_verifier

    try:
        r = requests.post(token_url, data=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Google token exchange failed: {e}")

    access_token = data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail="No access_token from Google.")

    return access_token, data.get("id_token")


def _fetch_google_identity(access_token: str) -> dict:
    userinfo_url = os.getenv("GOOGLE_USERINFO_URL")
    if not userinfo_url:
        raise HTTPException(status_code=500, detail="Missing GOOGLE_USERINFO_URL.")

    try:
        r = requests.get(
            userinfo_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Google API error: {e}")

    if not data.get("sub"):
        raise HTTPException(status_code=502, detail="Google profile missing 'sub'.")

    return data


class GoogleSignInPayload(BaseModel):
    code: str
    code_verifier: str

    @field_validator("code", "code_verifier", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v


class GoogleSignInCommand(BaseCommand):
    name = "google/user/signin"
    schema = GoogleSignInPayload
    require_auth = False

    def run(self, payload: GoogleSignInPayload, request: Optional[Request] = None):
        access_token, _ = _exchange_code_for_token(
            payload.code,
            payload.code_verifier,
        )

        g = _fetch_google_identity(access_token)
        google_user_id = g["sub"]

        db = SessionLocal()
        try:
            row = db.execute(
                text(
                    """
                    SELECT ug.user_id, u.email
                    FROM tbl_user_google ug
                    JOIN tbl_users u ON u.id = ug.user_id
                    WHERE ug.google_user_id = :gid
                    LIMIT 1
                    """
                ),
                {"gid": google_user_id},
            ).fetchone()

            if not row:
                temp_token = _create_google_register_temp_token(
                    {
                        "sub": google_user_id,
                        "email": g.get("email"),
                        "name": g.get("name"),
                        "picture": g.get("picture"),
                    }
                )

                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "ACCOUNT_NOT_LINKED",
                        "temp_token": temp_token,
                        "google": {
                            "sub": google_user_id,
                            "email": g.get("email"),
                            "name": g.get("name"),
                            "picture": g.get("picture"),
                        },
                    },
                )

            user_id, email = row

            access_tok = create_access_token({"user_id": user_id})
            refresh_tok = create_refresh_token({"user_id": user_id})

            db.commit()

            return {
                "access_token": access_tok,
                "refresh_token": refresh_tok,
                "token_type": "bearer",
                "expires_in": TOKEN_EXPIRE_MINUTES * 60,
                "user_id": user_id,
                "email": email,
            }

        finally:
            db.close()