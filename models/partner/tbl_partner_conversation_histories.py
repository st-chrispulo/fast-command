from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from auth.db import Base


class PartnerConversationHistory(Base):
    __tablename__ = "tbl_partner_conversation_histories"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)

    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tbl_partner_conversations.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    created_by = Column(Integer, ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True)
    user_id = Column(Integer, ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True)
    sequence_no = Column(Integer, nullable=False)

    user_message = Column(Text, nullable=True)
    assistant_message = Column(Text, nullable=True)
    assistant_suggested_answers = Column(JSONB, nullable=True)

    partner_name = Column(String(255), nullable=True)
    partner_industry = Column(String(255), nullable=True)

    partner_job_responsibilities = Column(ARRAY(Text), nullable=False, server_default="{}")
    partner_pains = Column(ARRAY(Text), nullable=False, server_default="{}")
    partner_wishes = Column(ARRAY(Text), nullable=False, server_default="{}")

    partner_customer_segments = Column(ARRAY(Text), nullable=False, server_default="{}")
    partner_customer_relationships = Column(ARRAY(Text), nullable=False, server_default="{}")
    partner_channels = Column(ARRAY(Text), nullable=False, server_default="{}")
    partner_key_activities = Column(ARRAY(Text), nullable=False, server_default="{}")
    partner_key_resources = Column(ARRAY(Text), nullable=False, server_default="{}")
    partner_key_partners = Column(ARRAY(Text), nullable=False, server_default="{}")
    partner_revenue_streams = Column(ARRAY(Text), nullable=False, server_default="{}")

    conversation_summary = Column(Text, nullable=True)
    confidence = Column(Numeric(5, 4), nullable=True)
    missing_fields = Column(ARRAY(Text), nullable=False, server_default="{}")
    status = Column(String(50), nullable=False, server_default="ask_followup")
    reason = Column(Text, nullable=True)

    model_name = Column(String(100), nullable=True)
    prompt_version = Column(String(100), nullable=True)
    metadata_json = Column("metadata", JSONB, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "sequence_no",
            name="uq_tbl_partner_conversation_histories_conversation_seq",
        ),
        Index("idx_tbl_partner_conversation_histories_conversation_id", "conversation_id"),
        Index("idx_tbl_partner_conversation_histories_created_by", "created_by"),
        Index("idx_tbl_partner_conversation_histories_user_id", "user_id"),
        Index("idx_tbl_partner_conversation_histories_sequence_no", "sequence_no"),
        Index("idx_tbl_partner_conversation_histories_status", "status"),
        Index("idx_tbl_partner_conversation_histories_partner_name", "partner_name"),
        Index("idx_tbl_partner_conversation_histories_partner_industry", "partner_industry"),
        Index("idx_tbl_partner_conversation_histories_created_at", "created_at"),
        Index(
            "idx_tbl_partner_conversation_histories_partner_job_responsibilities_gin",
            "partner_job_responsibilities",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_partner_pains_gin",
            "partner_pains",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_partner_wishes_gin",
            "partner_wishes",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_partner_customer_segments_gin",
            "partner_customer_segments",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_partner_customer_relationships_gin",
            "partner_customer_relationships",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_partner_channels_gin",
            "partner_channels",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_partner_key_activities_gin",
            "partner_key_activities",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_partner_key_resources_gin",
            "partner_key_resources",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_partner_key_partners_gin",
            "partner_key_partners",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_partner_revenue_streams_gin",
            "partner_revenue_streams",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_missing_fields_gin",
            "missing_fields",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_assistant_suggested_answers_gin",
            "assistant_suggested_answers",
            postgresql_using="gin",
        ),
        Index("idx_tbl_partner_conversation_histories_metadata_gin", "metadata", postgresql_using="gin"),
    )


__all__ = ["PartnerConversationHistory"]