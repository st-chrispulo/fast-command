ALTER TABLE tbl_comp_pages
    ADD COLUMN metadata JSONB;

CREATE INDEX idx_tbl_comp_pages_metadata_gin
    ON tbl_comp_pages
    USING gin (metadata);
