from sqlalchemy import (
    Column, String, Text, DateTime, Integer, ForeignKey, Index, func, Table
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY

# If you already have a central Base, import it instead:
# from models.base import Base
Base = declarative_base()

# Minimal FK stub so create_all() resolves tbl_users
tbl_users = Table(
    "tbl_users",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    extend_existing=True,
)


class CompNavigation(Base):
    __tablename__ = "tbl_comp_navigations"

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
    file_links = Column(JSONB, nullable=True)  # JSONB (replaces file_link TEXT)

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
        Index("idx_tbl_comp_navigations_template_id", "template_id"),
        Index("idx_tbl_comp_navigations_created_at", "created_at"),
        Index("idx_tbl_comp_navigations_images_gin", "images", postgresql_using="gin"),
        Index("idx_tbl_comp_navigations_created_by", "created_by"),
        Index("idx_tbl_comp_navigations_updated_by", "updated_by"),
        Index("idx_tbl_comp_navigations_tags_gin", "tags", postgresql_using="gin"),
        Index(
            "idx_tbl_comp_navigations_metadata_gin",
            "metadata",
            postgresql_using="gin",
        ),
        # If you later add a GIN index on file_links, uncomment the line below
        # Index("idx_tbl_comp_navigations_file_links_gin", "file_links", postgresql_using="gin"),
    )


__all__ = ["CompNavigation"]
