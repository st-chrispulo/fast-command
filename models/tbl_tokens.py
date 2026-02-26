from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Text, func

from auth.db import Base


class Token(Base):
    """Represents an issued access/refresh token pair for a user."""

    __tablename__ = "tbl_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)

    user_id = Column(
        Integer,
        ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=False)
    scope = Column(Text, nullable=False)

    issued_at = Column(DateTime(timezone=False), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=False), nullable=False)
    refresh_token_expires_at = Column(DateTime(timezone=False), nullable=False)

    revoked = Column(Boolean, nullable=False, server_default="false")
