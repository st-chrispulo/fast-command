from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Index, String, Text, func

from models.socket.base import Base


class SocketServer(Base):
    """Represents a socket server instance that hosts many rooms."""

    __tablename__ = "tbl_socket_servers"

    id = Column(BigInteger, primary_key=True, autoincrement=True, nullable=False)
    server_key = Column(Text, nullable=False, unique=True)

    name = Column(String(255), nullable=True)
    host = Column(Text, nullable=True)
    region = Column(String(100), nullable=True)

    is_active = Column(Boolean, nullable=False, server_default="true")

    max_rooms = Column(BigInteger, nullable=False, server_default="0")
    rooms_used = Column(BigInteger, nullable=False, server_default="0")

    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_tbl_socket_servers_is_active", "is_active"),
        Index("idx_tbl_socket_servers_capacity", "max_rooms", "rooms_used"),
        Index("idx_tbl_socket_servers_last_seen_at", "last_seen_at"),
    )


__all__ = ["SocketServer"]
