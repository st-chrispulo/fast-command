-- 047_add_group_id_and_sub_type_to_tbl_comp_pages.sql

BEGIN;

-- 1) Add columns
ALTER TABLE tbl_comp_pages
  ADD COLUMN IF NOT EXISTS group_id uuid;

ALTER TABLE tbl_comp_pages
  ADD COLUMN IF NOT EXISTS sub_type varchar(255);

-- 2) Add indexes (safe if re-run)
CREATE INDEX IF NOT EXISTS idx_tbl_comp_pages_group_id
  ON tbl_comp_pages (group_id);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_pages_sub_type
  ON tbl_comp_pages (sub_type);

COMMIT;
