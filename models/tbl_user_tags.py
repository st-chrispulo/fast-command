from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Table, Text, func
from sqlalchemy.orm import declarative_base

Base = declarative_base()

tbl_users = Table(
    "tbl_users",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    extend_existing=True,
)


class UserTag(Base):
    __tablename__ = "tbl_user_tags"

    user_id = Column(
        Integer,
        ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    name = Column(String(255), primary_key=True, nullable=False)

    color_hex = Column(String(7), nullable=True)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="true")

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_tbl_user_tags_user_active", "user_id", "is_active"),
        Index("idx_tbl_user_tags_user_lname", "user_id", func.lower(name)),
    )

    def __repr__(self) -> str:
        return f"<UserTag user_id={self.user_id} name='{self.name}' active={self.is_active}>"


__all__ = ["UserTag"]
