-- 028_tbl_comp_pages.sql
-- EXACT mirror of tbl_comp_contents: columns, trigger, indexes

-- ========= Up =========

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS tbl_comp_pages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name VARCHAR(255) NOT NULL,
  description TEXT NULL,

  created_by INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,
  updated_by INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,

  thumbnail TEXT NULL,
  images JSONB NULL,
  template_id UUID NULL,
  file_link TEXT NULL,

  -- exact addition used for contents
  tags TEXT[] NOT NULL DEFAULT '{}'::text[],

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION trg_set_updated_at_tbl_comp_pages()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tbl_comp_pages_touch ON tbl_comp_pages;
CREATE TRIGGER trg_tbl_comp_pages_touch
BEFORE UPDATE ON tbl_comp_pages
FOR EACH ROW
EXECUTE FUNCTION trg_set_updated_at_tbl_comp_pages();

CREATE INDEX IF NOT EXISTS idx_tbl_comp_pages_template_id ON tbl_comp_pages (template_id);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_pages_created_at  ON tbl_comp_pages (created_at);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_pages_images_gin  ON tbl_comp_pages USING GIN (images);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_pages_created_by  ON tbl_comp_pages (created_by);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_pages_updated_by  ON tbl_comp_pages (updated_by);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_pages_tags_gin    ON tbl_comp_pages USING GIN (tags);

-- ========= Down (manual) =========
-- DROP INDEX IF EXISTS idx_tbl_comp_pages_tags_gin;
-- DROP INDEX IF EXISTS idx_tbl_comp_pages_updated_by;
-- DROP INDEX IF EXISTS idx_tbl_comp_pages_created_by;
-- DROP INDEX IF EXISTS idx_tbl_comp_pages_images_gin;
-- DROP INDEX IF EXISTS idx_tbl_comp_pages_created_at;
-- DROP INDEX IF EXISTS idx_tbl_comp_pages_template_id;
-- DROP TRIGGER IF EXISTS trg_tbl_comp_pages_touch ON tbl_comp_pages;
-- DROP FUNCTION IF EXISTS trg_set_updated_at_tbl_comp_pages();
-- DROP TABLE IF EXISTS tbl_comp_pages;
