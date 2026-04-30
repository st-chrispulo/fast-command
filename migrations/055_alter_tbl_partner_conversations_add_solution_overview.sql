-- 055_alter_tbl_partner_conversations_add_solution_overview.sql
--
-- Adds the part2 ("solution overview") columns to the partner conversation
-- and history tables. Part2 uses Solitud's capability catalog to assess
-- whether each captured pain / wish / responsibility is something Solitud
-- can deliver.
--
-- Also introduces tbl_partner_conversation_histories.part_no so we can
-- discriminate which sub-conversation each turn belongs to.

BEGIN;

-- =========================================================
-- tbl_partner_conversations  -- part2 state + captured data
-- =========================================================

ALTER TABLE tbl_partner_conversations
    ADD COLUMN IF NOT EXISTS solution_overview_status VARCHAR(50) NULL,
    ADD COLUMN IF NOT EXISTS solution_overview_summary TEXT NULL,
    ADD COLUMN IF NOT EXISTS solution_overview_matches JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS solution_overview_capability_fits TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS solution_overview_out_of_scope TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS solution_overview_recommended_focus TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS solution_overview_confidence NUMERIC(5,4) NULL,
    ADD COLUMN IF NOT EXISTS solution_overview_missing_fields TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS solution_overview_last_user_message TEXT NULL,
    ADD COLUMN IF NOT EXISTS solution_overview_last_assistant_message TEXT NULL,
    ADD COLUMN IF NOT EXISTS solution_overview_assistant_suggested_answers JSONB NULL;

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_solution_overview_status
    ON tbl_partner_conversations (solution_overview_status);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_solution_overview_matches_gin
    ON tbl_partner_conversations USING gin (solution_overview_matches);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_solution_overview_capability_fits_gin
    ON tbl_partner_conversations USING gin (solution_overview_capability_fits);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_solution_overview_out_of_scope_gin
    ON tbl_partner_conversations USING gin (solution_overview_out_of_scope);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_solution_overview_recommended_focus_gin
    ON tbl_partner_conversations USING gin (solution_overview_recommended_focus);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_solution_overview_missing_fields_gin
    ON tbl_partner_conversations USING gin (solution_overview_missing_fields);


-- =========================================================
-- tbl_partner_conversation_histories  -- part2 snapshot + part_no
-- =========================================================

ALTER TABLE tbl_partner_conversation_histories
    ADD COLUMN IF NOT EXISTS part_no SMALLINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS solution_overview_summary TEXT NULL,
    ADD COLUMN IF NOT EXISTS solution_overview_matches JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS solution_overview_capability_fits TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS solution_overview_out_of_scope TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS solution_overview_recommended_focus TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS solution_overview_confidence NUMERIC(5,4) NULL,
    ADD COLUMN IF NOT EXISTS solution_overview_missing_fields TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS solution_overview_status VARCHAR(50) NULL;

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_part_no
    ON tbl_partner_conversation_histories (part_no);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_solution_overview_status
    ON tbl_partner_conversation_histories (solution_overview_status);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_solution_overview_matches_gin
    ON tbl_partner_conversation_histories USING gin (solution_overview_matches);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_solution_overview_capability_fits_gin
    ON tbl_partner_conversation_histories USING gin (solution_overview_capability_fits);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_solution_overview_out_of_scope_gin
    ON tbl_partner_conversation_histories USING gin (solution_overview_out_of_scope);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_solution_overview_recommended_focus_gin
    ON tbl_partner_conversation_histories USING gin (solution_overview_recommended_focus);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversation_histories_solution_overview_missing_fields_gin
    ON tbl_partner_conversation_histories USING gin (solution_overview_missing_fields);


-- =========================================================
-- column comments
-- =========================================================

COMMENT ON COLUMN tbl_partner_conversations.solution_overview_status IS
'State machine for the part2 (solution overview) sub-conversation: ask_followup | confirm | complete. NULL = not yet started.';

COMMENT ON COLUMN tbl_partner_conversations.solution_overview_matches IS
'JSONB array of pain/wish/responsibility -> Solitud capability matches with fit assessment. Each entry: { source_field, source_item, fit, capability_families, rationale, caveats }.';

COMMENT ON COLUMN tbl_partner_conversation_histories.part_no IS
'Which sub-conversation this history row belongs to: 1=part1 (profile/BMC), 2=part2 (solution overview), 3=part3 (objectives), ...';

COMMIT;
