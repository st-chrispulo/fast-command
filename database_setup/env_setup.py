import os
from dotenv import load_dotenv, set_key
from pathlib import Path
from logger import logger


class EnvSetup:
    def __init__(self, init_env_path, output_env_path=".env"):
        self.init_env_path = init_env_path
        self.output_env_path = Path(output_env_path)
        load_dotenv(dotenv_path=self.init_env_path)

    def get(self, key):
        value = os.getenv(key)
        if value is None:
            raise ValueError(f"Missing key '{key}' in {self.init_env_path}")
        return value

    def generate_app_env(self, db_name, db_user, db_password, db_host, db_port):
        self.output_env_path.write_text("")
        set_key(str(self.output_env_path), "APP_NAME", self.get("APP_NAME"))
        set_key(str(self.output_env_path), "SECRET_KEY", self.get("SECRET_KEY"))
        set_key(str(self.output_env_path), "PG_DB_NAME", db_name)
        set_key(str(self.output_env_path), "PG_DB_USER", db_user)
        set_key(str(self.output_env_path), "PG_DB_PASSWORD", db_password)
        set_key(str(self.output_env_path), "PG_DB_HOST", db_host)
        set_key(str(self.output_env_path), "PG_DB_PORT", db_port)

        logger.info(".env file created")

