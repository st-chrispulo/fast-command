import os
import json
import secrets
import string
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

from pydantic import BaseModel, field_validator
from fastapi import HTTPException
from sqlalchemy import text

from commands.base_command import BaseCommand
from auth.db import SessionLocal
from passlib.context import CryptContext

# NEW: token helpers
from auth.token import (
    create_access_token,
    create_refresh_token,
    TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
)

# password hashing – keep consistent with your login flow
pwd_context = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")


# -------------------------
# Helpers
# -------------------------
def _generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _exchange_code_for_token(code: str, redirect_uri: Optional[str] = None) -> Tuple[str, Optional[str], Optional[str], Optional[datetime]]:
    """
    Exchange OAuth 'code' for access token. Returns (access_token, token_type, scope, expires_at)
    GitHub classic OAuth typically gives access_token + token_type; scopes often via headers.
    """
    client_id = os.getenv("GITHUB_CLIENT_ID", "").strip()
    client_secret = os.getenv("GITHUB_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured (client_id/secret).")

    payload = {"client_id": client_id, "client_secret": client_secret, "code": code}
    if redirect_uri:
        payload["redirect_uri"] = redirect_uri  # must match FE

    try:
        r = requests.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"GitHub token exchange failed: {e}")

    data = r.json()
    access_token = data.get("access_token")
    token_type = data.get("token_type")
    # Classic OAuth usually not returning expires_in/refresh_token; GitHub Apps/OIDC differ
    scope = data.get("scope")  # sometimes present as space/comma list
    expires_at = None

    if not access_token:
        raise HTTPException(status_code=502, detail=f"GitHub token exchange returned no access_token: {data}")
    return access_token, token_type, scope, expires_at


def _fetch_github_user(access_token: str) -> dict:
    try:
        u = requests.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        u.raise_for_status()
        profile = u.json()
        scopes_header = u.headers.get("x-oauth-scopes")  # comma-separated or empty

        email = profile.get("email")
        if not email:
            em = requests.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
                timeout=15,
            )
            if em.status_code == 200:
                emails = em.json() or []
                primary_verified = next((e["email"] for e in emails if e.get("primary") and e.get("verified")), None)
                email = primary_verified or (emails[0]["email"] if emails else None)

        return {
            "github_user_id": profile.get("id"),
            "login": profile.get("login"),
            "name": profile.get("name"),
            "email": email,
            "avatar_url": profile.get("avatar_url"),
            "token_scope": scopes_header,
            "profile_json": profile,
        }
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"GitHub API error: {e}")


def _make_unique_username(db, base_login: str) -> str:
    base = (base_login or "gh_user").strip().lower() or "gh_user"
    candidate = base
    i = 0
    while True:
        row = db.execute(text("SELECT 1 FROM tbl_users WHERE username = :u"), {"u": candidate}).fetchone()
        if not row:
            return candidate
        i += 1
        candidate = f"{base}{i}"


