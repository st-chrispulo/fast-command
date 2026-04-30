-- 059_alter_tbl_partner_conversations_add_scope.sql
--
-- Adds the part4 ("scope & limitations") columns to the partner conversation
-- and history tables. Part4 produces a global view of what is in scope, what
-- is out of scope, the assumptions and dependencies, and the limitations
-- shaping the engagement -- derived from part1 (profile), part2 (solution
-- overview matches with their fit classifications), and part3 (objectives).

BEGIN;

-- =========================================================
-- tbl_partner_conversations  -- part4 state + captured data
-- =========================================================

ALTER TABLE tbl_partner_conversations
    ADD COLUMN IF NOT EXISTS scope_status VARCHAR(50) NULL,
    ADD COLUMN IF NOT EXISTS scope_summary TEXT NULL,
    ADD COLUMN IF NOT EXISTS scope_in_scope TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS scope_out_of_scope TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS scope_assumptions TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS scope_dependencies TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS scope_limitations TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS scope_confidence NUMERIC(5,4) NULL,
    ADD COLUMN IF NOT EXISTS scope_missing_fields TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS scope_last_user_message TEXT NULL,
    ADD COLUMN IF NOT EXISTS scope_last_assistant_message TEXT NULL,
    ADD COLUMN IF NOT EXISTS scope_assistant_suggested_answers JSONB NULL;

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_scope_status
    ON tbl_partner_conversations (scope_status);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_scope_in_scope_gin
    ON tbl_partner_conversations USING gin (scope_in_scope);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_scope_out_of_scope_gin
    ON tbl_partner_conversations USING gin (scope_out_of_scope);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_scope_assumptions_gin
    ON tbl_partner_conversations USING gin (scope_assumptions);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_scope_dependencies_gin
    ON tbl_partner_conversations USING gin (scope_dependencies);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_scope_limitations_gin
    ON tbl_partner_conversations USING gin (scope_limitations);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_scope_missing_fields_gin
    ON tbl_partner_conversations USING gin (scope_missing_fields);


-- =========================================================
-- tbl_partner_conversation_histories  -- part4 snapshot
-- =========================================================

ALTER TABLE tbl_partner_conversation_histories
    ADD COLUMN IF NOT EXISTS scope_summary TEXT NULL,
    ADD COLUMN IF NOT EXISTS scope_in_scope TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS scope_out_of_scope TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS scope_assumptions TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS scope_dependencies TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS scope_limitations TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS scope_confidence NUMERIC(5,4) NULL,
    ADD COLUMN IF NOT EXISTS scope_missing_fields TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS scope_status VARCHAR(50) NULL;

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_scope_status
    ON tbl_partner_conversation_histories (scope_status);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_scope_in_scope_gin
    ON tbl_partner_conversation_histories USING gin (scope_in_scope);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_scope_out_of_scope_gin
    ON tbl_partner_conversation_histories USING gin (scope_out_of_scope);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_scope_assumptions_gin
    ON tbl_partner_conversation_histories USING gin (scope_assumptions);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_scope_dependencies_gin
    ON tbl_partner_conversation_histories USING gin (scope_dependencies);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_scope_limitations_gin
    ON tbl_partner_conversation_histories USING gin (scope_limitations);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_scope_missing_fields_gin
    ON tbl_partner_conversation_histories USING gin (scope_missing_fields);


-- =========================================================
-- column comments
-- =========================================================

COMMENT ON COLUMN tbl_partner_conversations.scope_status IS
'State machine for the part4 (scope & limitations) sub-conversation: ask_followup | confirm | complete. NULL = not yet started.';

COMMENT ON COLUMN tbl_partner_conversations.scope_in_scope IS
'Items the engagement will deliver. Each entry is a short keyword/sentence.';

COMMENT ON COLUMN tbl_partner_conversations.scope_out_of_scope IS
'Items explicitly outside this engagement. Includes capability not_fits, deferrals, and explicit exclusions.';

COMMENT ON COLUMN tbl_partner_conversations.scope_assumptions IS
'Working assumptions the scope rests on (e.g. "partner provides API access", "data already structured").';

COMMENT ON COLUMN tbl_partner_conversations.scope_dependencies IS
'External dependencies that must be satisfied for the scope to hold (e.g. integration access, third-party approvals).';

COMMENT ON COLUMN tbl_partner_conversations.scope_limitations IS
'Constraints/limits that shape the boundary (technical, time, budget, compliance).';

COMMIT;
