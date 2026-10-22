"""Declarative base and shared column helpers (SQLAlchemy 2.0 style).

Mirrors database/alembic/versions/*.py, which are authoritative. If you change a
model, add a NEW migration; never edit an applied one.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def fk_uuid(target: str, *, ondelete: str = "RESTRICT", nullable: bool = False, **kw) -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, ondelete=ondelete), nullable=nullable, **kw)


class CreatedMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class TimestampMixin(CreatedMixin):
    # DB trigger set_updated_at() maintains this; onupdate keeps ORM objects fresh.
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
