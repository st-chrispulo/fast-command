from __future__ import annotations

from sqlalchemy import BIGINT, Column, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, TIMESTAMP

from auth.db import Base


class UserGithub(Base):
    """Represents a 1:1 linkage between an app user and a GitHub identity."""

    __tablename__ = "tbl_user_github"

    id = Column(BIGINT, primary_key=True, autoincrement=True, nullable=False)

    user_id = Column(
        Integer,
        ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    github_user_id = Column(BIGINT, nullable=False, unique=True, index=True)
    login = Column(CITEXT, nullable=False, unique=True, index=True)
    name = Column(Text, nullable=True)
    email = Column(CITEXT, nullable=True)
    avatar_url = Column(Text, nullable=True)

    access_token_enc = Column(Text, nullable=True)
    refresh_token_enc = Column(Text, nullable=True)
    token_type = Column(Text, nullable=True)
    token_scope = Column(Text, nullable=True)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=True)

    profile_json = Column(JSONB, nullable=True)
    installed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    last_synced_at = Column(TIMESTAMP(timezone=True), nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_tbl_user_github_user_id"),
        UniqueConstraint("github_user_id", name="uq_tbl_user_github_github_user_id"),
        UniqueConstraint("login", name="uq_tbl_user_github_login"),
        Index("idx_tbl_user_github_user_id", "user_id"),
        Index("idx_tbl_user_github_login", "login"),
        Index("idx_tbl_user_github_github_user_id", "github_user_id"),
    )
