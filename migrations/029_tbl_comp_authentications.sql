-- 029_tbl_comp_authentications.sql
-- EXACT mirror of tbl_comp_layouts, except:
--   file_link TEXT -> file_links JSONB

-- ========= Up =========

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS tbl_comp_authentications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name VARCHAR(255) NOT NULL,
  description TEXT NULL,

  created_by INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,
  updated_by INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,

  thumbnail TEXT NULL,
  images JSONB NULL,
  template_id UUID NULL,
  file_links JSONB NULL,         -- changed from file_link TEXT to JSONB

  -- same tag structure as contents/layouts
  tags TEXT[] NOT NULL DEFAULT '{}'::text[],

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION trg_set_updated_at_tbl_comp_authentications()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tbl_comp_authentications_touch ON tbl_comp_authentications;
CREATE TRIGGER trg_tbl_comp_authentications_touch
BEFORE UPDATE ON tbl_comp_authentications
FOR EACH ROW
EXECUTE FUNCTION trg_set_updated_at_tbl_comp_authentications();

CREATE INDEX IF NOT EXISTS idx_tbl_comp_authentications_template_id ON tbl_comp_authentications (template_id);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_authentications_created_at  ON tbl_comp_authentications (created_at);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_authentications_images_gin  ON tbl_comp_authentications USING GIN (images);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_authentications_created_by  ON tbl_comp_authentications (created_by);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_authentications_updated_by  ON tbl_comp_authentications (updated_by);
CREATE INDEX IF NOT EXISTS idx_tbl_comp_authentications_tags_gin    ON tbl_comp_authentications USING GIN (tags);
-- Optional (enable if you plan to query keys/values inside file_links frequently):
-- CREATE INDEX IF NOT EXISTS idx_tbl_comp_authentications_file_links_gin ON tbl_comp_authentications USING GIN (file_links);

-- ========= Down (manual) =========
-- DROP INDEX IF EXISTS idx_tbl_comp_authentications_tags_gin;
-- DROP INDEX IF EXISTS idx_tbl_comp_authentications_updated_by;
-- DROP INDEX IF EXISTS idx_tbl_comp_authentications_created_by;
-- DROP INDEX IF EXISTS idx_tbl_comp_authentications_images_gin;
-- DROP INDEX IF EXISTS idx_tbl_comp_authentications_created_at;
-- DROP INDEX IF EXISTS idx_tbl_comp_authentications_template_id;
-- DROP INDEX IF EXISTS idx_tbl_comp_authentications_file_links_gin; -- if created
-- DROP TRIGGER IF EXISTS trg_tbl_comp_authentications_touch ON tbl_comp_authentications;
-- DROP FUNCTION IF EXISTS trg_set_updated_at_tbl_comp_authentications();
-- DROP TABLE IF EXISTS tbl_comp_authentications;
