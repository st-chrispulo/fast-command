from pathlib import Path
from typing import Dict, Optional

from dotenv import dotenv_values

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("db.env_setup")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class EnvSetup:
    """Loads environment key/values from a dotenv file and exposes typed accessors."""

    def __init__(self, env_path: str):
        self.env_path = Path(env_path)
        if not self.env_path.exists():
            raise FileNotFoundError(f"Env file not found at: {self.env_path}")

        self.map: Dict[str, str] = {
            k: str(v) for k, v in dotenv_values(self.env_path).items() if v is not None
        }

    def get(self, key: str) -> str:
        """Returns the value for the given key or raises a ValueError."""
        val = self.map.get(key)
        if val is None:
            raise ValueError(f"Missing key '{key}' in {self.env_path}")
        return str(val)

    def get_optional(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Returns the value for the given key or a default when missing."""
        return self.map.get(key, default)