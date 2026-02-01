# models/components/tbl_comp_connections.py
from sqlalchemy import (
    Column, String, DateTime, Integer, ForeignKey, Index, func, Table
)
from sqlalchemy.dialects.postgresql import UUID, JSONB

from auth.db import Base

# --- lightweight table stubs so FKs can resolve even if ORM models aren't imported yet ---

tbl_users = Table(
    "tbl_users",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    extend_existing=True,
)

tbl_funnels = Table(
    "tbl_funnels",
    Base.metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    extend_existing=True,
)

# If your funnels table is schema-qualified (common in Postgres), use this instead:
# tbl_funnels = Table(
#     "tbl_funnels",
#     Base.metadata,
#     Column("id", UUID(as_uuid=True), primary_key=True),
#     schema="public",
#     extend_existing=True,
# )


class CompConnection(Base):
    __tablename__ = "tbl_comp_connections"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        nullable=False,
    )

    funnel_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tbl_funnels.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    # If funnels is in schema public, use:
    # ForeignKey("public.tbl_funnels.id", onupdate="CASCADE", ondelete="CASCADE")

    # New (frontend/static identifiers)
    from_node_id = Column(String, nullable=True)  # TEXT in DB
    to_node_id = Column(String, nullable=True)    # TEXT in DB

    from_component_id = Column(UUID(as_uuid=True), nullable=False)
    from_port_id = Column(String(255), nullable=False)

    to_component_id = Column(UUID(as_uuid=True), nullable=False)
    to_port_id = Column(String(255), nullable=False)

    metadata_json = Column("metadata", JSONB, nullable=True)

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

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_tbl_comp_connections_funnel_id", "funnel_id"),
        Index("idx_tbl_comp_connections_from_component_id", "from_component_id"),
        Index("idx_tbl_comp_connections_to_component_id", "to_component_id"),

        # New indexes
        Index("idx_tbl_comp_connections_from_node_id", "from_node_id"),
        Index("idx_tbl_comp_connections_to_node_id", "to_node_id"),

        Index("idx_tbl_comp_connections_created_at", "created_at"),
        Index("idx_tbl_comp_connections_created_by", "created_by"),
        Index("idx_tbl_comp_connections_updated_by", "updated_by"),
        Index("idx_tbl_comp_connections_metadata_gin", "metadata", postgresql_using="gin"),
        {"schema": "public"},
    )


__all__ = ["CompConnection"]
