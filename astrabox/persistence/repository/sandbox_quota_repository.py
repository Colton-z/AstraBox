"""Each Environment and sandbox backend's quota ledger: its inventory and its reservations, one row per box.

A backend's usage is what its last inventory charges plus the reservations for
boxes that inventory does not charge. Two operations can raise that usage and
both hold the backend's :class:`~astrabox.persistence.sql.models.sandboxes.SandboxCapacity`
row lock: admitting a reservation, and folding in a new inventory. So two
replicas that each see room for one more box cannot both take it. Marking a box
created and releasing a reservation never raise usage; each touches only its
own reservation row and takes no ledger lock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Table,
    Select,
    and_,
    cast,
    case,
    insert,
    delete,
    exists,
    func,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.config.settings import get_settings, validate_environment_name
from astrabox.persistence.sql import execute, fetch_all, fetch_one, fetch_value, to_iso, transaction
from astrabox.persistence.sql.statements import insert_if_absent_primary_key, upsert_primary_key
from astrabox.persistence.sql.models.sandboxes import (
    SandboxCapacity,
    SandboxInventoryBox,
    SandboxReservation,
)

T = TypeVar("T")

_CAPACITY = SandboxCapacity.__table__
_RESERVATIONS = SandboxReservation.__table__
_INVENTORY = SandboxInventoryBox.__table__


class InventoryNotTaken(Exception):
    """No inventory of the backend has been folded into its ledger yet.

    A ledger without one would count every running box as absent, so nothing
    is admitted against it.
    """


def _backend_owner(table: Table, backend: str, environment_name: str) -> ColumnElement[bool]:
    return and_(table.c.backend == backend, table.c.environment_name == environment_name)


def _held_by_inventory() -> Any:
    """Whether the backend's inventory charges a reservation's box itself."""
    box = _INVENTORY.alias("held_box")
    return exists().where(
        box.c.backend == _RESERVATIONS.c.backend,
        box.c.environment_name == _RESERVATIONS.c.environment_name,
        box.c.sandbox_id == _RESERVATIONS.c.sandbox_id,
        box.c.released.is_(False),
    )


def _usage_statement(backend: str, *, environment_name: str) -> Select[Any]:
    """The backend's usage: the boxes its inventory charges plus pending reservations."""
    charged = (
        select(
            func.count().label("boxes"),
            cast(func.coalesce(func.sum(_INVENTORY.c.memory_bytes), 0), BigInteger).label("memory"),
            cast(func.coalesce(func.sum(_INVENTORY.c.cpu_millicores), 0), BigInteger).label("cpu"),
        )
        .where(_backend_owner(_INVENTORY, backend, environment_name), _INVENTORY.c.released.is_(False))
        .subquery()
    )
    pending = (
        select(
            func.count().label("boxes"),
            cast(func.coalesce(func.sum(_RESERVATIONS.c.memory_bytes), 0), BigInteger).label(
                "memory"
            ),
            cast(func.coalesce(func.sum(_RESERVATIONS.c.cpu_millicores), 0), BigInteger).label(
                "cpu"
            ),
            func.count(case((_RESERVATIONS.c.state == "creating", 1))).label("creating"),
        )
        .where(_backend_owner(_RESERVATIONS, backend, environment_name), ~_held_by_inventory())
        .subquery()
    )
    return select(
        (charged.c.boxes + pending.c.boxes).label("boxes"),
        (charged.c.memory + pending.c.memory).label("memory_bytes"),
        (charged.c.cpu + pending.c.cpu).label("cpu_millicores"),
        pending.c.creating.label("creating"),
    ).select_from(charged.join(pending, true()))


