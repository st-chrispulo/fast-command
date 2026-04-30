-- 062_alter_tbl_partner_conversations_add_field_candidates_and_sources.sql
--
-- Adds two JSONB columns to tbl_partner_conversations to support the
-- attachment-driven field upsert flow:
--
--   field_candidates JSONB
--     Accumulated, conversation-wide bucket of extracted-field findings that
--     were NOT auto-applied. Two sources feed it:
--       (a) confidence in [0.5, 0.9) -- below auto-upsert threshold
--       (b) confidence >= 0.9 but blocked by a user-set scalar (conflict)
--     Shape:
--       {
--         "partner_industry": [
--           { "value": "B2B SaaS", "confidence": 0.74,
--             "evidence": "...", "attachment_id": "<uuid>",
--             "filename": "deck.pdf", "blocked_reason": null,
--             "captured_at": "2026-04-29T12:34:56Z" }
--         ],
--         "partner_pains": [ ... ],
--         ...
--       }
--     The FE can render these as "we found this in your file -- want to use
--     it?" prompts.
--
--   field_sources JSONB
--     Audit trail per stage column. Records where the currently-stored value
--     came from -- user input vs a specific attachment.
--     Shape:
--       {
--         "partner_name":     { "source": "user_message", "set_at": "..." },
--         "partner_industry": { "source": "attachment",
--                               "attachment_id": "<uuid>",
--                               "confidence": 0.93,
--                               "set_at": "..." },
--         "partner_pains":    [
--           { "value": "manual onboarding", "source": "attachment",
--             "attachment_id": "<uuid>", "confidence": 0.91, "set_at": "..." },
--           { "value": "slow support", "source": "user_message", "set_at": "..." }
--         ]
--       }
--     Scalars store one object; arrays store a parallel array of provenance
--     entries. Lets us show the user where each piece of data originated and
--     decide whether a human override should win on re-extract.

BEGIN;

ALTER TABLE tbl_partner_conversations
    ADD COLUMN IF NOT EXISTS field_candidates JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS field_sources   JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_field_candidates_gin
    ON tbl_partner_conversations USING gin (field_candidates);

CREATE INDEX IF NOT EXISTS idx_tbl_partner_conversations_field_sources_gin
    ON tbl_partner_conversations USING gin (field_sources);

COMMENT ON COLUMN tbl_partner_conversations.field_candidates IS
'Conversation-wide bucket of attachment-extracted field findings that were NOT auto-applied (confidence < 0.9 or blocked by an existing user-set scalar). Keyed by stage column name. The FE surfaces these as confirmation prompts.';

COMMENT ON COLUMN tbl_partner_conversations.field_sources IS
'Audit trail per stage column. Tracks whether the current value came from user_message or from a specific attachment, with confidence and timestamp. Scalars store one provenance object; arrays store a parallel array of per-element provenance entries.';

COMMIT;
