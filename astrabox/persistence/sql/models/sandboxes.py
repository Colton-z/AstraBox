"""Sandbox capacity reservations, inventory and startup allocations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base


class SandboxCapacity(Base):
    """One sandbox backend's quota ledger.

    Admitting a reservation and folding in an inventory both hold this row's
    lock, so two replicas that each see room for one more box cannot both
    take it. Reserving, settling and releasing one box touch only that box's
    reservation row.
    """

    __tablename__ = "sandbox_capacity"

    backend: Mapped[str] = mapped_column(Text, primary_key=True)
    #: When the inventory the ledger holds was taken; null until the first one.
    inventory_at: Mapped[datetime | None]


class SandboxReservation(Base):
    """Room taken on a backend for one box its inventory does not charge yet."""

    __tablename__ = "sandbox_reservations"
    __table_args__ = (
        CheckConstraint(
            "(state = 'creating' AND sandbox_id IS NULL AND box_created_at IS NULL) "
            "OR (state = 'created' AND sandbox_id IS NOT NULL AND box_created_at IS NOT NULL)",
            name="state",
        ),
    )

    backend: Mapped[str] = mapped_column(
        Text, ForeignKey("sandbox_capacity.backend", ondelete="CASCADE"), primary_key=True
    )
    reservation_key: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text)
    cpu_millicores: Mapped[int] = mapped_column(BigInteger)
    memory_bytes: Mapped[int] = mapped_column(BigInteger)
    #: Why the box is created: a conversation's own box, a shared box, a pool member, a resume.
    reason: Mapped[str] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(Text)
    assignment_id: Mapped[str] = mapped_column(Text)
    sandbox_id: Mapped[str | None] = mapped_column(Text)
    reserved_at: Mapped[datetime] = mapped_column(server_default=func.now())
    #: When the box's create or resume answered.
    box_created_at: Mapped[datetime | None]


class SandboxInventoryBox(Base):
    """One box the backend's last inventory listed for this installation, and its charge.

    Each inventory replaces the backend's rows, so the table holds only the
    boxes the backend still lists.
    """

    __tablename__ = "sandbox_inventory"

    backend: Mapped[str] = mapped_column(
        Text, ForeignKey("sandbox_capacity.backend", ondelete="CASCADE"), primary_key=True
    )
    sandbox_id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: What the box was created with; a released box holds none of it.
    cpu_millicores: Mapped[int] = mapped_column(BigInteger)
    memory_bytes: Mapped[int] = mapped_column(BigInteger)
    #: Paused and released by its backend: it holds nothing until its resume is reserved.
    released: Mapped[bool]
    state: Mapped[str] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(Text)
