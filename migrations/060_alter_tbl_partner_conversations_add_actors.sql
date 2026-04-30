-- 060_alter_tbl_partner_conversations_add_actors.sql
--
-- Adds the part5 ("actors & roles") columns to the partner conversation
-- and history tables. Part5 captures the key actors involved and what their
-- role is. It does NOT capture where each actor sits in the business
-- journey -- that is reserved for a future part7.
--
-- Each actor entry is a JSONB object: { actor, role, notes }.

BEGIN;

-- =========================================================
-- tbl_partner_conversations  -- part5 state + captured data
-- =========================================================

ALTER TABLE tbl_partner_conversations
    ADD COLUMN IF NOT EXISTS actors_status VARCHAR(50) NULL,
    ADD COLUMN IF NOT EXISTS actors_summary TEXT NULL,
    ADD COLUMN IF NOT EXISTS actors JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS actors_confidence NUMERIC(5,4) NULL,
    ADD COLUMN IF NOT EXISTS actors_missing_fields TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS actors_last_user_message TEXT NULL,
    ADD COLUMN IF NOT EXISTS actors_last_assistant_message TEXT NULL,
    ADD COLUMN IF NOT EXISTS actors_assistant_suggested_answers JSONB NULL;

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_actors_status
    ON tbl_partner_conversations (actors_status);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_actors_gin
    ON tbl_partner_conversations USING gin (actors);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_actors_missing_fields_gin
    ON tbl_partner_conversations USING gin (actors_missing_fields);


-- =========================================================
-- tbl_partner_conversation_histories  -- part5 snapshot
-- =========================================================

ALTER TABLE tbl_partner_conversation_histories
    ADD COLUMN IF NOT EXISTS actors_summary TEXT NULL,
    ADD COLUMN IF NOT EXISTS actors JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS actors_confidence NUMERIC(5,4) NULL,
    ADD COLUMN IF NOT EXISTS actors_missing_fields TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS actors_status VARCHAR(50) NULL;

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_actors_status
    ON tbl_partner_conversation_histories (actors_status);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_actors_gin
    ON tbl_partner_conversation_histories USING gin (actors);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_actors_missing_fields_gin
    ON tbl_partner_conversation_histories USING gin (actors_missing_fields);


-- =========================================================
-- column comments
-- =========================================================

COMMENT ON COLUMN tbl_partner_conversations.actors_status IS
'State machine for the part5 (actors & roles) sub-conversation: ask_followup | confirm | complete. NULL = not yet started.';

COMMENT ON COLUMN tbl_partner_conversations.actors IS
'JSONB array of actors. Each entry: { actor, role, notes }. Captures WHO is involved and WHAT their role is. Where each actor sits in the business journey is reserved for part7.';

COMMIT;
