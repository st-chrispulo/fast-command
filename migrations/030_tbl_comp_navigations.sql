-- 0xx_tbl_comp_navigations.sql
-- EXACT mirror of tbl_comp_authentications, except table name:
--   tbl_comp_authentications -> tbl_comp_navigations

-- ========= Up =========

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS tbl_comp_navigations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name VARCHAR(255) NOT NULL,
  description TEXT NULL,

  created_by INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,
  updated_by INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,

  thumbnail TEXT NULL,
  images JSONB NULL,
  template_id UUID NULL,
  file_links JSONB NULL,         -- same as authentications: JSONB

  -- same tag structure as contents/layouts/authentications
  tags TEXT[] NOT NULL DEFAULT '{}'::text[],

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION trg_set_updated_at_tbl_comp_navigations()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tbl_comp_navigations_touch ON tbl_comp_navigations;
CREATE TRIGGER trg_tbl_comp_navigations_touch
BEFORE UPDATE ON tbl_comp_navigations
FOR EACH ROW
EXECUTE FUNCTION trg_set_updated_at_tbl_comp_navigations();

CREATE INDEX IF NOT EXISTS idx_tbl_comp_navigations_template_id ON tbl_comp_navigations (template_id);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_navigations_created_at  ON tbl_comp_navigations (created_at);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_navigations_images_gin  ON tbl_comp_navigations USING GIN (images);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_navigations_created_by  ON tbl_comp_navigations (created_by);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_navigations_updated_by  ON tbl_comp_navigations (updated_by);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_navigations_tags_gin    ON tbl_comp_navigations USING GIN (tags);
-- Optional (enable if you plan to query keys/values inside file_links frequently):
-- CREATE INDEX IF NOT EXISTS idx_tbl_comp_navigations_file_links_gin ON tbl_comp_navigations USING GIN (file_links);

-- ========= Down (manual) =========
-- DROP INDEX IF EXISTS idx_tbl_comp_navigations_tags_gin;
-- DROP INDEX IF EXISTS idx_tbl_comp_navigations_updated_by;
-- DROP INDEX IF EXISTS idx_tbl_comp_navigations_created_by;
-- DROP INDEX IF EXISTS idx_tbl_comp_navigations_images_gin;
-- DROP INDEX IF EXISTS idx_tbl_comp_navigations_created_at;
-- DROP INDEX IF EXISTS idx_tbl_comp_navigations_template_id;
-- DROP INDEX IF EXISTS idx_tbl_comp_navigations_file_links_gin; -- if created
-- DROP TRIGGER IF EXISTS trg_tbl_comp_navigations_touch ON tbl_comp_navigations;
-- DROP FUNCTION IF EXISTS trg_set_updated_at_tbl_comp_navigations();
-- DROP TABLE IF EXISTS tbl_comp_navigations;
