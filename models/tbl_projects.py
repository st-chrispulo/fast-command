from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from auth.db import Base


class Project(Base):
    __tablename__ = "tbl_projects"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    created_by = Column(Integer, ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True)
    updated_by = Column(Integer, ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True)

    thumbnail = Column(Text, nullable=True)
    images = Column(JSONB, nullable=True)
    metadata_json = Column("metadata", JSONB, nullable=True)

    template_id = Column(UUID(as_uuid=True), nullable=True)
    file_link = Column(Text, nullable=True)

    tags = Column(ARRAY(String), nullable=False, server_default="{}")

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_tbl_projects_template_id", "template_id"),
        Index("idx_tbl_projects_created_at", "created_at"),
        Index("idx_tbl_projects_images_gin", "images", postgresql_using="gin"),
        Index("idx_tbl_projects_created_by", "created_by"),
        Index("idx_tbl_projects_updated_by", "updated_by"),
        Index("idx_tbl_projects_tags_gin", "tags", postgresql_using="gin"),
        Index("idx_tbl_projects_metadata_gin", "metadata", postgresql_using="gin"),
    )


__all__ = ["Project"]
