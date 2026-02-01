-- 042_add_group_id_and_sub_type_to_tbl_comp_contents.sql
-- Adds: group_id (UUID), sub_type (VARCHAR)
-- Table: tbl_comp_contents

BEGIN;

ALTER TABLE tbl_comp_contents
  ADD COLUMN IF NOT EXISTS group_id UUID,
  ADD COLUMN IF NOT EXISTS sub_type VARCHAR(255);

-- Optional but recommended indexes (safe for filtering/sorting)
CREATE INDEX IF NOT EXISTS idx_tbl_comp_contents_group_id
  ON tbl_comp_contents (group_id);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_contents_sub_type
  ON tbl_comp_contents (sub_type);

COMMIT;

-- ---------------------------
-- DOWN (rollback)
-- ---------------------------
-- BEGIN;
-- DROP INDEX IF EXISTS idx_tbl_comp_contents_sub_type;
-- DROP INDEX IF EXISTS idx_tbl_comp_contents_group_id;
-- ALTER TABLE tbl_comp_contents
--   DROP COLUMN IF EXISTS sub_type,
--   DROP COLUMN IF EXISTS group_id;
-- COMMIT;
