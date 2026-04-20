import jwt
from datetime import datetime, timedelta
from fastapi import HTTPException
from os import getenv
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = getenv("JWT_SECRET_KEY")
ALGORITHM = getenv("JWT_ALGORITHM")
TOKEN_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = 7


def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    jw = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return jw


def create_refresh_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))
    to_encode.update({"exp": expire})
    jw = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return jw


def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if datetime.utcnow().timestamp() > payload["exp"]:
            raise HTTPException(status_code=401, detail="Token expired")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_token_payload(token: str) -> dict:
    payload = None
    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"verify_exp": False}
        )
        return payload
    except jwt.InvalidTokenError:
        payload = payload
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    return payload


def create_system_token(days_valid=365):
    return create_access_token({
        "user_id": "system",
        "role": "internal"
    }, expires_delta=timedelta(days=days_valid))
