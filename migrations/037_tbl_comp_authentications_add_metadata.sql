ALTER TABLE tbl_comp_authentications
    ADD COLUMN metadata JSONB;

CREATE INDEX idx_tbl_comp_authentications_metadata_gin
    ON tbl_comp_authentications
    USING gin (metadata);
