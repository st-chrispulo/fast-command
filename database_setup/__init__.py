from pathlib import Path

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("db.init")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


def initialize_database() -> None:
    """Validates that the application .env exists and does not mutate it."""
    env_file = Path(".env")
    if not env_file.exists():
        raise FileNotFoundError(
            ".env is required but was not found. Create .env (or mount it in Docker) before running migrations."
        )

    logger.info(".env found, skipping env/db/user creation.")


def initialize_migration_table(migrations_dir: str) -> None:
    """Runs SQL migrations from the provided directory using values from .env."""
    from .env_setup import EnvSetup
    from .migration_runner import MigrationRunner

    env = EnvSetup(".env")
    runner = MigrationRunner(env, migrations_dir)
    runner.run()