class SandboxQuotaRepository:
    def __init__(self, *, environment_name: str | None = None) -> None:
        self.environment_name = validate_environment_name(
            get_settings().environment_name if environment_name is None else environment_name
        )

    async def admit(
        self,
        backend: str,
        reservation: dict[str, Any],
        refuse: Callable[[dict[str, int]], T | None] | None,
    ) -> T | None:
        """Store ``reservation`` unless ``refuse`` turns it away; ``refuse``'s answer.

        ``refuse`` is given the backend's usage (``boxes``, ``memory_bytes``,
        ``cpu_millicores``, ``creating``) while the ledger is locked, so the
        usage it judges is the usage the reservation is added to. ``None``
        stores without reading usage, for a backend with no ceiling.

        Raises :class:`InventoryNotTaken` before any inventory is folded in.
        """

        async def work(conn: AsyncConnection) -> T | None:
            ledger = select(_CAPACITY.c.inventory_at).where(
                _backend_owner(_CAPACITY, backend, self.environment_name)
            )
            if refuse is not None:
                # FOR NO KEY UPDATE orders admissions and inventory folds on the
                # ledger row, and lets reservation inserts take the key-share
                # lock their foreign key needs.
                ledger = ledger.with_for_update(key_share=True)
            taken = (await conn.execute(ledger)).first()
            if taken is None or taken.inventory_at is None:
                raise InventoryNotTaken(backend)
            if refuse is not None:
                # A statement after the lock reads every reservation committed
                # by the admission that held it before.
                usage = (await conn.execute(
                    _usage_statement(backend, environment_name=self.environment_name)
                )).one()
                refusal = refuse(dict(usage._mapping))
                if refusal is not None:
                    return refusal
            await conn.execute(insert(_RESERVATIONS).values({
                **reservation, "backend": backend, "environment_name": self.environment_name,
            }))
            return None

        return await transaction(work)

    async def mark_created(self, backend: str, key: str, sandbox_id: str) -> None:
        await execute(
            update(_RESERVATIONS)
            .where(
                _backend_owner(_RESERVATIONS, backend, self.environment_name),
                _RESERVATIONS.c.reservation_key == key,
            )
            .values(state="created", sandbox_id=sandbox_id, box_created_at=func.now())
        )

    async def release(self, backend: str, key: str) -> None:
        await execute(
            delete(_RESERVATIONS).where(
                _backend_owner(_RESERVATIONS, backend, self.environment_name), _RESERVATIONS.c.reservation_key == key
            )
        )

    async def clock(self) -> datetime:
        """The database's current time, the one clock every ledger time is read from."""
        now = await fetch_value(select(func.now()))
        assert isinstance(now, datetime)
        return now

    async def fold_inventory(
        self,
        backend: str,
        *,
        taken_at: datetime,
        boxes: Iterable[dict[str, Any]],
        create_grace: timedelta,
    ) -> int | None:
        """Replace the backend's inventory with ``boxes`` and clear settled reservations.

        ``boxes`` is everything the backend listed for this installation, taken
        no earlier than ``taken_at``. A reservation leaves the ledger when the
        inventory charges its box from then on, when its box was created (or
        resumed) before the inventory was taken and the inventory does not
        charge it, or when its create has gone unanswered for ``create_grace``
        with no box to show for it.

        Returns how many reservations were released for the last two reasons,
        or ``None`` when an inventory taken later is already folded in, in
        which case nothing changes.
        """
        listed = {str(box["sandbox_id"]): box for box in boxes}
        rows = [
            {**box, "backend": backend, "sandbox_id": key, "environment_name": self.environment_name}
            for key, box in listed.items()
        ]

        async def work(conn: AsyncConnection) -> int | None:
            await conn.execute(
                insert_if_absent_primary_key(conn, _CAPACITY, {
                    "environment_name": self.environment_name, "backend": backend,
                })
            )
            newer = (
                await conn.execute(
                    update(_CAPACITY)
                    .where(
                        _backend_owner(_CAPACITY, backend, self.environment_name),
                        or_(
                            _CAPACITY.c.inventory_at.is_(None),
                            _CAPACITY.c.inventory_at < taken_at,
                        ),
                    )
                    .values(inventory_at=taken_at)
                )
            ).rowcount
            if newer != 1:
                return None
            await conn.execute(
                delete(_INVENTORY).where(
                    _backend_owner(_INVENTORY, backend, self.environment_name),
                    _INVENTORY.c.sandbox_id.not_in(list(listed)),
                )
            )
            if rows:
                # Every inventory writer holds this ledger, so the comparison
                # and upsert cannot lose a concurrent inventory change.
                existing = {row["sandbox_id"]: row for row in (
                    await conn.execute(select(_INVENTORY).where(
                        _backend_owner(_INVENTORY, backend, self.environment_name)
                    ))
                ).mappings()}
                columns = ("cpu_millicores", "memory_bytes", "released", "state", "session_id")
                changed = [row for row in rows if row["sandbox_id"] not in existing or any(
                    existing[row["sandbox_id"]][key] != row.get(key) for key in columns
                )]
                if changed:
                    await conn.execute(upsert_primary_key(
                        conn, _INVENTORY, changed, update_columns=columns,
                    ))
            # Charged by the inventory from now on: moved, not released.
            await conn.execute(
                delete(_RESERVATIONS).where(
                    _backend_owner(_RESERVATIONS, backend, self.environment_name), _held_by_inventory()
                )
            )
            released = await conn.execute(
                delete(_RESERVATIONS).where(
                    _backend_owner(_RESERVATIONS, backend, self.environment_name),
                    or_(
                        and_(
                            _RESERVATIONS.c.state == "created",
                            _RESERVATIONS.c.box_created_at < taken_at,
                        ),
                        and_(
                            _RESERVATIONS.c.state == "creating",
                            _RESERVATIONS.c.reserved_at < taken_at - create_grace,
                        ),
                    ),
                )
            )
            return int(released.rowcount)

        return await transaction(work)

    async def inventory_box(self, backend: str, sandbox_id: str) -> dict[str, Any] | None:
        return await fetch_one(
            select(_INVENTORY).where(
                _backend_owner(_INVENTORY, backend, self.environment_name), _INVENTORY.c.sandbox_id == sandbox_id
            )
        )

    async def ledger(self, backend: str) -> dict[str, Any]:
        """The backend's usage and when its inventory was taken, read together."""
        usage = _usage_statement(backend, environment_name=self.environment_name).subquery()
        row = await fetch_one(
            select(
                usage,
                select(_CAPACITY.c.inventory_at)
                .where(_backend_owner(_CAPACITY, backend, self.environment_name))
                .scalar_subquery()
                .label("inventory_at"),
            )
        )
        assert row is not None
        return {**row, "inventory_at": to_iso(row["inventory_at"])}

    async def list_inventory(self, backend: str) -> list[dict[str, Any]]:
        return await fetch_all(
            select(_INVENTORY)
            .where(_backend_owner(_INVENTORY, backend, self.environment_name))
            .order_by(_INVENTORY.c.sandbox_id)
        )

    async def list_pending_reservations(self, backend: str) -> list[dict[str, Any]]:
        """Reservations whose box the inventory does not charge, oldest first."""
        rows = await fetch_all(
            select(_RESERVATIONS)
            .where(_backend_owner(_RESERVATIONS, backend, self.environment_name), ~_held_by_inventory())
            .order_by(_RESERVATIONS.c.reserved_at, _RESERVATIONS.c.reservation_key)
        )
        return [{**row, "reserved_at": to_iso(row["reserved_at"])} for row in rows]
