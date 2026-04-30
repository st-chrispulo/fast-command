from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, Numeric, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from auth.db import Base


class PartnerConversation(Base):
    __tablename__ = "tbl_partner_conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)

    created_by = Column(Integer, ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True)
    user_id = Column(Integer, ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True)
    conversation_key = Column(String(255), nullable=True)
    status = Column(String(50), nullable=False, server_default="ask_followup")
    current_part = Column(SmallInteger, nullable=False, server_default="1")

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

    last_user_message = Column(Text, nullable=True)
    last_assistant_message = Column(Text, nullable=True)
    assistant_suggested_answers = Column(JSONB, nullable=True)

    # ---- part2: solution overview ----
    solution_overview_status = Column(String(50), nullable=True)
    solution_overview_summary = Column(Text, nullable=True)
    solution_overview_matches = Column(JSONB, nullable=False, server_default="[]")
    solution_overview_capability_fits = Column(ARRAY(Text), nullable=False, server_default="{}")
    solution_overview_recommended_focus = Column(ARRAY(Text), nullable=False, server_default="{}")
    solution_overview_confidence = Column(Numeric(5, 4), nullable=True)
    solution_overview_missing_fields = Column(ARRAY(Text), nullable=False, server_default="{}")
    solution_overview_last_user_message = Column(Text, nullable=True)
    solution_overview_last_assistant_message = Column(Text, nullable=True)
    solution_overview_assistant_suggested_answers = Column(JSONB, nullable=True)

    # ---- part3: objectives ----
    objectives_status = Column(String(50), nullable=True)
    objectives_summary = Column(Text, nullable=True)
    objectives_high_level = Column(ARRAY(Text), nullable=False, server_default="{}")
    objectives_intent = Column(ARRAY(Text), nullable=False, server_default="{}")
    objectives_success_criteria = Column(ARRAY(Text), nullable=False, server_default="{}")
    objectives_constraints = Column(ARRAY(Text), nullable=False, server_default="{}")
    objectives_confidence = Column(Numeric(5, 4), nullable=True)
    objectives_missing_fields = Column(ARRAY(Text), nullable=False, server_default="{}")
    objectives_last_user_message = Column(Text, nullable=True)
    objectives_last_assistant_message = Column(Text, nullable=True)
    objectives_assistant_suggested_answers = Column(JSONB, nullable=True)

    # ---- part4: scope & limitations ----
    scope_status = Column(String(50), nullable=True)
    scope_summary = Column(Text, nullable=True)
    scope_in_scope = Column(ARRAY(Text), nullable=False, server_default="{}")
    scope_out_of_scope = Column(ARRAY(Text), nullable=False, server_default="{}")
    scope_assumptions = Column(ARRAY(Text), nullable=False, server_default="{}")
    scope_dependencies = Column(ARRAY(Text), nullable=False, server_default="{}")
    scope_limitations = Column(ARRAY(Text), nullable=False, server_default="{}")
    scope_confidence = Column(Numeric(5, 4), nullable=True)
    scope_missing_fields = Column(ARRAY(Text), nullable=False, server_default="{}")
    scope_last_user_message = Column(Text, nullable=True)
    scope_last_assistant_message = Column(Text, nullable=True)
    scope_assistant_suggested_answers = Column(JSONB, nullable=True)

    # ---- part5: actors & roles ----
    actors_status = Column(String(50), nullable=True)
    actors_summary = Column(Text, nullable=True)
    actors = Column(JSONB, nullable=False, server_default="[]")
    actors_confidence = Column(Numeric(5, 4), nullable=True)
    actors_missing_fields = Column(ARRAY(Text), nullable=False, server_default="{}")
    actors_last_user_message = Column(Text, nullable=True)
    actors_last_assistant_message = Column(Text, nullable=True)
    actors_assistant_suggested_answers = Column(JSONB, nullable=True)

    # ---- part6: entity model ----
    entity_model_status = Column(String(50), nullable=True)
    entity_model_summary = Column(Text, nullable=True)
    entity_model = Column(JSONB, nullable=False, server_default="[]")
    entity_model_open_questions = Column(ARRAY(Text), nullable=False, server_default="{}")
    entity_model_confidence = Column(Numeric(5, 4), nullable=True)
    entity_model_missing_fields = Column(ARRAY(Text), nullable=False, server_default="{}")
    entity_model_last_user_message = Column(Text, nullable=True)
    entity_model_last_assistant_message = Column(Text, nullable=True)
    entity_model_assistant_suggested_answers = Column(JSONB, nullable=True)

    metadata_json = Column("metadata", JSONB, nullable=True)

    # ---- attachment-driven field upsert state ----
    # Conversation-wide bucket of attachment-extracted findings that were NOT
    # auto-applied (confidence < 0.9 or blocked by an existing user-set
    # scalar). Keyed by stage column name.
    field_candidates = Column(JSONB, nullable=False, server_default="{}")
    # Audit trail per stage column: where the current value came from
    # (user_message vs attachment + attachment_id + confidence + set_at).
    field_sources = Column(JSONB, nullable=False, server_default="{}")

    updated_by = Column(Integer, ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_tbl_partner_conversations_created_by", "created_by"),
        Index("idx_tbl_partner_conversations_user_id", "user_id"),
        Index("idx_tbl_partner_conversations_conversation_key", "conversation_key"),
        Index("idx_tbl_partner_conversations_status", "status"),
        Index("idx_tbl_partner_conversations_current_part", "current_part"),
        Index("idx_tbl_partner_conversations_partner_name", "partner_name"),
        Index("idx_tbl_partner_conversations_partner_industry", "partner_industry"),
        Index("idx_tbl_partner_conversations_created_at", "created_at"),
        Index("idx_tbl_partner_conversations_updated_at", "updated_at"),
        Index("idx_tbl_partner_conversations_updated_by", "updated_by"),
        Index(
            "idx_tbl_partner_conversations_partner_job_responsibilities_gin",
            "partner_job_responsibilities",
            postgresql_using="gin",
        ),
        Index("idx_tbl_partner_conversations_partner_pains_gin", "partner_pains", postgresql_using="gin"),
        Index("idx_tbl_partner_conversations_partner_wishes_gin", "partner_wishes", postgresql_using="gin"),
        Index(
            "idx_tbl_partner_conversations_partner_customer_segments_gin",
            "partner_customer_segments",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_partner_customer_relationships_gin",
            "partner_customer_relationships",
            postgresql_using="gin",
        ),
        Index("idx_tbl_partner_conversations_partner_channels_gin", "partner_channels", postgresql_using="gin"),
        Index(
            "idx_tbl_partner_conversations_partner_key_activities_gin",
            "partner_key_activities",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_partner_key_resources_gin",
            "partner_key_resources",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_partner_key_partners_gin",
            "partner_key_partners",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_partner_revenue_streams_gin",
            "partner_revenue_streams",
            postgresql_using="gin",
        ),
        Index("idx_tbl_partner_conversations_missing_fields_gin", "missing_fields", postgresql_using="gin"),
        Index(
            "idx_tbl_partner_conversations_assistant_suggested_answers_gin",
            "assistant_suggested_answers",
            postgresql_using="gin",
        ),
        Index("idx_tbl_partner_conversations_metadata_gin", "metadata", postgresql_using="gin"),

        # ---- part2: solution overview indexes ----
        Index("idx_tbl_partner_conversations_solution_overview_status", "solution_overview_status"),
        Index(
            "idx_tbl_partner_conversations_solution_overview_matches_gin",
            "solution_overview_matches",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_solution_overview_capability_fits_gin",
            "solution_overview_capability_fits",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_solution_overview_recommended_focus_gin",
            "solution_overview_recommended_focus",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_solution_overview_missing_fields_gin",
            "solution_overview_missing_fields",
            postgresql_using="gin",
        ),

        # ---- part3: objectives indexes ----
        Index("idx_tbl_partner_conversations_objectives_status", "objectives_status"),
        Index(
            "idx_tbl_partner_conversations_objectives_high_level_gin",
            "objectives_high_level",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_objectives_intent_gin",
            "objectives_intent",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_objectives_success_criteria_gin",
            "objectives_success_criteria",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_objectives_constraints_gin",
            "objectives_constraints",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_objectives_missing_fields_gin",
            "objectives_missing_fields",
            postgresql_using="gin",
        ),

        # ---- part4: scope & limitations indexes ----
        Index("idx_tbl_partner_conversations_scope_status", "scope_status"),
        Index(
            "idx_tbl_partner_conversations_scope_in_scope_gin",
            "scope_in_scope",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_scope_out_of_scope_gin",
            "scope_out_of_scope",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_scope_assumptions_gin",
            "scope_assumptions",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_scope_dependencies_gin",
            "scope_dependencies",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_scope_limitations_gin",
            "scope_limitations",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_scope_missing_fields_gin",
            "scope_missing_fields",
            postgresql_using="gin",
        ),

        # ---- part5: actors & roles indexes ----
        Index("idx_tbl_partner_conversations_actors_status", "actors_status"),
        Index(
            "idx_tbl_partner_conversations_actors_gin",
            "actors",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_actors_missing_fields_gin",
            "actors_missing_fields",
            postgresql_using="gin",
        ),

        # ---- part6: entity model indexes ----
        Index("idx_tbl_partner_conversations_entity_model_status", "entity_model_status"),
        Index(
            "idx_tbl_partner_conversations_entity_model_gin",
            "entity_model",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_entity_model_open_questions_gin",
            "entity_model_open_questions",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_entity_model_missing_fields_gin",
            "entity_model_missing_fields",
            postgresql_using="gin",
        ),

        # ---- attachment-driven field upsert indexes ----
        Index(
            "idx_tbl_partner_conversations_field_candidates_gin",
            "field_candidates",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversations_field_sources_gin",
            "field_sources",
            postgresql_using="gin",
        ),
    )


__all__ = ["PartnerConversation"]
