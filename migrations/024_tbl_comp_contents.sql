-- 024_tbl_comp_contents.sql
-- Creates tbl_comp_contents with FKs to tbl_users + safe trigger + indexes

-- ========= Up =========

-- UUID generator
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Ensure users table exists (as specified)
CREATE TABLE IF NOT EXISTS tbl_users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Main table
CREATE TABLE IF NOT EXISTS tbl_comp_contents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name VARCHAR(255) NOT NULL,
  description TEXT NULL,

  created_by INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,
  updated_by INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,

  thumbnail TEXT NULL,
  images JSONB NULL,
  template_id UUID NULL,
  file_link TEXT NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Use a trigger name & function name unique to this table to avoid conflicts
CREATE OR REPLACE FUNCTION trg_set_updated_at_tbl_comp_contents()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tbl_comp_contents_touch ON tbl_comp_contents;
CREATE TRIGGER trg_tbl_comp_contents_touch
BEFORE UPDATE ON tbl_comp_contents
FOR EACH ROW
EXECUTE FUNCTION trg_set_updated_at_tbl_comp_contents();

-- Indexes
CREATE INDEX IF NOT EXISTS idx_tbl_comp_contents_template_id
  ON tbl_comp_contents (template_id);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_contents_created_at
  ON tbl_comp_contents (created_at);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_contents_images_gin
  ON tbl_comp_contents USING GIN (images);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_contents_created_by
  ON tbl_comp_contents (created_by);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_contents_updated_by
  ON tbl_comp_contents (updated_by);

-- ========= Down (manual) =========
-- DROP INDEX IF EXISTS idx_tbl_comp_contents_updated_by;
-- DROP INDEX IF EXISTS idx_tbl_comp_contents_created_by;
-- DROP INDEX IF EXISTS idx_tbl_comp_contents_images_gin;
-- DROP INDEX IF EXISTS idx_tbl_comp_contents_created_at;
-- DROP INDEX IF EXISTS idx_tbl_comp_contents_template_id;
-- DROP TRIGGER IF EXISTS trg_tbl_comp_contents_touch ON tbl_comp_contents;
-- DROP FUNCTION IF EXISTS trg_set_updated_at_tbl_comp_contents();
-- DROP TABLE IF EXISTS tbl_comp_contents;
-- (Typically keep tbl_users and pgcrypto)
