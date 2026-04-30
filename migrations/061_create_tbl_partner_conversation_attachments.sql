-- 061_create_tbl_partner_conversation_attachments.sql
--
-- Stores per-conversation file attachments uploaded via the partner flow.
-- The file bytes themselves live in GCS (gcs_key); this table holds metadata,
-- the LLM-generated summary captured ONCE at upload time, and the extracted
-- field candidates produced from that same pass.
--
-- Design notes:
--   * One LLM call at upload time produces:
--       - summary_text (100-300 word prose, what we drop into part1-5 prompts)
--       - summary      (structured JSONB: purpose, key_topics, key_insights,
--                       structures, entities, etc.)
--       - extracted_fields (per stage column: value + confidence + evidence)
--   * extracted_fields is then applied to the parent conversation row using
--     these rules (handled in code, not in SQL):
--       - confidence >= 0.9   -> auto-upsert (scalars: only if conv col is
--                                empty; arrays: dedupe-append)
--       - 0.5 <= conf < 0.9   -> stashed as candidate (NOT auto-applied)
--       - conf < 0.5          -> dropped
--   * applied_fields / candidate_fields are denormalized snapshots of what
--     this particular attachment contributed; the accumulated view across
--     all attachments lives on tbl_partner_conversations (next migration).
--   * After the summary is written we do NOT keep the raw extracted text.
--     If we ever need to re-extract we re-fetch from GCS via gcs_key.

BEGIN;

CREATE TABLE IF NOT EXISTS tbl_partner_conversation_attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    conversation_id UUID NOT NULL
        REFERENCES tbl_partner_conversations(id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    conversation_key VARCHAR(255) NULL,
    user_id INTEGER NULL
        REFERENCES tbl_users(id)
        ON UPDATE CASCADE ON DELETE SET NULL,

    -- File metadata
    filename VARCHAR(512) NOT NULL,
    gcs_key TEXT NOT NULL,
    content_type VARCHAR(255) NULL,
    size_bytes BIGINT NULL,

    -- Lifecycle
    status VARCHAR(50) NOT NULL DEFAULT 'uploaded',
    classification VARCHAR(100) NULL,
    extraction_error TEXT NULL,

    -- LLM-produced content (single pass, captured at upload)
    summary JSONB NULL,
    summary_text TEXT NULL,
    extracted_fields JSONB NULL,
    applied_fields JSONB NULL,
    candidate_fields JSONB NULL,

    -- Provider trace
    model_provider VARCHAR(50) NULL,
    model_name VARCHAR(100) NULL,
    prompt_version VARCHAR(50) NULL,

    metadata JSONB NULL,

    created_by INTEGER NULL
        REFERENCES tbl_users(id)
        ON UPDATE CASCADE ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Lookups
CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_attachments_conversation_id
    ON tbl_partner_conversation_attachments (conversation_id);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_attachments_conversation_key
    ON tbl_partner_conversation_attachments (conversation_key);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_attachments_user_id
    ON tbl_partner_conversation_attachments (user_id);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_attachments_status
    ON tbl_partner_conversation_attachments (status);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_attachments_classification
    ON tbl_partner_conversation_attachments (classification);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_attachments_created_at
    ON tbl_partner_conversation_attachments (created_at);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_attachments_created_by
    ON tbl_partner_conversation_attachments (created_by);

-- JSONB GIN indexes
CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_attachments_summary_gin
    ON tbl_partner_conversation_attachments USING gin (summary);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_attachments_extracted_fields_gin
    ON tbl_partner_conversation_attachments USING gin (extracted_fields);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_attachments_applied_fields_gin
    ON tbl_partner_conversation_attachments USING gin (applied_fields);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_attachments_candidate_fields_gin
    ON tbl_partner_conversation_attachments USING gin (candidate_fields);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_attachments_metadata_gin
    ON tbl_partner_conversation_attachments USING gin (metadata);

-- Column comments
COMMENT ON TABLE tbl_partner_conversation_attachments IS
'Files attached to a partner conversation. Bytes live in GCS; this row holds metadata plus the upload-time LLM summary used to feed parts 1-5 prompts and pre-populate stage fields.';

COMMENT ON COLUMN tbl_partner_conversation_attachments.gcs_key IS
'GCS object key under the configured bucket; never store the full URL. Use signed URLs or server-side reads via SA creds for retrieval.';

COMMENT ON COLUMN tbl_partner_conversation_attachments.status IS
'Lifecycle: uploaded | extracting | summarizing | ready | failed.';

COMMENT ON COLUMN tbl_partner_conversation_attachments.classification IS
'High-level file class chosen by the summarizer: pdf_report | spreadsheet | image | doc | text | other.';

COMMENT ON COLUMN tbl_partner_conversation_attachments.summary IS
'Structured JSONB summary: { purpose, key_topics, key_insights, structures, entities, page_count, row_count, ... }. Shape varies by classification.';

COMMENT ON COLUMN tbl_partner_conversation_attachments.summary_text IS
'100-300 word prose summary written by the LLM. This is what gets injected into part1-5 prompts as attachment context.';

COMMENT ON COLUMN tbl_partner_conversation_attachments.extracted_fields IS
'Raw extracted-fields output from the summarizer. Shape: { partner_name: {value, confidence, evidence}, partner_pains: [{value, confidence, evidence}, ...], ... }. NOT directly applied -- see applied_fields and candidate_fields.';

COMMENT ON COLUMN tbl_partner_conversation_attachments.applied_fields IS
'Subset of extracted_fields that met confidence >= 0.9 AND passed the conflict policy (scalars: target was empty; arrays: unique additions). These were upserted to tbl_partner_conversations.';

COMMENT ON COLUMN tbl_partner_conversation_attachments.candidate_fields IS
'Subset of extracted_fields that scored 0.5..0.9 OR scored >= 0.9 but were blocked by an existing user-set scalar. Surfaced to the FE for user confirmation.';

COMMIT;
