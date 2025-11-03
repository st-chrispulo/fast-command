#!/usr/bin/env bash

###############################################################################
# EFDN / App bootstrap script (Windows Git Bash / Linux / macOS)
#
# WHAT THIS SCRIPT DOES END-TO-END:
#
# 1. Requires a bootstrap file .env.init in project root, with ALL of these:
#
#    APP_NAME=efdn
#    SECRET_KEY=change_me_to_a_secure_random_string
#
#    PG_INIT_DB_NAME=postgres
#    PG_INIT_DB_USER=postgres
#    PG_INIT_DB_PASSWORD=postgres_password_here
#    PG_INIT_DB_HOST=localhost
#    PG_INIT_DB_PORT=5432
#
# EXPLANATION:
#   - APP_NAME
#       We will create a new app database called  {APP_NAME}_db
#       We will create a new db user called       {APP_NAME}_user
#       We'll generate a strong random password for that user.
#
#   - SECRET_KEY
#       Copied into the final runtime .env.
#
#   - PG_INIT_DB_NAME / PG_INIT_DB_USER / PG_INIT_DB_PASSWORD / PG_INIT_DB_HOST / PG_INIT_DB_PORT
#       These are the "bootstrap" Postgres credentials with CREATE DATABASE /
#       CREATE ROLE privileges. We connect using these to:
#           - CREATE DATABASE {APP_NAME}_db (if not exists)
#           - CREATE ROLE {APP_NAME}_user (if not exists) with a generated password
#           - GRANT privileges on that DB/schema
#
# OUTPUTS / SIDE EFFECTS:
#   - Generates .env (runtime creds) with:
#         APP_NAME
#         SECRET_KEY
#         PG_DB_NAME        <- {APP_NAME}_db
#         PG_DB_USER        <- {APP_NAME}_user
#         PG_DB_PASSWORD    <- generated secure password
#         PG_DB_HOST
#         PG_DB_PORT
#
#   - Runs migrations/ .sql files exactly once each, tracking them in
#     tbl_migration_history inside the new DB.
#
#   - Starts uvicorn for local FastAPI dev using BACKEND_APP_MODULE.
#
# FLOW:
#   1. Verify .env.init exists and has all required keys
#   2. Create/activate .venv
#   3. pip install requirements + core deps (psycopg2-binary, fastapi, uvicorn)
#   4. python -m database_setup --migrations-dir migrations
#   5. uvicorn main:app --reload
###############################################################################

set -e  # stop on first error

########################################
# CONFIGURATION YOU CAN ADJUST
########################################
VENV_DIR=".venv"
BACKEND_APP_MODULE="app:app"
UVICORN_HOST="0.0.0.0"
UVICORN_PORT="8000"
MIGRATIONS_DIR="migrations"

########################################
# helper: pretty log line
########################################
log () {
    echo ""
    echo "==== $1 ===="
    echo ""
}

########################################
# detect platform / python command
#
# We'll decide:
#   PYTHON_CMD = python (Windows Git Bash) or python3 (Linux/macOS fallback python)
#   ACTIVATE_PATH = .venv/Scripts/activate (Windows venv layout)
#                 = .venv/bin/activate    (*nix venv layout)
########################################
IS_WINDOWS=0
case "$OSTYPE" in
  msys*|cygwin*|win32*)
    IS_WINDOWS=1
    ;;
esac

if [ "$IS_WINDOWS" -eq 1 ]; then
    # likely Git Bash on Windows
    if command -v python >/dev/null 2>&1; then
        PYTHON_CMD="python"
    else
        echo "ERROR: Could not find 'python' on PATH in Git Bash."
        exit 1
    fi
    ACTIVATE_PATH="$VENV_DIR/Scripts/activate"
else
    # Linux / macOS
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_CMD="python3"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_CMD="python"
    else
        echo "ERROR: Could not find python3 or python on PATH."
        exit 1
    fi
    ACTIVATE_PATH="$VENV_DIR/bin/activate"
