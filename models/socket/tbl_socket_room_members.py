from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB

from models.socket.base import Base


class SocketRoomMember(Base):
    """Represents a membership record for a service joined to a room."""

    __tablename__ = "tbl_socket_room_members"

    id = Column(BigInteger, primary_key=True, autoincrement=True, nullable=False)

    room_id = Column(
        BigInteger,
        ForeignKey("tbl_socket_rooms.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    member_type = Column(String(50), nullable=False, server_default="service")
    member_key = Column(Text, nullable=False)

    joined_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    left_at = Column(DateTime(timezone=True), nullable=True)

    meta_json = Column(JSONB, nullable=True)

    __table_args__ = (
        Index("idx_tbl_socket_room_members_member_key", "member_key"),
        Index("idx_tbl_socket_room_members_left_at", "left_at"),
        Index("idx_tbl_socket_room_members_meta_json_gin", "meta_json", postgresql_using="gin"),
        Index(
            "uq_tbl_socket_room_members_active",
            "room_id",
            "member_type",
            "member_key",
            unique=True,
            postgresql_where=(left_at.is_(None)),
        ),
    )


__all__ = ["SocketRoomMember"]
