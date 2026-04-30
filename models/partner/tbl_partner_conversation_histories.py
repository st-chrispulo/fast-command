from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, Numeric, SmallInteger, String, Text, UniqueConstraint, func
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
    part_no = Column(SmallInteger, nullable=False, server_default="1")

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

    # ---- part2: solution overview snapshot ----
    solution_overview_summary = Column(Text, nullable=True)
    solution_overview_matches = Column(JSONB, nullable=False, server_default="[]")
    solution_overview_capability_fits = Column(ARRAY(Text), nullable=False, server_default="{}")
    solution_overview_recommended_focus = Column(ARRAY(Text), nullable=False, server_default="{}")
    solution_overview_confidence = Column(Numeric(5, 4), nullable=True)
    solution_overview_missing_fields = Column(ARRAY(Text), nullable=False, server_default="{}")
    solution_overview_status = Column(String(50), nullable=True)

    # ---- part3: objectives snapshot ----
    objectives_summary = Column(Text, nullable=True)
    objectives_high_level = Column(ARRAY(Text), nullable=False, server_default="{}")
    objectives_intent = Column(ARRAY(Text), nullable=False, server_default="{}")
    objectives_success_criteria = Column(ARRAY(Text), nullable=False, server_default="{}")
    objectives_constraints = Column(ARRAY(Text), nullable=False, server_default="{}")
    objectives_confidence = Column(Numeric(5, 4), nullable=True)
    objectives_missing_fields = Column(ARRAY(Text), nullable=False, server_default="{}")
    objectives_status = Column(String(50), nullable=True)

    # ---- part4: scope & limitations snapshot ----
    scope_summary = Column(Text, nullable=True)
    scope_in_scope = Column(ARRAY(Text), nullable=False, server_default="{}")
    scope_out_of_scope = Column(ARRAY(Text), nullable=False, server_default="{}")
    scope_assumptions = Column(ARRAY(Text), nullable=False, server_default="{}")
    scope_dependencies = Column(ARRAY(Text), nullable=False, server_default="{}")
    scope_limitations = Column(ARRAY(Text), nullable=False, server_default="{}")
    scope_confidence = Column(Numeric(5, 4), nullable=True)
    scope_missing_fields = Column(ARRAY(Text), nullable=False, server_default="{}")
    scope_status = Column(String(50), nullable=True)

    # ---- part5: actors & roles snapshot ----
    actors_summary = Column(Text, nullable=True)
    actors = Column(JSONB, nullable=False, server_default="[]")
    actors_confidence = Column(Numeric(5, 4), nullable=True)
    actors_missing_fields = Column(ARRAY(Text), nullable=False, server_default="{}")
    actors_status = Column(String(50), nullable=True)

    # ---- part6: entity model snapshot ----
    entity_model_summary = Column(Text, nullable=True)
    entity_model = Column(JSONB, nullable=False, server_default="[]")
    entity_model_open_questions = Column(ARRAY(Text), nullable=False, server_default="{}")
    entity_model_confidence = Column(Numeric(5, 4), nullable=True)
    entity_model_missing_fields = Column(ARRAY(Text), nullable=False, server_default="{}")
    entity_model_status = Column(String(50), nullable=True)

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

        # ---- part_no discriminator ----
        Index("idx_tbl_partner_conversation_histories_part_no", "part_no"),

        # ---- part2: solution overview snapshot indexes ----
        Index(
            "idx_tbl_partner_conversation_histories_solution_overview_status",
            "solution_overview_status",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_solution_overview_matches_gin",
            "solution_overview_matches",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_solution_overview_capability_fits_gin",
            "solution_overview_capability_fits",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_solution_overview_recommended_focus_gin",
            "solution_overview_recommended_focus",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_solution_overview_missing_fields_gin",
            "solution_overview_missing_fields",
            postgresql_using="gin",
        ),

        # ---- part3: objectives snapshot indexes ----
        Index(
            "idx_tbl_partner_conversation_histories_objectives_status",
            "objectives_status",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_objectives_high_level_gin",
            "objectives_high_level",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_objectives_intent_gin",
            "objectives_intent",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_objectives_success_criteria_gin",
            "objectives_success_criteria",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_objectives_constraints_gin",
            "objectives_constraints",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_objectives_missing_fields_gin",
            "objectives_missing_fields",
            postgresql_using="gin",
        ),

        # ---- part4: scope & limitations snapshot indexes ----
        Index(
            "idx_tbl_partner_conversation_histories_scope_status",
            "scope_status",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_scope_in_scope_gin",
            "scope_in_scope",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_scope_out_of_scope_gin",
            "scope_out_of_scope",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_scope_assumptions_gin",
            "scope_assumptions",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_scope_dependencies_gin",
            "scope_dependencies",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_scope_limitations_gin",
            "scope_limitations",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_scope_missing_fields_gin",
            "scope_missing_fields",
            postgresql_using="gin",
        ),

        # ---- part5: actors & roles snapshot indexes ----
        Index(
            "idx_tbl_partner_conversation_histories_actors_status",
            "actors_status",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_actors_gin",
            "actors",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_actors_missing_fields_gin",
            "actors_missing_fields",
            postgresql_using="gin",
        ),

        # ---- part6: entity model snapshot indexes ----
        Index(
            "idx_tbl_partner_conversation_histories_entity_model_status",
            "entity_model_status",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_entity_model_gin",
            "entity_model",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_entity_model_open_questions_gin",
            "entity_model_open_questions",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_histories_entity_model_missing_fields_gin",
            "entity_model_missing_fields",
            postgresql_using="gin",
        ),
    )


__all__ = ["PartnerConversationHistory"]
