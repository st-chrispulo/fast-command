# auth/db.py

import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# ✅ Always load .env from project root (1 level above /auth)
BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

APP_NAME = os.getenv("APP_NAME")
PG_DB_NAME = os.getenv("PG_DB_NAME")
PG_DB_USER = os.getenv("PG_DB_USER")
PG_DB_PASSWORD = os.getenv("PG_DB_PASSWORD")
PG_DB_HOST = os.getenv("PG_DB_HOST")
PG_DB_PORT = os.getenv("PG_DB_PORT")

DATABASE_URL = (
    f"postgresql+psycopg2://{PG_DB_USER}:{PG_DB_PASSWORD}"
    f"@{PG_DB_HOST}:{PG_DB_PORT}/{PG_DB_NAME}"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
