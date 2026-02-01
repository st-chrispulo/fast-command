-- 043_add_group_id_and_sub_type_to_tbl_comp_layouts.sql
-- Adds: group_id (UUID), sub_type (VARCHAR)
-- Table: tbl_comp_layouts

BEGIN;

ALTER TABLE tbl_comp_layouts
  ADD COLUMN IF NOT EXISTS group_id UUID,
  ADD COLUMN IF NOT EXISTS sub_type VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_layouts_group_id
  ON tbl_comp_layouts (group_id);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_layouts_sub_type
  ON tbl_comp_layouts (sub_type);

COMMIT;

-- ---------------------------
-- DOWN (rollback)
-- ---------------------------
-- BEGIN;
-- DROP INDEX IF EXISTS idx_tbl_comp_layouts_sub_type;
-- DROP INDEX IF EXISTS idx_tbl_comp_layouts_group_id;
-- ALTER TABLE tbl_comp_layouts
--   DROP COLUMN IF EXISTS sub_type,
--   DROP COLUMN IF EXISTS group_id;
-- COMMIT;
