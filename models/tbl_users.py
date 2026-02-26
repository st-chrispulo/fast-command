from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String, Text, func

from auth.db import Base


class User(Base):
    """Represents an application user record."""

    __tablename__ = "tbl_users"

    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    username = Column(String(50), nullable=False)
    email = Column(String(100), nullable=False)
    password = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=False), nullable=False, server_default=func.now())
