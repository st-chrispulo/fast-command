-- 058_alter_tbl_partner_conversations_drop_solution_overview_out_of_scope.sql
--
-- Drops the global ``solution_overview_out_of_scope`` aggregate from both
-- the conversation and history tables. Out-of-scope is now a global concern
-- owned by part4 (scope & limitations); part2 keeps per-match ``fit``
-- classifications inside ``solution_overview_matches`` (including
-- ``"not_fit"``) but no longer maintains a global aggregate.

BEGIN;

DROP INDEX IF EXISTS idx_tbl_partner_conversations_solution_overview_out_of_scope_gin;
DROP INDEX IF EXISTS idx_tbl_partner_conversation_histories_solution_overview_out_of_scope_gin;

ALTER TABLE tbl_partner_conversations
    DROP COLUMN IF EXISTS solution_overview_out_of_scope;

ALTER TABLE tbl_partner_conversation_histories
    DROP COLUMN IF EXISTS solution_overview_out_of_scope;

COMMIT;
