-- 048_update_tbl_comp_authentications_file_link_group_id_sub_type.sql

BEGIN;

-- 1) Add new columns
ALTER TABLE tbl_comp_authentications
  ADD COLUMN IF NOT EXISTS file_link text;

ALTER TABLE tbl_comp_authentications
  ADD COLUMN IF NOT EXISTS group_id uuid;

ALTER TABLE tbl_comp_authentications
  ADD COLUMN IF NOT EXISTS sub_type varchar(255);

-- 2) Migrate data from file_links -> file_link (best-effort)
--    - if file_links is a JSON array, take first element
--    - if file_links is a JSON string, take it as-is
UPDATE tbl_comp_authentications
SET file_link = CASE
  WHEN file_links IS NULL THEN NULL
  WHEN jsonb_typeof(file_links) = 'array'  THEN file_links->>0
  WHEN jsonb_typeof(file_links) = 'string' THEN file_links #>> '{}'
  ELSE NULL
END
WHERE file_link IS NULL;

-- 3) Drop old column (since multiple files no longer supported)
ALTER TABLE tbl_comp_authentications
  DROP COLUMN IF EXISTS file_links;

-- 4) Add indexes (safe if re-run)
CREATE INDEX IF NOT EXISTS idx_tbl_comp_authentications_group_id
  ON tbl_comp_authentications (group_id);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_authentications_sub_type
  ON tbl_comp_authentications (sub_type);

COMMIT;
