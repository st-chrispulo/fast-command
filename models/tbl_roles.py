from __future__ import annotations

from sqlalchemy import Column, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY

from auth.db import Base


class Role(Base):
    """Represents an application role and its allowed command names."""

    __tablename__ = "tbl_roles"

    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    name = Column(String(50), nullable=False)
    description = Column(Text, nullable=True)
    command_names = Column(ARRAY(Text), nullable=True)
