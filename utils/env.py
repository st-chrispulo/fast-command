from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, Tuple

from dotenv import dotenv_values

ROOT_DIR = Path(__file__).resolve().parents[1]
BASE_ENV_FILE = ROOT_DIR / ".env"
LOCAL_ENV_FILE = ROOT_DIR / ".env.local"


def env_file_paths() -> Tuple[Path, ...]:
    return BASE_ENV_FILE, LOCAL_ENV_FILE


def existing_env_file_paths() -> Tuple[Path, ...]:
    return tuple(path for path in env_file_paths() if path.exists())


def merged_env_values(paths: Iterable[Path] | None = None) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for path in paths or env_file_paths():
        if not path.exists():
            continue
        for key, value in dotenv_values(path).items():
            if key and value is not None:
                merged[str(key)] = str(value)
    return merged


def bootstrap_environment() -> Tuple[str, ...]:
    loaded = existing_env_file_paths()
    for key, value in merged_env_values(loaded).items():
        os.environ.setdefault(key, value)
    return tuple(str(path) for path in loaded)
