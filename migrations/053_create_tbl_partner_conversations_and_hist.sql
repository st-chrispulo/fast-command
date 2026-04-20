CREATE TABLE tbl_partner_conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    user_id INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,
    partner_id UUID NULL,
    conversation_key VARCHAR(255) NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'ask_followup',

    partner_name VARCHAR(255) NULL,
    partner_industry VARCHAR(255) NULL,

    partner_job_responsibilities TEXT[] NOT NULL DEFAULT '{}',
    partner_pains TEXT[] NOT NULL DEFAULT '{}',
    partner_wishes TEXT[] NOT NULL DEFAULT '{}',

    partner_customer_segments TEXT[] NOT NULL DEFAULT '{}',
    partner_customer_relationships TEXT[] NOT NULL DEFAULT '{}',
    partner_channels TEXT[] NOT NULL DEFAULT '{}',
    partner_key_activities TEXT[] NOT NULL DEFAULT '{}',
    partner_key_resources TEXT[] NOT NULL DEFAULT '{}',
    partner_key_partners TEXT[] NOT NULL DEFAULT '{}',
    partner_revenue_streams TEXT[] NOT NULL DEFAULT '{}',

    conversation_summary TEXT NULL,
    confidence NUMERIC(5,4) NULL,
    missing_fields TEXT[] NOT NULL DEFAULT '{}',

    last_user_message TEXT NULL,
    last_assistant_message TEXT NULL,
    assistant_suggested_answers JSONB NULL,

    metadata JSONB NULL,

    created_by INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,
    updated_by INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tbl_partner_conversations_user_id
    ON tbl_partner_conversations (user_id);

CREATE INDEX idx_tbl_partner_conversations_partner_id
    ON tbl_partner_conversations (partner_id);

CREATE INDEX idx_tbl_partner_conversations_conversation_key
    ON tbl_partner_conversations (conversation_key);

CREATE INDEX idx_tbl_partner_conversations_status
    ON tbl_partner_conversations (status);

CREATE INDEX idx_tbl_partner_conversations_partner_name
    ON tbl_partner_conversations (partner_name);

CREATE INDEX idx_tbl_partner_conversations_partner_industry
    ON tbl_partner_conversations (partner_industry);

CREATE INDEX idx_tbl_partner_conversations_created_at
    ON tbl_partner_conversations (created_at);

CREATE INDEX idx_tbl_partner_conversations_updated_at
    ON tbl_partner_conversations (updated_at);

CREATE INDEX idx_tbl_partner_conversations_created_by
    ON tbl_partner_conversations (created_by);

CREATE INDEX idx_tbl_partner_conversations_updated_by
    ON tbl_partner_conversations (updated_by);

CREATE INDEX idx_tbl_partner_conversations_partner_job_responsibilities_gin
    ON tbl_partner_conversations USING gin (partner_job_responsibilities);

CREATE INDEX idx_tbl_partner_conversations_partner_pains_gin
    ON tbl_partner_conversations USING gin (partner_pains);

CREATE INDEX idx_tbl_partner_conversations_partner_wishes_gin
    ON tbl_partner_conversations USING gin (partner_wishes);

CREATE INDEX idx_tbl_partner_conversations_partner_customer_segments_gin
    ON tbl_partner_conversations USING gin (partner_customer_segments);

CREATE INDEX idx_tbl_partner_conversations_partner_customer_relationships_gin
    ON tbl_partner_conversations USING gin (partner_customer_relationships);

CREATE INDEX idx_tbl_partner_conversations_partner_channels_gin
    ON tbl_partner_conversations USING gin (partner_channels);

CREATE INDEX idx_tbl_partner_conversations_partner_key_activities_gin
    ON tbl_partner_conversations USING gin (partner_key_activities);

CREATE INDEX idx_tbl_partner_conversations_partner_key_resources_gin
    ON tbl_partner_conversations USING gin (partner_key_resources);

CREATE INDEX idx_tbl_partner_conversations_partner_key_partners_gin
    ON tbl_partner_conversations USING gin (partner_key_partners);

CREATE INDEX idx_tbl_partner_conversations_partner_revenue_streams_gin
    ON tbl_partner_conversations USING gin (partner_revenue_streams);

CREATE INDEX idx_tbl_partner_conversations_missing_fields_gin
    ON tbl_partner_conversations USING gin (missing_fields);

CREATE INDEX idx_tbl_partner_conversations_assistant_suggested_answers_gin
    ON tbl_partner_conversations USING gin (assistant_suggested_answers);

CREATE INDEX idx_tbl_partner_conversations_metadata_gin
    ON tbl_partner_conversations USING gin (metadata);



