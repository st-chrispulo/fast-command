from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from auth.db import Base


class PartnerConversationAttachment(Base):
    """File attached to a partner conversation.

    Bytes live in GCS (``gcs_key``). This row carries metadata plus the
    upload-time LLM summary used to feed parts 1-5 prompts and to
    pre-populate stage fields on the parent conversation row.

    Lifecycle (``status``):
        uploaded -> extracting -> summarizing -> ready
                                            \\-> failed
    """

    __tablename__ = "tbl_partner_conversation_attachments"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        nullable=False,
    )

    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "tbl_partner_conversations.id",
            onupdate="CASCADE",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    conversation_key = Column(String(255), nullable=True)
    user_id = Column(
        Integer,
        ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="SET NULL"),
        nullable=True,
    )

    # ---- file metadata -------------------------------------------------
    filename = Column(String(512), nullable=False)
    gcs_key = Column(Text, nullable=False)
    content_type = Column(String(255), nullable=True)
    size_bytes = Column(BigInteger, nullable=True)

    # ---- lifecycle -----------------------------------------------------
    status = Column(String(50), nullable=False, server_default="uploaded")
    classification = Column(String(100), nullable=True)
    extraction_error = Column(Text, nullable=True)

    # ---- LLM-produced content -----------------------------------------
    # Single pass at upload time produces all four of these.
    summary = Column(JSONB, nullable=True)
    summary_text = Column(Text, nullable=True)
    extracted_fields = Column(JSONB, nullable=True)
    applied_fields = Column(JSONB, nullable=True)
    candidate_fields = Column(JSONB, nullable=True)

    # ---- provider trace ------------------------------------------------
    model_provider = Column(String(50), nullable=True)
    model_name = Column(String(100), nullable=True)
    prompt_version = Column(String(50), nullable=True)

    metadata_json = Column("metadata", JSONB, nullable=True)

    created_by = Column(
        Integer,
        ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index(
            "idx_tbl_partner_conversation_attachments_conversation_id",
            "conversation_id",
        ),
        Index(
            "idx_tbl_partner_conversation_attachments_conversation_key",
            "conversation_key",
        ),
        Index(
            "idx_tbl_partner_conversation_attachments_user_id",
            "user_id",
        ),
        Index(
            "idx_tbl_partner_conversation_attachments_status",
            "status",
        ),
        Index(
            "idx_tbl_partner_conversation_attachments_classification",
            "classification",
        ),
        Index(
            "idx_tbl_partner_conversation_attachments_created_at",
            "created_at",
        ),
        Index(
            "idx_tbl_partner_conversation_attachments_created_by",
            "created_by",
        ),
        Index(
            "idx_tbl_partner_conversation_attachments_summary_gin",
            "summary",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_attachments_extracted_fields_gin",
            "extracted_fields",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_attachments_applied_fields_gin",
            "applied_fields",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_attachments_candidate_fields_gin",
            "candidate_fields",
            postgresql_using="gin",
        ),
        Index(
            "idx_tbl_partner_conversation_attachments_metadata_gin",
            "metadata",
            postgresql_using="gin",
        ),
    )


__all__ = ["PartnerConversationAttachment"]
