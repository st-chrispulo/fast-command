from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB

from models.socket.base import Base


class SocketRoom(Base):
    """Represents a socket room scoped under a socket server."""

    __tablename__ = "tbl_socket_rooms"

    id = Column(BigInteger, primary_key=True, autoincrement=True, nullable=False)

    server_id = Column(
        BigInteger,
        ForeignKey("tbl_socket_servers.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    room_key = Column(Text, nullable=False)
    room_type = Column(String(50), nullable=False, server_default="custom")

    join_token_hash = Column(Text, nullable=False, unique=True)

    scope_json = Column(JSONB, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="true")

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("uq_tbl_socket_rooms_server_room_key", "server_id", "room_key", unique=True),
        Index("idx_tbl_socket_rooms_room_type", "room_type"),
        Index("idx_tbl_socket_rooms_is_active", "is_active"),
        Index("idx_tbl_socket_rooms_scope_json_gin", "scope_json", postgresql_using="gin"),
    )


__all__ = ["SocketRoom"]
