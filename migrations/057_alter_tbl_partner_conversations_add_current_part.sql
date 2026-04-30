-- 057_alter_tbl_partner_conversations_add_current_part.sql
--
-- Adds the dispatcher-owned `current_part` column. The dispatcher
-- (partner/chat/dispatch) reads this to decide which sub-conversation
-- to forward the user's message to, and advances it when the active
-- sub-conversation reports its local status as "complete".
--
-- Mapping of values is owned in code (commands/partner/_shared.STAGE_REGISTRY):
--   1 -> customer_profile  (part1)
--   2 -> solution_overview (part2)
--   3 -> objectives        (part3)
--   N (past last) -> flow is complete

BEGIN;

ALTER TABLE tbl_partner_conversations
    ADD COLUMN IF NOT EXISTS current_part SMALLINT NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_current_part
    ON tbl_partner_conversations (current_part);

COMMENT ON COLUMN tbl_partner_conversations.current_part IS
'Dispatcher-owned pointer to the active sub-conversation. 1=customer_profile, 2=solution_overview, 3=objectives. When the value is past the last registered stage the planning flow is complete.';

COMMIT;
