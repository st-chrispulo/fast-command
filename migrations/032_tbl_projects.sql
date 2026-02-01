-- 0xx_tbl_projects.sql
-- EXACT mirror of tbl_comp_pages: columns, trigger, indexes
-- but for tbl_projects

-- ========= Up =========

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS tbl_projects (
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

CREATE OR REPLACE FUNCTION trg_set_updated_at_tbl_projects()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tbl_projects_touch ON tbl_projects;
CREATE TRIGGER trg_tbl_projects_touch
BEFORE UPDATE ON tbl_projects
FOR EACH ROW
EXECUTE FUNCTION trg_set_updated_at_tbl_projects();

CREATE INDEX IF NOT EXISTS idx_tbl_projects_template_id ON tbl_projects (template_id);
CREATE INDEX IF NOT EXISTS idx_tbl_projects_created_at  ON tbl_projects (created_at);
CREATE INDEX IF NOT EXISTS idx_tbl_projects_images_gin  ON tbl_projects USING GIN (images);
CREATE INDEX IF NOT EXISTS idx_tbl_projects_created_by  ON tbl_projects (created_by);
CREATE INDEX IF NOT EXISTS idx_tbl_projects_updated_by  ON tbl_projects (updated_by);
CREATE INDEX IF NOT EXISTS idx_tbl_projects_tags_gin    ON tbl_projects USING GIN (tags);

-- ========= Down (manual) =========
-- DROP INDEX IF EXISTS idx_tbl_projects_tags_gin;
-- DROP INDEX IF EXISTS idx_tbl_projects_updated_by;
-- DROP INDEX IF EXISTS idx_tbl_projects_created_by;
-- DROP INDEX IF EXISTS idx_tbl_projects_images_gin;
-- DROP INDEX IF EXISTS idx_tbl_projects_created_at;
-- DROP INDEX IF EXISTS idx_tbl_projects_template_id;
-- DROP TRIGGER IF EXISTS trg_tbl_projects_touch ON tbl_projects;
-- DROP FUNCTION IF EXISTS trg_set_updated_at_tbl_projects();
-- DROP TABLE IF EXISTS tbl_projects;
