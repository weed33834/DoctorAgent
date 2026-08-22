"""SQLAlchemy ORM models for the conversations subsystem (P2 pilot).

Mirrors the SQLite DDL in ``doctoragent.conversations`` one-to-one so both
backends stay byte-compatible during dual-run:

* ``database_url`` empty  → legacy raw-sqlite3 path (untouched)
* ``database_url`` set    → these models via :mod:`doctoragent.db.engine`

Column types are deliberately conservative (Text/Float) so the same models
serve SQLite and PostgreSQL; JSONB arrives with P3's pgvector/dialect work.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ConversationsBase(DeclarativeBase):
    """Shared declarative base for the conversations subsystem."""


class ConversationORM(ConversationsBase):
    """conv_conversations — one clinical chat session."""

    __tablename__ = "conv_conversations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[str | None] = mapped_column(Text)  # JSON-encoded dict
    tenant_id: Mapped[str] = mapped_column(
        Text, nullable=False, default="default", server_default="default"
    )


class ConvMessageORM(ConversationsBase):
    """conv_messages — one turn inside a conversation."""

    __tablename__ = "conv_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(String, index=True)
    role: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[str | None] = mapped_column(Text)
    feedback: Mapped[int] = mapped_column(Integer, default=0)
    feedback_comment: Mapped[str | None] = mapped_column(Text, default="")


class ConvShareORM(ConversationsBase):
    """conv_shares — public share tokens (cross-tenant by design)."""

    __tablename__ = "conv_shares"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[float | None] = mapped_column(Float)


def to_dict(obj: Any) -> dict[str, Any]:
    """Model instance → plain dict matching the legacy row shape."""
    return {
        c.name: getattr(obj, c.name) for c in obj.__table__.columns  # type: ignore[attr-defined]
    }
