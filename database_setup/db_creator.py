import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from .utils import generate_password
from logger import logger


class DBCreator:
    def __init__(self, env_setup):
        self.env = env_setup
        self.app_name = str(self.env.get("APP_NAME")).lower()

        # admin / bootstrap creds
        self.init_db = self.env.get("PG_INIT_DB_NAME")
        self.init_user = self.env.get("PG_INIT_DB_USER")
        self.init_password = self.env.get("PG_INIT_DB_PASSWORD")
        self.host = self.env.get("PG_INIT_DB_HOST")
        self.port = self.env.get("PG_INIT_DB_PORT")

        # generated runtime db + role
        self.new_db = f"{self.app_name}_db"
        self.new_user = f"{self.app_name}_user"
        self.new_password = generate_password()

        self.generated_password = None

    def _configure_app_user_privileges(self):
        conn_cfg = None
        cur_cfg = None

        try:
            conn_cfg = psycopg2.connect(
                dbname=self.new_db,
                user=self.init_user,
                password=self.init_password,
                host=self.host,
                port=self.port,
            )
            conn_cfg.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            cur_cfg = conn_cfg.cursor()

            cur_cfg.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s;",
                (self.new_user,)
            )
            if not cur_cfg.fetchone():
                raise RuntimeError(
                    f"Role {self.new_user} does not exist; cannot configure privileges."
                )

            cur_cfg.execute(
                f"GRANT USAGE, CREATE ON SCHEMA public TO {self.new_user};"
            )

            cur_cfg.execute(
                f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO {self.new_user};"
            )

            cur_cfg.execute(
                f"GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO {self.new_user};"
            )

            cur_cfg.execute(
                f"""
                ALTER DEFAULT PRIVILEGES IN SCHEMA public
                GRANT ALL PRIVILEGES ON TABLES TO {self.new_user};
                """
            )

            cur_cfg.execute(
                f"""
                ALTER DEFAULT PRIVILEGES IN SCHEMA public
                GRANT ALL PRIVILEGES ON SEQUENCES TO {self.new_user};
                """
            )

            logger.info(
                "Full privileges (schema, tables, sequences, defaults) granted to '%s'.",
                self.new_user,
            )

        except Exception as e:
            logger.exception(
                "Error configuring privileges for app user '%s': %s",
                self.new_user,
                e,
            )
            raise
        finally:
            if cur_cfg:
                cur_cfg.close()
            if conn_cfg:
                conn_cfg.close()

    def create(self):
        conn = None
        cur = None
        conn_new = None
        cur_new = None

        try:
            # connect to bootstrap DB (admin scope)
            conn = psycopg2.connect(
                dbname=self.init_db,
                user=self.init_user,
                password=self.init_password,
                host=self.host,
                port=self.port,
            )
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            cur = conn.cursor()

            # ensure database exists
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s;",
                (self.new_db,),
            )
            if not cur.fetchone():
                cur.execute(f"CREATE DATABASE {self.new_db};")
                logger.info(f"Database '{self.new_db}' created.")
            else:
                logger.info(f"Database '{self.new_db}' already exists.")

            # ensure role fresh
            cur.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s;",
                (self.new_user,),
            )
            role_exists = cur.fetchone() is not None

            if role_exists:
                logger.info(
                    f"User '{self.new_user}' already exists. Recreating it for a clean password..."
                )

                # cleanup ownership in bootstrap DB
                cur.execute(
                    f"REASSIGN OWNED BY {self.new_user} TO {self.init_user};"
                )
                cur.execute(f"DROP OWNED BY {self.new_user};")

                # cleanup ownership in app DB
                try:
                    conn_tmp = psycopg2.connect(
                        dbname=self.new_db,
                        user=self.init_user,
                        password=self.init_password,
                        host=self.host,
                        port=self.port,
                    )
                    conn_tmp.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                    cur_tmp = conn_tmp.cursor()
                    cur_tmp.execute(
                        f"REASSIGN OWNED BY {self.new_user} TO {self.init_user};"
                    )
                    cur_tmp.execute(f"DROP OWNED BY {self.new_user};")
                    cur_tmp.close()
                    conn_tmp.close()
                except Exception as sub_e:
                    logger.info(
                        f"No owned objects to clean in {self.new_db} "
                        f"(or cleanup not needed): {sub_e}"
                    )

                # finally drop the role
                cur.execute(f"DROP ROLE {self.new_user};")
                logger.info(f"Dropped existing role '{self.new_user}'.")

            # create role again w/ new password
            self.generated_password = generate_password()
            cur.execute(
                f"CREATE ROLE {self.new_user} WITH LOGIN PASSWORD %s;",
                (self.generated_password,),
            )
            logger.info(
                f"User '{self.new_user}' created with fresh password."
            )

            # grant DB-level privileges (connect, create temp tables, etc.)
            cur.execute(
                f"GRANT ALL PRIVILEGES ON DATABASE {self.new_db} TO {self.new_user};"
            )
            logger.info("Database privileges granted.")

            # connect to new DB to do first-pass schema grants
            conn_new = psycopg2.connect(
                dbname=self.new_db,
                user=self.init_user,
                password=self.init_password,
                host=self.host,
                port=self.port,
            )
            conn_new.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            cur_new = conn_new.cursor()

            # let app user use/create in public schema right away
            cur_new.execute(
                f"GRANT USAGE, CREATE ON SCHEMA public TO {self.new_user};"
            )
            logger.info("Schema privileges granted (USAGE, CREATE).")

            cur_new.close()
            conn_new.close()
            cur_new = None
            conn_new = None

            # 🔥 now run the full configure step (tables + sequences + defaults)
            self._configure_app_user_privileges()

            # write final .env with creds
            self.env.generate_app_env(
                self.new_db,
                self.new_user,
                self.generated_password,
                self.host,
                self.port,
            )
            logger.info(
                ".env has been (re)generated and is now in sync with Postgres."
            )

        except Exception as e:
            logger.error(f"Error during DB creation: {e}")
            raise
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()
            if cur_new:
                cur_new.close()
            if conn_new:
                conn_new.close()
