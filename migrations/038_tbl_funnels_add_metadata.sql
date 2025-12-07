ALTER TABLE tbl_funnels
    ADD COLUMN metadata JSONB;

CREATE INDEX idx_tbl_funnels_metadata_gin
    ON tbl_funnels
    USING gin (metadata);
