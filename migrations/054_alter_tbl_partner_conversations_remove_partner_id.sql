-- 054_alter_tbl_partner_conversations_remove_partner_id.sql

BEGIN;

-- =========================================================
-- tbl_partner_conversations
-- Remove partner_id because ownership/actor is now:
--   created_by = conversation creator
--   user_id    = current/latest actor who sent message
-- =========================================================

DROP INDEX IF EXISTS idx_tbl_partner_conversations_partner_id;

ALTER TABLE tbl_partner_conversations
    DROP COLUMN IF EXISTS partner_id;

COMMENT ON COLUMN tbl_partner_conversations.created_by IS
'User who originally created the conversation';

COMMENT ON COLUMN tbl_partner_conversations.user_id IS
'User who most recently sent or is currently sending the message for this conversation';

COMMENT ON COLUMN tbl_partner_conversation_histories.user_id IS
'User who sent the message for this conversation history row';

COMMENT ON COLUMN tbl_partner_conversation_histories.created_by IS
'User who created the history row';

COMMIT;