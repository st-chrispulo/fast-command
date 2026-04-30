-- 056_alter_tbl_partner_conversations_add_objectives.sql
--
-- Adds the part3 ("objectives") columns to the partner conversation and
-- history tables. Part3 captures the high-level objectives and intent
-- behind the solutions surfaced in part2.

BEGIN;

-- =========================================================
-- tbl_partner_conversations  -- part3 state + captured data
-- =========================================================

ALTER TABLE tbl_partner_conversations
    ADD COLUMN IF NOT EXISTS objectives_status VARCHAR(50) NULL,
    ADD COLUMN IF NOT EXISTS objectives_summary TEXT NULL,
    ADD COLUMN IF NOT EXISTS objectives_high_level TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS objectives_intent TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS objectives_success_criteria TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS objectives_constraints TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS objectives_confidence NUMERIC(5,4) NULL,
    ADD COLUMN IF NOT EXISTS objectives_missing_fields TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS objectives_last_user_message TEXT NULL,
    ADD COLUMN IF NOT EXISTS objectives_last_assistant_message TEXT NULL,
    ADD COLUMN IF NOT EXISTS objectives_assistant_suggested_answers JSONB NULL;

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_objectives_status
    ON tbl_partner_conversations (objectives_status);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_objectives_high_level_gin
    ON tbl_partner_conversations USING gin (objectives_high_level);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_objectives_intent_gin
    ON tbl_partner_conversations USING gin (objectives_intent);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_objectives_success_criteria_gin
    ON tbl_partner_conversations USING gin (objectives_success_criteria);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_objectives_constraints_gin
    ON tbl_partner_conversations USING gin (objectives_constraints);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_objectives_missing_fields_gin
    ON tbl_partner_conversations USING gin (objectives_missing_fields);


-- =========================================================
-- tbl_partner_conversation_histories  -- part3 snapshot
-- =========================================================

ALTER TABLE tbl_partner_conversation_histories
    ADD COLUMN IF NOT EXISTS objectives_summary TEXT NULL,
    ADD COLUMN IF NOT EXISTS objectives_high_level TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS objectives_intent TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS objectives_success_criteria TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS objectives_constraints TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS objectives_confidence NUMERIC(5,4) NULL,
    ADD COLUMN IF NOT EXISTS objectives_missing_fields TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS objectives_status VARCHAR(50) NULL;

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_objectives_status
    ON tbl_partner_conversation_histories (objectives_status);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_objectives_high_level_gin
    ON tbl_partner_conversation_histories USING gin (objectives_high_level);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_objectives_intent_gin
    ON tbl_partner_conversation_histories USING gin (objectives_intent);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_objectives_success_criteria_gin
    ON tbl_partner_conversation_histories USING gin (objectives_success_criteria);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_objectives_constraints_gin
    ON tbl_partner_conversation_histories USING gin (objectives_constraints);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_objectives_missing_fields_gin
    ON tbl_partner_conversation_histories USING gin (objectives_missing_fields);


-- =========================================================
-- column comments
-- =========================================================

COMMENT ON COLUMN tbl_partner_conversations.objectives_status IS
'State machine for the part3 (objectives) sub-conversation: ask_followup | confirm | complete. NULL = not yet started.';

COMMENT ON COLUMN tbl_partner_conversations.objectives_high_level IS
'High-level objectives the partner wants to achieve through Solitud (short keyword/sentence per entry).';

COMMENT ON COLUMN tbl_partner_conversations.objectives_intent IS
'The why - business rationale for each objective, paired by index where reasonable.';

COMMENT ON COLUMN tbl_partner_conversations.objectives_success_criteria IS
'How success is measured for these objectives (short outcome statements).';

COMMENT ON COLUMN tbl_partner_conversations.objectives_constraints IS
'Known constraints that shape the objectives (budget, timeline, compliance, scope limits).';

COMMIT;
