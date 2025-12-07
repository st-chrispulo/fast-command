ALTER TABLE tbl_projects
    ADD COLUMN metadata JSONB;

CREATE INDEX idx_tbl_projects_metadata_gin
    ON tbl_projects
    USING gin (metadata);
