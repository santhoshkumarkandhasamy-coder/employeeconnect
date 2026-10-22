from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import ARRAY, Boolean, CheckConstraint, Integer, SmallInteger, Text, Time, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, uuid_pk


class Department(TimestampMixin, Base):
    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint("name", name="uq_departments_name"),
        UniqueConstraint("code", name="uq_departments_code"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(CITEXT, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class Shift(TimestampMixin, Base):
    __tablename__ = "shifts"
    __table_args__ = (
        UniqueConstraint("name", name="uq_shifts_name"),
        CheckConstraint("grace_minutes BETWEEN 0 AND 120", name="ck_shifts_grace_minutes"),
        CheckConstraint("start_time <> end_time", name="ck_shifts_start_ne_end"),
        CheckConstraint("working_days <@ ARRAY[1,2,3,4,5,6,7]::smallint[]", name="ck_shifts_working_days"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    end_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    grace_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    working_days: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), nullable=False, server_default=text("'{1,2,3,4,5}'"))  # ISO: 1=Mon
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
