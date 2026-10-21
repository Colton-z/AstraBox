"""Sandbox capacity reservations, inventory and startup allocations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base
from astrabox.persistence.sql.schema import MYSQL_TABLE_OPTIONS, boolean_checks
from astrabox.persistence.sql.types import KeyText, payload_text


class SandboxCapacity(Base):
    """One Environment's quota ledger for a sandbox backend.

    Admitting a reservation and folding in an inventory both hold this row's
    lock, so two replicas that each see room for one more box cannot both
    take it. Reserving, settling and releasing one box touch only that box's
    reservation row.
    """

    __tablename__ = "sandbox_capacity"
    __table_args__ = (
        MYSQL_TABLE_OPTIONS,
    )

    backend: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    environment_name: Mapped[str] = mapped_column(
        KeyText(128), primary_key=True
    )
    #: When the inventory the ledger holds was taken; null until the first one.
    inventory_at: Mapped[datetime | None]


class SandboxReservation(Base):
    """Room taken on one target and backend for a box its inventory does not charge yet."""

    __tablename__ = "sandbox_reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["backend", "environment_name"],
            ["sandbox_capacity.backend", "sandbox_capacity.environment_name"],
            name="fk_sandbox_reservations_capacity", ondelete="CASCADE",
        ),
        CheckConstraint(
            "(state = 'creating' AND sandbox_id IS NULL AND box_created_at IS NULL) "
            "OR (state = 'created' AND sandbox_id IS NOT NULL AND box_created_at IS NOT NULL)",
            name="state",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    backend: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    environment_name: Mapped[str] = mapped_column(
        KeyText(128), primary_key=True
    )
    reservation_key: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    state: Mapped[str] = mapped_column(payload_text())
    cpu_millicores: Mapped[int] = mapped_column(BigInteger)
    memory_bytes: Mapped[int] = mapped_column(BigInteger)
    #: Why the box is created: a conversation's own box, a shared box, a pool member, a resume.
    reason: Mapped[str] = mapped_column(payload_text())
    session_id: Mapped[str | None] = mapped_column(payload_text())
    assignment_id: Mapped[str] = mapped_column(payload_text())
    sandbox_id: Mapped[str | None] = mapped_column(payload_text())
    reserved_at: Mapped[datetime] = mapped_column(server_default=func.now())
    #: When the box's create or resume answered.
    box_created_at: Mapped[datetime | None]


class SandboxInventoryBox(Base):
    """One box in the target and backend's last inventory, and its charge.

    Each inventory replaces the backend's rows, so the table holds only the
    boxes the backend still lists.
    """

    __tablename__ = "sandbox_inventory"
    __table_args__ = (
        ForeignKeyConstraint(
            ["backend", "environment_name"],
            ["sandbox_capacity.backend", "sandbox_capacity.environment_name"],
            name="fk_sandbox_inventory_capacity", ondelete="CASCADE",
        ),
        *boolean_checks("released"),
        MYSQL_TABLE_OPTIONS,
    )

    backend: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    environment_name: Mapped[str] = mapped_column(
        KeyText(128), primary_key=True
    )
    sandbox_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    #: What the box was created with; a released box holds none of it.
    cpu_millicores: Mapped[int] = mapped_column(BigInteger)
    memory_bytes: Mapped[int] = mapped_column(BigInteger)
    #: Paused and released by its backend: it holds nothing until its resume is reserved.
    released: Mapped[bool]
    state: Mapped[str] = mapped_column(payload_text())
    session_id: Mapped[str | None] = mapped_column(payload_text())
