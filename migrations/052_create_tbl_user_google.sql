-- 052_create_tbl_user_google.sql

CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS tbl_user_google (
    id                 BIGSERIAL PRIMARY KEY,

    user_id            INTEGER NOT NULL UNIQUE
                           REFERENCES tbl_users(id)
                           ON UPDATE CASCADE
                           ON DELETE CASCADE,

    google_user_id     TEXT     NOT NULL UNIQUE,  -- Google "sub"
    email              CITEXT,
    name               TEXT,
    picture            TEXT,

    access_token_enc   TEXT,
    refresh_token_enc  TEXT,
    token_type         TEXT,
    token_scope        TEXT,
    expires_at         TIMESTAMPTZ,

    profile_json       JSONB,
    last_synced_at     TIMESTAMPTZ,

    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- updated_at trigger
CREATE OR REPLACE FUNCTION trg_touch_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tbl_user_google_touch ON tbl_user_google;
CREATE TRIGGER trg_tbl_user_google_touch
BEFORE UPDATE ON tbl_user_google
FOR EACH ROW
EXECUTE FUNCTION trg_touch_updated_at();

-- indexes
CREATE INDEX IF NOT EXISTS idx_tbl_user_google_user_id
    ON tbl_user_google(user_id);

CREATE INDEX IF NOT EXISTS idx_tbl_user_google_google_user_id
    ON tbl_user_google(google_user_id);

CREATE INDEX IF NOT EXISTS idx_tbl_user_google_email
    ON tbl_user_google(email);

-- optional defensive constraints
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'chk_tbl_user_google_name_len'
  ) THEN
    ALTER TABLE tbl_user_google
      ADD CONSTRAINT chk_tbl_user_google_name_len
      CHECK (name IS NULL OR char_length(name) <= 200);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'chk_tbl_user_google_email_len'
  ) THEN
    ALTER TABLE tbl_user_google
      ADD CONSTRAINT chk_tbl_user_google_email_len
      CHECK (email IS NULL OR char_length(email::text) <= 255);
  END IF;
END$$;

-- =========================
-- DOWN
-- =========================
-- DROP TRIGGER IF EXISTS trg_tbl_user_google_touch ON tbl_user_google;
-- DROP FUNCTION IF EXISTS trg_touch_updated_at();
-- DROP TABLE IF EXISTS tbl_user_google;