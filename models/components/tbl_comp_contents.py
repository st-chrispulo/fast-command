# models/components/tbl_comp_contents.py
from sqlalchemy import (
    Column, String, Text, DateTime, Integer, ForeignKey, Index, func, Table
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY

Base = declarative_base()

tbl_users = Table(
    "tbl_users",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    extend_existing=True,
)


class CompContent(Base):
    __tablename__ = "tbl_comp_contents"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        nullable=False,
    )
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    created_by = Column(
        Integer,
        ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="SET NULL"),
        nullable=True,
    )
    updated_by = Column(
        Integer,
        ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="SET NULL"),
        nullable=True,
    )

    thumbnail = Column(Text, nullable=True)
    images = Column(JSONB, nullable=True)

    # NEW: metadata JSONB column (DB name "metadata", attribute name metadata_json)
    metadata_json = Column("metadata", JSONB, nullable=True)

    template_id = Column(UUID(as_uuid=True), nullable=True)
    file_link = Column(Text, nullable=True)

    tags = Column(ARRAY(String), nullable=False, server_default="{}")

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("idx_tbl_comp_contents_template_id", "template_id"),
        Index("idx_tbl_comp_contents_created_at", "created_at"),
        Index("idx_tbl_comp_contents_images_gin", "images", postgresql_using="gin"),
        Index("idx_tbl_comp_contents_created_by", "created_by"),
        Index("idx_tbl_comp_contents_updated_by", "updated_by"),
        Index("idx_tbl_comp_contents_tags_gin", "tags", postgresql_using="gin"),
        # Optional if you add the GIN index for metadata in the migration:
        # Index("idx_tbl_comp_contents_metadata_gin", "metadata", postgresql_using="gin"),
    )


__all__ = ["CompContent"]
