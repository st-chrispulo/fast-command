# models/tbl_user_tags.py
from sqlalchemy import (
    Column, String, Text, DateTime, Integer, Boolean, ForeignKey, Index, func, Table
)
from sqlalchemy.orm import declarative_base

# If you already have a central Base in your project, import that instead.
Base = declarative_base()

# --- Minimal registration so create_all() can resolve FKs to tbl_users ---
# NOTE: use ONLY extend_existing=True (do NOT combine with keep_existing).
tbl_users = Table(
    "tbl_users",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    extend_existing=True,
)

class UserTag(Base):
    __tablename__ = "tbl_user_tags"

    # Composite primary key: (user_id, name)
    user_id = Column(
        Integer,
        ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    name = Column(String(255), primary_key=True, nullable=False)

    color_hex = Column(String(7), nullable=True)  # e.g. "#10b981"
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="true")

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
        # list/filter tags by user + active fast
        Index("idx_tbl_user_tags_user_active", "user_id", "is_active"),
        # case-insensitive lookup by name within a user
        Index("idx_tbl_user_tags_user_lname", "user_id", func.lower(name)),
    )

    def __repr__(self) -> str:
        return (
            f"<UserTag user_id={self.user_id} "
            f"name='{self.name}' active={self.is_active}>"
        )

__all__ = ["UserTag"]