# -------------------------
# Payloads
# -------------------------
class RegisterGithubUserPayload(BaseModel):
    code: Optional[str] = None
    access_token: Optional[str] = None

    # (direct mode/testing)
    github_user_id: Optional[int] = None
    login: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    avatar_url: Optional[str] = None

    @field_validator("code", "access_token", "login", "name", "email", "avatar_url", mode="before")
    @classmethod
    def _strip_opt(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("github_user_id")
    @classmethod
    def _validate_ids(cls, v):
        if v is not None and v <= 0:
            raise ValueError("github_user_id must be a positive integer")
        return v


class RegisterGithubUserCommand(BaseCommand):
    """
    Registers (or links) a user via GitHub OAuth.

    Modes:
      - Provide "code": we exchange to get access_token, then fetch /user (+/user/emails)
      - Provide "access_token": we fetch /user (+/user/emails)
      - Provide direct fields: github_user_id + login (and optionally name/email/avatar_url)
    """
    name = "github/user/register"
    schema = RegisterGithubUserPayload
    require_auth = False  # public OAuth callback

    def run(self, payload: RegisterGithubUserPayload):
        db = SessionLocal()
        try:
            access_token = None
            token_type = None
            token_scope = None
            refresh_token_enc = None
            expires_at = None

            if payload.code:
                redirect_uri = os.getenv("GITHUB_REDIRECT_URI", "").strip() or None
                access_token, token_type, token_scope_from_exchange, expires_at = _exchange_code_for_token(
                    payload.code, redirect_uri
                )
                gh = _fetch_github_user(access_token)
                # prefer /user header for scopes; fallback to exchange payload
                token_scope = gh.get("token_scope") or token_scope_from_exchange
            elif payload.access_token:
                access_token = payload.access_token
                token_type = "bearer"
                gh = _fetch_github_user(access_token)
                token_scope = gh.get("token_scope")
            else:
                if not payload.github_user_id or not payload.login:
                    raise HTTPException(status_code=400, detail="Provide 'code', 'access_token', or both 'github_user_id' and 'login'.")
                gh = {
                    "github_user_id": payload.github_user_id,
                    "login": payload.login,
                    "name": payload.name,
                    "email": payload.email,
                    "avatar_url": payload.avatar_url,
                    "token_scope": None,
                    "profile_json": None,
                }

            github_user_id = gh.get("github_user_id")
            login = gh.get("login")
            if not github_user_id or not login:
                raise HTTPException(status_code=502, detail="GitHub profile missing 'id' or 'login'.")

            name = gh.get("name")
            email = gh.get("email")
            avatar_url = gh.get("avatar_url")
            profile_json = gh.get("profile_json")

            # synthesize email if user hides it
            if not email:
                email = f"{github_user_id}+{login}@users.noreply.github.com"

            # 2) Find or create local user (email → github link)
            user_row = db.execute(text("SELECT id FROM tbl_users WHERE email = :email"), {"email": email}).fetchone()

            if not user_row:
                linked = db.execute(
                    text("SELECT user_id FROM tbl_user_github WHERE github_user_id = :gid"),
                    {"gid": github_user_id},
                ).fetchone()
                if linked:
                    user_row = linked

            if not user_row:
                username = _make_unique_username(db, login)
                random_pw = _generate_password(24)
                hashed_pw = pwd_context.hash(random_pw)

                # IMPORTANT: match your tbl_users column name (password vs password)
                user_row = db.execute(
                    text("""
                        INSERT INTO tbl_users (username, email, password)
                        VALUES (:username, :email, :password)
                        RETURNING id
                    """),
                    {"username": username, "email": email, "password": hashed_pw},
                ).fetchone()

            user_id = user_row[0]

            # 3) Upsert GitHub linkage
            # TODO: replace with real encryption-at-rest (Fernet/KMS) for tokens
            gh_access_token_enc = access_token
            now_utc = datetime.now(timezone.utc)
            now_iso = now_utc.isoformat()

            db.execute(
                text("""
                    INSERT INTO tbl_user_github (
                        user_id, github_user_id, login, name, email, avatar_url,
                        access_token_enc, refresh_token_enc, token_type, token_scope, expires_at,
                        installed_at, profile_json, last_synced_at
                    )
                    VALUES (
                        :user_id, :github_user_id, :login, :name, :email, :avatar_url,
                        :access_token_enc, :refresh_token_enc, :token_type, :token_scope, :expires_at,
                        NOW(), CAST(:profile_json AS JSONB), NOW()
                    )
                    ON CONFLICT (github_user_id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        login = EXCLUDED.login,
                        name = EXCLUDED.name,
                        email = EXCLUDED.email,
                        avatar_url = EXCLUDED.avatar_url,
                        access_token_enc = EXCLUDED.access_token_enc,
                        refresh_token_enc = EXCLUDED.refresh_token_enc,
                        token_type = EXCLUDED.token_type,
                        token_scope = EXCLUDED.token_scope,
                        expires_at = EXCLUDED.expires_at,
                        profile_json = EXCLUDED.profile_json,
                        last_synced_at = NOW(),
                        updated_at = NOW()
                """),
                {
                    "user_id": user_id,
                    "github_user_id": github_user_id,
                    "login": login,
                    "name": name,
                    "email": email,
                    "avatar_url": avatar_url,
                    "access_token_enc": gh_access_token_enc,
                    "refresh_token_enc": refresh_token_enc,  # usually None for classic OAuth
                    "token_type": token_type,
                    "token_scope": token_scope,
                    "expires_at": expires_at,
                    "profile_json": json.dumps(profile_json) if profile_json is not None else None,
                },
            )

            # 4) Issue *your app's* access/refresh tokens (same as LoginCommand)
            app_access_token = create_access_token({"user_id": user_id})
            app_refresh_token = create_refresh_token({"user_id": user_id})
            app_access_exp = datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
            app_refresh_exp = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

            db.execute(
                text("""
                    INSERT INTO tbl_tokens (
                        user_id, access_token, refresh_token, scope, expires_at, refresh_token_expires_at
                    )
                    VALUES (
                        :user_id, :access_token, :refresh_token, :scope, :expires_at, :refresh_token_expires_at
                    )
                """),
                {
                    "user_id": user_id,
                    "access_token": app_access_token,
                    "refresh_token": app_refresh_token,
                    "scope": "github",  # tag the issuance source
                    "expires_at": app_access_exp,
                    "refresh_token_expires_at": app_refresh_exp,
                }
            )

            db.commit()

            return {
                "status": "ok",
                "user_id": user_id,
                "github_user_id": github_user_id,
                "login": login,
                "email": email,
                "linked": True,
                "created_or_updated_at": now_iso,

                # NEW: return your app session tokens
                "access_token": app_access_token,
                "refresh_token": app_refresh_token,
                "token_type": "bearer",
                "expires_in": TOKEN_EXPIRE_MINUTES * 60,
            }
        finally:
            db.close()
