from __future__ import annotations

import os
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker
from utils.env import bootstrap_environment

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("auth.db")
except Exception:
    import logging

    logger = logging.getLogger(__name__)

Base = declarative_base()


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip()
    return v or default


def _build_database_url(external=False) -> str:
    user = _env("PG_DB_USER", "")
    password = _env("PG_DB_PASSWORD", "")
    host = _env("PG_DB_HOST", "localhost")
    port = _env("PG_DB_PORT", "5432")
    db = _env("PG_DB_NAME", "")
    if external:
        if host == 'postgres':
            host = 'localhost'
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"


def _create_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


bootstrap_environment()

APP_NAME = _env("APP_NAME")
DATABASE_URL = _build_database_url()

engine = _create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

DATABASE_URL_EXT = _build_database_url(True)

engine = _create_engine(DATABASE_URL_EXT)
SessionLocalExternal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
