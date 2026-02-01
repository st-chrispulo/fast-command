ALTER TABLE tbl_comp_navigations
    ADD COLUMN metadata JSONB;

CREATE INDEX idx_tbl_comp_navigations_metadata_gin
    ON tbl_comp_navigations
    USING gin (metadata);
