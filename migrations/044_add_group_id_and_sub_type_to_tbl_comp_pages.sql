-- 044_add_group_id_and_sub_type_to_tbl_comp_pages.sql
-- Adds: group_id (UUID), sub_type (VARCHAR)
-- Table: tbl_comp_pages
--
-- NOTE on numbering:
-- - You said last migration is 041.
-- - If you already used 042 for contents and 043 for layouts, then pages should be 044.
-- - If not, rename this file number to match your sequence.

BEGIN;

ALTER TABLE tbl_comp_pages
  ADD COLUMN IF NOT EXISTS group_id UUID,
  ADD COLUMN IF NOT EXISTS sub_type VARCHAR(255);

-- Recommended indexes
CREATE INDEX IF NOT EXISTS idx_tbl_comp_pages_group_id
  ON tbl_comp_pages (group_id);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_pages_sub_type
  ON tbl_comp_pages (sub_type);

COMMIT;

-- ---------------------------
-- DOWN (rollback)
-- ---------------------------
-- BEGIN;
-- DROP INDEX IF EXISTS idx_tbl_comp_pages_sub_type;
-- DROP INDEX IF EXISTS idx_tbl_comp_pages_group_id;
-- ALTER TABLE tbl_comp_pages
--   DROP COLUMN IF EXISTS sub_type,
--   DROP COLUMN IF EXISTS group_id;
-- COMMIT;
