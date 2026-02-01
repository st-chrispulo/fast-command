ALTER TABLE tbl_comp_layouts
    ADD COLUMN metadata JSONB;

CREATE INDEX idx_tbl_comp_layouts_metadata_gin
    ON tbl_comp_layouts
    USING gin (metadata);
