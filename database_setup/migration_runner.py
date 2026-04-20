from pathlib import Path

import psycopg2

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("db.migration_runner")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class MigrationRunner:
    """Applies pending SQL migrations and records them in tbl_migration_history."""

    def __init__(self, env_setup, migrations_dir: str):
        self.env = env_setup
        self.migrations_dir = Path(migrations_dir)

        self.conn = psycopg2.connect(
            dbname=self.env.get("PG_DB_NAME"),
            user=self.env.get("PG_DB_USER"),
            password=self.env.get("PG_DB_PASSWORD"),
            host=self.env.get("PG_DB_HOST"),
            port=self.env.get("PG_DB_PORT"),
        )
        self.conn.autocommit = True
        self.cursor = self.conn.cursor()

    def ensure_migration_table(self) -> None:
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tbl_migration_history (
                id SERIAL PRIMARY KEY,
                filename TEXT UNIQUE NOT NULL,
                applied_at TIMESTAMP DEFAULT NOW()
            );
            """
        )
        logger.info("Ensured tbl_migration_history table exists.")

    def get_applied_migrations(self) -> set[str]:
        self.cursor.execute("SELECT filename FROM tbl_migration_history;")
        return {row[0] for row in self.cursor.fetchall()}

    def run(self) -> None:
        self.ensure_migration_table()

        applied = self.get_applied_migrations()
        all_migrations = sorted(self.migrations_dir.glob("*.sql"))
        pending = [p for p in all_migrations if p.name not in applied]

        applied_count = 0
        for i, path in enumerate(pending, start=1):
            logger.info("[%s/%s] Applying migration: %s", i, len(pending), path.name)

            sql = path.read_text(encoding="utf-8")
            self.cursor.execute(sql)
            self.cursor.execute(
                "INSERT INTO tbl_migration_history (filename) VALUES (%s);",
                (path.name,),
            )
            applied_count += 1

        logger.info("All migrations complete. Total applied: %s", applied_count)