CREATE TABLE tbl_partner_conversation_histories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    conversation_id UUID NOT NULL REFERENCES tbl_partner_conversations(id) ON UPDATE CASCADE ON DELETE CASCADE,
    user_id INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,
    sequence_no INTEGER NOT NULL,

    user_message TEXT NULL,
    assistant_message TEXT NULL,
    assistant_suggested_answers JSONB NULL,

    partner_name VARCHAR(255) NULL,
    partner_industry VARCHAR(255) NULL,

    partner_job_responsibilities TEXT[] NOT NULL DEFAULT '{}',
    partner_pains TEXT[] NOT NULL DEFAULT '{}',
    partner_wishes TEXT[] NOT NULL DEFAULT '{}',

    partner_customer_segments TEXT[] NOT NULL DEFAULT '{}',
    partner_customer_relationships TEXT[] NOT NULL DEFAULT '{}',
    partner_channels TEXT[] NOT NULL DEFAULT '{}',
    partner_key_activities TEXT[] NOT NULL DEFAULT '{}',
    partner_key_resources TEXT[] NOT NULL DEFAULT '{}',
    partner_key_partners TEXT[] NOT NULL DEFAULT '{}',
    partner_revenue_streams TEXT[] NOT NULL DEFAULT '{}',

    conversation_summary TEXT NULL,
    confidence NUMERIC(5,4) NULL,
    missing_fields TEXT[] NOT NULL DEFAULT '{}',
    status VARCHAR(50) NOT NULL DEFAULT 'ask_followup',
    reason TEXT NULL,

    model_name VARCHAR(100) NULL,
    prompt_version VARCHAR(100) NULL,
    metadata JSONB NULL,

    created_by INTEGER NULL REFERENCES tbl_users(id) ON UPDATE CASCADE ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_tbl_partner_conversation_histories_conversation_seq
        UNIQUE (conversation_id, sequence_no)
);

CREATE INDEX idx_tbl_partner_conversation_histories_conversation_id
    ON tbl_partner_conversation_histories (conversation_id);

CREATE INDEX idx_tbl_partner_conversation_histories_user_id
    ON tbl_partner_conversation_histories (user_id);

CREATE INDEX idx_tbl_partner_conversation_histories_sequence_no
    ON tbl_partner_conversation_histories (sequence_no);

CREATE INDEX idx_tbl_partner_conversation_histories_status
    ON tbl_partner_conversation_histories (status);

CREATE INDEX idx_tbl_partner_conversation_histories_partner_name
    ON tbl_partner_conversation_histories (partner_name);

CREATE INDEX idx_tbl_partner_conversation_histories_partner_industry
    ON tbl_partner_conversation_histories (partner_industry);

CREATE INDEX idx_tbl_partner_conversation_histories_created_at
    ON tbl_partner_conversation_histories (created_at);

CREATE INDEX idx_tbl_partner_conversation_histories_created_by
    ON tbl_partner_conversation_histories (created_by);

CREATE INDEX idx_tbl_partner_conversation_histories_partner_job_responsibilities_gin
    ON tbl_partner_conversation_histories USING gin (partner_job_responsibilities);

CREATE INDEX idx_tbl_partner_conversation_histories_partner_pains_gin
    ON tbl_partner_conversation_histories USING gin (partner_pains);

CREATE INDEX idx_tbl_partner_conversation_histories_partner_wishes_gin
    ON tbl_partner_conversation_histories USING gin (partner_wishes);

CREATE INDEX idx_tbl_partner_conversation_histories_partner_customer_segments_gin
    ON tbl_partner_conversation_histories USING gin (partner_customer_segments);

CREATE INDEX idx_tbl_partner_conversation_histories_partner_customer_relationships_gin
    ON tbl_partner_conversation_histories USING gin (partner_customer_relationships);

CREATE INDEX idx_tbl_partner_conversation_histories_partner_channels_gin
    ON tbl_partner_conversation_histories USING gin (partner_channels);

CREATE INDEX idx_tbl_partner_conversation_histories_partner_key_activities_gin
    ON tbl_partner_conversation_histories USING gin (partner_key_activities);

CREATE INDEX idx_tbl_partner_conversation_histories_partner_key_resources_gin
    ON tbl_partner_conversation_histories USING gin (partner_key_resources);

CREATE INDEX idx_tbl_partner_conversation_histories_partner_key_partners_gin
    ON tbl_partner_conversation_histories USING gin (partner_key_partners);

CREATE INDEX idx_tbl_partner_conversation_histories_partner_revenue_streams_gin
    ON tbl_partner_conversation_histories USING gin (partner_revenue_streams);

CREATE INDEX idx_tbl_partner_conversation_histories_missing_fields_gin
    ON tbl_partner_conversation_histories USING gin (missing_fields);

CREATE INDEX idx_tbl_partner_conversation_histories_assistant_suggested_answers_gin
    ON tbl_partner_conversation_histories USING gin (assistant_suggested_answers);

CREATE INDEX idx_tbl_partner_conversation_histories_metadata_gin
    ON tbl_partner_conversation_histories USING gin (metadata);