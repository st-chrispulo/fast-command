-- 025_alter_tbl_comp_contents_add_tags.sql
-- Adds tags TEXT[] column + GIN index to tbl_comp_contents (no subqueries in CHECK)

-- ========= Up =========

-- Add the column; keep it non-null with empty-array default
ALTER TABLE tbl_comp_contents
  ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}'::text[];

-- If an older constraint name somehow exists from a failed attempt, drop it first
ALTER TABLE tbl_comp_contents
  DROP CONSTRAINT IF EXISTS chk_tbl_comp_contents_tags_no_blank;

-- Re-add the clean CHECK: disallow empty-string elements (no subquery)
ALTER TABLE tbl_comp_contents
  ADD CONSTRAINT chk_tbl_comp_contents_tags_no_blank
  CHECK (array_position(tags, '') IS NULL);

-- GIN index for overlap/containment queries
CREATE INDEX IF NOT EXISTS idx_tbl_comp_contents_tags_gin
  ON tbl_comp_contents USING GIN (tags);

-- ========= Down =========
-- DROP INDEX IF EXISTS idx_tbl_comp_contents_tags_gin;
-- ALTER TABLE tbl_comp_contents DROP CONSTRAINT IF EXISTS chk_tbl_comp_contents_tags_no_blank;
-- ALTER TABLE tbl_comp_contents DROP COLUMN IF EXISTS tags;
