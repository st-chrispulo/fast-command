-- 063_alter_tbl_partner_conversations_add_entity_model.sql
--
-- Adds the part6 ("entity model") columns to the partner conversation
-- and history tables. Part6 turns the prior parts' outputs (customer
-- profile, objectives, scope, actors) plus any user-attached artifacts
-- (Excel/CSV/Word/PDF/screenshots/messages) into a flat, machine-readable
-- entity model that downstream FE/BE generation can consume.
--
-- Each entity entry is a JSONB object roughly shaped like:
--   {
--     "id": "dog.medical_history",
--     "name": "MedicalHistory",
--     "aliases": ["Vet Visit", "Health Record"],
--     "description": "...",
--     "status": "draft" | "confirmed" | "deprecated",
--     "sensitivity": ["pii", "confidential"],
--     "volume_estimate": "~1M",
--     "relationships": [
--       { "kind": "parent", "to": ["dog.uuid"], "cardinality": "1:N", ... },
--       { "kind": "reference", "to": ["vet.uuid"], "cardinality": "N:1", ... }
--     ],
--     "constraints": [{ "kind": "unique", "on": [...], ... }],
--     "attachments": [{ "kind": "sample_data", "path": "...", ... }],
--     "open_questions": [...],
--     "attributes": [
--       { "id": "...", "type": "...", "profile": {...}, ... }
--     ]
--   }

BEGIN;

-- =========================================================
-- tbl_partner_conversations  -- part6 state + captured data
-- =========================================================

ALTER TABLE tbl_partner_conversations
    ADD COLUMN IF NOT EXISTS entity_model_status VARCHAR(50) NULL,
    ADD COLUMN IF NOT EXISTS entity_model_summary TEXT NULL,
    ADD COLUMN IF NOT EXISTS entity_model JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS entity_model_open_questions TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS entity_model_confidence NUMERIC(5,4) NULL,
    ADD COLUMN IF NOT EXISTS entity_model_missing_fields TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS entity_model_last_user_message TEXT NULL,
    ADD COLUMN IF NOT EXISTS entity_model_last_assistant_message TEXT NULL,
    ADD COLUMN IF NOT EXISTS entity_model_assistant_suggested_answers JSONB NULL;

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_entity_model_status
    ON tbl_partner_conversations (entity_model_status);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_entity_model_gin
    ON tbl_partner_conversations USING gin (entity_model);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_entity_model_open_questions_gin
    ON tbl_partner_conversations USING gin (entity_model_open_questions);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_entity_model_missing_fields_gin
    ON tbl_partner_conversations USING gin (entity_model_missing_fields);


-- =========================================================
-- tbl_partner_conversation_histories  -- part6 snapshot
-- =========================================================

ALTER TABLE tbl_partner_conversation_histories
    ADD COLUMN IF NOT EXISTS entity_model_summary TEXT NULL,
    ADD COLUMN IF NOT EXISTS entity_model JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS entity_model_open_questions TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS entity_model_confidence NUMERIC(5,4) NULL,
    ADD COLUMN IF NOT EXISTS entity_model_missing_fields TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS entity_model_status VARCHAR(50) NULL;

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_entity_model_status
    ON tbl_partner_conversation_histories (entity_model_status);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_entity_model_gin
    ON tbl_partner_conversation_histories USING gin (entity_model);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_entity_model_open_questions_gin
    ON tbl_partner_conversation_histories USING gin (entity_model_open_questions);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_entity_model_missing_fields_gin
    ON tbl_partner_conversation_histories USING gin (entity_model_missing_fields);


-- =========================================================
-- column comments
-- =========================================================

COMMENT ON COLUMN tbl_partner_conversations.entity_model_status IS
'State machine for the part6 (entity model) sub-conversation: ask_followup | confirm | complete. NULL = not yet started.';

COMMENT ON COLUMN tbl_partner_conversations.entity_model IS
'JSONB array of entity definitions. Flat list (no nested children); parent/reference relationships are expressed as relationship metadata on each entity. Each entity carries: id, name, aliases, description, status, sensitivity, volume_estimate, relationships, constraints, attachments, open_questions, attributes (with type, profile, sensitivity, confidence, inferred_from). Hierarchical IDs (e.g. dog.medical_history.temperature) keep every node globally addressable.';

COMMENT ON COLUMN tbl_partner_conversations.entity_model_open_questions IS
'Conversation-level open questions the extractor could not resolve and is asking the user about (e.g. "should treatments be a separate entity?").';

COMMIT;
