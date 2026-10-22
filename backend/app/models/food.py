from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, Date, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, fk_uuid, uuid_pk


class FoodEnrollment(TimestampMixin, Base):
    """Simple Yes/No per user per date. No row = not answered (counted as No)."""
    __tablename__ = "food_enrollments"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_food_enrollments_user_date"),
        Index("ix_food_enrollments_date", "date"),
        Index("ix_food_enrollments_date_need", "date", postgresql_where=text("need_food")),
        Index("ix_food_enrollments_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    need_food: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