fi

log "Using python command: $PYTHON_CMD"
log "Will activate venv from: $ACTIVATE_PATH"

########################################
# 1. Check .env.init exists
########################################
if [ ! -f ".env.init" ]; then
    echo ""
    echo "ERROR: .env.init not found."
    echo "You MUST create .env.init in project root before running this script."
    echo "See header comment in run.sh for the required keys."
    echo ""
    exit 1
fi

########################################
# 1.1 Load .env.init into this shell
# We temporarily enable 'set -a' to auto-export
########################################
set -a
# shellcheck disable=SC1091
source .env.init
set +a

########################################
# 1.2 Validate required env values from .env.init
########################################
REQUIRED_VARS=(
    "APP_NAME"
    "SECRET_KEY"
    "PG_INIT_DB_NAME"
    "PG_INIT_DB_USER"
    "PG_INIT_DB_PASSWORD"
    "PG_INIT_DB_HOST"
    "PG_INIT_DB_PORT"
)

MISSING_ANY=0
for VAR in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!VAR}" ]; then
        echo "ERROR: $VAR is missing or empty in .env.init"
        MISSING_ANY=1
    fi
done

if [ "$MISSING_ANY" -ne 0 ]; then
    echo ""
    echo "One or more required variables are not set in .env.init."
    echo "Please fill all of them before running again."
    exit 1
fi

########################################
# 1.3 Check migrations directory exists
########################################
if [ ! -d "$MIGRATIONS_DIR" ]; then
    echo ""
    echo "ERROR: migrations directory '$MIGRATIONS_DIR' not found."
    echo "Create it (with .sql files) or update MIGRATIONS_DIR in run.sh."
    echo ""
    exit 1
fi

########################################
# 2. Create venv if missing
########################################
if [ ! -d "$VENV_DIR" ]; then
    log "Creating virtual environment in $VENV_DIR"
    "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

########################################
# 3. Activate venv
########################################
if [ ! -f "$ACTIVATE_PATH" ]; then
    echo "ERROR: Can't find venv activate script at $ACTIVATE_PATH"
    echo "Stopping so you don't continue half-configured."
    exit 1
fi

log "Activating virtual environment"
# shellcheck disable=SC1090
source "$ACTIVATE_PATH"

# make Python print logs immediately (nicer for migrations/logging)
export PYTHONUNBUFFERED=1

########################################
# 4. Install Python requirements
########################################
if [ -f "requirements.txt" ]; then
    log "Installing requirements.txt"
    pip install -r requirements.txt
else
    log "requirements.txt not found, continuing without it"
fi

log "Ensuring core bootstrap deps (psycopg2-binary, python-dotenv, uvicorn, fastapi)"
pip install psycopg2-binary python-dotenv uvicorn fastapi

########################################
# 5. Run database initialization + migrations
#    This calls `python -m database_setup --migrations-dir <dir>`
#
#    Inside database_setup it will:
#      - connect to Postgres using PG_INIT_DB_* from .env.init
#      - create {APP_NAME}_db and {APP_NAME}_user if they don't exist
#      - generate .env with runtime creds (PG_DB_NAME, PG_DB_USER, PG_DB_PASSWORD, etc.)
#      - run each SQL file in migrations/ once and record in tbl_migration_history
#      - if .env already exists, it will NOT recreate DB/user; it just runs new migrations
########################################
log "Running database bootstrap & migrations"
"$PYTHON_CMD" -m database_setup --migrations-dir "$MIGRATIONS_DIR"

########################################
# 6. Launch FastAPI backend with uvicorn
########################################
export WATCHFILES_FORCE_POLLING=1

log "Starting FastAPI backend with uvicorn ($BACKEND_APP_MODULE)"
uvicorn "$BACKEND_APP_MODULE" --host "$UVICORN_HOST" --port "$UVICORN_PORT" --reload
