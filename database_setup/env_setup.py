from pathlib import Path
from typing import Dict, Optional

from dotenv import dotenv_values, set_key

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("db.env_setup")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class EnvSetup:
    def __init__(self, init_env_path: str, output_env_path: str = ".env"):
        self.init_env_path = Path(init_env_path)
        self.output_env_path = Path(output_env_path)

        if not self.init_env_path.exists():
            raise FileNotFoundError(f".env.init not found at: {self.init_env_path}")

        self.init_map: Dict[str, str] = dotenv_values(self.init_env_path)

    def get(self, key: str) -> str:
        val = self.init_map.get(key)
        if val is None:
            raise ValueError(f"Missing key '{key}' in {self.init_env_path}")
        return str(val)

    @staticmethod
    def _is_init_var(key: str) -> bool:
        return "INIT" in key.upper()

    def _ensure_output_file(self) -> None:
        if not self.output_env_path.exists():
            self.output_env_path.write_text("", encoding="utf-8")

    def generate_app_env(
        self,
        db_name: str,
        db_user: str,
        db_password: str,
        db_host: str,
        db_port: str,
        *,
        extra_overrides: Optional[Dict[str, str]] = None,
        preserve_existing: bool = True,
    ) -> None:
        merged: Dict[str, str] = {
            k: str(v)
            for k, v in self.init_map.items()
            if v is not None and not self._is_init_var(k)
        }

        if preserve_existing and self.output_env_path.exists():
            existing = dotenv_values(self.output_env_path)
            merged.update({k: str(v) for k, v in existing.items() if v is not None})

        merged.update(
            {
                "APP_NAME": self.get("APP_NAME"),
                "SECRET_KEY": self.get("SECRET_KEY"),
                "PG_DB_NAME": db_name,
                "PG_DB_USER": db_user,
                "PG_DB_PASSWORD": db_password,
                "PG_DB_HOST": db_host,
                "PG_DB_PORT": db_port,
            }
        )

        if extra_overrides:
            for k, v in extra_overrides.items():
                if v is not None and not self._is_init_var(k):
                    merged[k] = str(v)

        self._ensure_output_file()

        wrote = 0
        for k, v in merged.items():
            if self._is_init_var(k):
                continue
            set_key(str(self.output_env_path), k, str(v))
            wrote += 1

        logger.info(".env created/updated with %s keys (skipped *INIT* vars).", wrote)
