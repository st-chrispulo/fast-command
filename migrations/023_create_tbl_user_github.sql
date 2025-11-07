-- 000X_create_tbl_user_github.sql

-- Pre-req: your tbl_users table (as provided)
-- CREATE TABLE IF NOT EXISTS tbl_users (
--     id SERIAL PRIMARY KEY,
--     username VARCHAR(50) UNIQUE NOT NULL,
--     email VARCHAR(100) UNIQUE NOT NULL,
--     password TEXT NOT NULL,
--     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
-- );

-- Extension for case-insensitive fields
CREATE EXTENSION IF NOT EXISTS citext;

-- === GitHub linkage table (1:1 with tbl_users) ===
CREATE TABLE IF NOT EXISTS tbl_user_github (
    id                 BIGSERIAL PRIMARY KEY,

    -- FK to tbl_users (RELATION YOU ASKED FOR)
    user_id            INTEGER NOT NULL UNIQUE
                           REFERENCES tbl_users(id)
                           ON UPDATE CASCADE
                           ON DELETE CASCADE,

    -- GitHub identity
    github_user_id     BIGINT  NOT NULL UNIQUE,
    login              CITEXT  NOT NULL UNIQUE,  -- e.g., "octocat"
    name               TEXT,
    email              CITEXT,
    avatar_url         TEXT,

    -- Token details (store encrypted in app layer)
    access_token_enc   TEXT,
    refresh_token_enc  TEXT,
    token_type         TEXT,
    token_scope        TEXT,
    expires_at         TIMESTAMPTZ,

    -- Optional snapshot & metadata
    profile_json       JSONB,
    installed_at       TIMESTAMPTZ,
    last_synced_at     TIMESTAMPTZ,

    -- Audit
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Touch trigger for updated_at
CREATE OR REPLACE FUNCTION trg_touch_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tbl_user_github_touch ON tbl_user_github;
CREATE TRIGGER trg_tbl_user_github_touch
BEFORE UPDATE ON tbl_user_github
FOR EACH ROW
EXECUTE FUNCTION trg_touch_updated_at();

-- Indexes
CREATE INDEX IF NOT EXISTS idx_tbl_user_github_user_id        ON tbl_user_github(user_id);
CREATE INDEX IF NOT EXISTS idx_tbl_user_github_login          ON tbl_user_github(login);
CREATE INDEX IF NOT EXISTS idx_tbl_user_github_github_user_id ON tbl_user_github(github_user_id);

-- Defensive length checks (optional)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'chk_tbl_user_github_login_len'
  ) THEN
    ALTER TABLE tbl_user_github
      ADD CONSTRAINT chk_tbl_user_github_login_len
      CHECK (char_length(login::text) BETWEEN 1 AND 100);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'chk_tbl_user_github_name_len'
  ) THEN
    ALTER TABLE tbl_user_github
      ADD CONSTRAINT chk_tbl_user_github_name_len
      CHECK (name IS NULL OR char_length(name) <= 200);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'chk_tbl_user_github_email_len'
  ) THEN
    ALTER TABLE tbl_user_github
      ADD CONSTRAINT chk_tbl_user_github_email_len
      CHECK (email IS NULL OR char_length(email::text) <= 255);
  END IF;
END$$;

-- (Optional) Privileges for app role:
-- GRANT SELECT, INSERT, UPDATE, DELETE ON tbl_user_github TO your_app_role;
-- GRANT USAGE, SELECT ON SEQUENCE tbl_user_github_id_seq TO your_app_role;

-- =========================
-- DOWN (rollback)
-- =========================
-- DROP TRIGGER IF EXISTS trg_tbl_user_github_touch ON tbl_user_github;
-- DROP FUNCTION IF EXISTS trg_touch_updated_at();
-- DROP TABLE IF EXISTS tbl_user_github;
