-- 026_create_tbl_user_tags.sql
-- Per-user tag namespace with composite PK (user_id, name)
-- Up-only migration: create table, index, function and trigger.

-- Ensure referenced table exists (quick guard, optional)
-- Remove or adjust the following check if you run in a different schema.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = current_schema() AND table_name = 'tbl_users'
  ) THEN
    RAISE EXCEPTION 'Required referenced table "%s.%s" not found', current_schema(), 'tbl_users';
  END IF;
END;
$$;

-- Create table
CREATE TABLE IF NOT EXISTS tbl_user_tags (
    user_id     INTEGER NOT NULL REFERENCES tbl_users(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    color_hex   VARCHAR(7),
    description TEXT,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, name)
);

-- Helpful filter index
CREATE INDEX IF NOT EXISTS idx_tbl_user_tags_user_active
    ON tbl_user_tags (user_id, is_active);

-- Table-scoped "touch" function (unique name to this table)
CREATE OR REPLACE FUNCTION trg_touch_tbl_user_tags_updated_at_fn()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := CURRENT_TIMESTAMP;
  RETURN NEW;
END;
$$;

-- If a trigger already exists on this table, drop it first (safe)
DROP TRIGGER IF EXISTS trg_touch_tbl_user_tags_updated_at ON tbl_user_tags;

-- Create trigger
CREATE TRIGGER trg_touch_tbl_user_tags_updated_at
BEFORE UPDATE ON tbl_user_tags
FOR EACH ROW
EXECUTE FUNCTION trg_touch_tbl_user_tags_updated_at_fn();
