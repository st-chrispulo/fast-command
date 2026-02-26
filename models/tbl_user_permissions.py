from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import relationship

from auth.db import Base
from models.tbl_users import User

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("models.user_permissions")
except Exception:
    import logging

    logger = logging.getLogger(__name__)


class UserPermission(Base):
    __tablename__ = "tbl_user_permissions"

    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)

    user_id = Column(
        Integer,
        ForeignKey("tbl_users.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    command_name = Column(String(255), nullable=False, index=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    user = relationship(User, primaryjoin=(user_id == User.id), viewonly=True)

    __table_args__ = (
        UniqueConstraint("user_id", "command_name", name="uq_tbl_user_permissions_user_command"),
        Index("idx_tbl_user_permissions_user_command", "user_id", "command_name"),
    )


__all__ = ["UserPermission"]
