-- 033: Add metadata column to tbl_comp_contents

ALTER TABLE tbl_comp_contents
    ADD COLUMN metadata JSONB NULL;

-- Optional but recommended if you'll query into metadata (e.g. metadata->>'foo')
CREATE INDEX idx_tbl_comp_contents_metadata_gin
    ON tbl_comp_contents
    USING gin (metadata);
