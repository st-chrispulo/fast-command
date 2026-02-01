-- 0xx_tbl_funnels.sql
-- EXACT mirror of tbl_comp_pages: columns, trigger, indexes
-- but for tbl_funnels

-- ========= Up =========

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS tbl_funnels (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name VARCHAR(255) NOT NULL,
  description TEXT NULL,

  created_by INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,
  updated_by INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,

  thumbnail TEXT NULL,
  images JSONB NULL,
  template_id UUID NULL,
  file_link TEXT NULL,

  -- exact addition used for contents/pages
  tags TEXT[] NOT NULL DEFAULT '{}'::text[],

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION trg_set_updated_at_tbl_funnels()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tbl_funnels_touch ON tbl_funnels;
CREATE TRIGGER trg_tbl_funnels_touch
BEFORE UPDATE ON tbl_funnels
FOR EACH ROW
EXECUTE FUNCTION trg_set_updated_at_tbl_funnels();

CREATE INDEX IF NOT EXISTS idx_tbl_funnels_template_id ON tbl_funnels (template_id);
CREATE INDEX IF NOT EXISTS idx_tbl_funnels_created_at  ON tbl_funnels (created_at);
CREATE INDEX IF NOT EXISTS idx_tbl_funnels_images_gin  ON tbl_funnels USING GIN (images);
CREATE INDEX IF NOT EXISTS idx_tbl_funnels_created_by  ON tbl_funnels (created_by);
CREATE INDEX IF NOT EXISTS idx_tbl_funnels_updated_by  ON tbl_funnels (updated_by);
CREATE INDEX IF NOT EXISTS idx_tbl_funnels_tags_gin    ON tbl_funnels USING GIN (tags);

-- ========= Down (manual) =========
-- DROP INDEX IF EXISTS idx_tbl_funnels_tags_gin;
-- DROP INDEX IF EXISTS idx_tbl_funnels_updated_by;
-- DROP INDEX IF EXISTS idx_tbl_funnels_created_by;
-- DROP INDEX IF EXISTS idx_tbl_funnels_images_gin;
-- DROP INDEX IF EXISTS idx_tbl_funnels_created_at;
-- DROP INDEX IF EXISTS idx_tbl_funnels_template_id;
-- DROP TRIGGER IF EXISTS trg_tbl_funnels_touch ON tbl_funnels;
-- DROP FUNCTION IF EXISTS trg_set_updated_at_tbl_funnels();
-- DROP TABLE IF EXISTS tbl_funnels;
