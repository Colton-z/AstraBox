"""Each sandbox backend's quota ledger: its inventory and its reservations, one row per box.

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
    Select,
    Text,
    all_,
    and_,
    cast,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.sql import execute, fetch_all, fetch_one, fetch_value, to_iso, transaction
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


def _held_by_inventory() -> Any:
    """Whether the backend's inventory charges a reservation's box itself."""
    box = _INVENTORY.alias("held_box")
    return exists().where(
        box.c.backend == _RESERVATIONS.c.backend,
        box.c.sandbox_id == _RESERVATIONS.c.sandbox_id,
        box.c.released.is_(False),
    )


def _usage_statement(backend: str) -> Select[Any]:
    """The backend's usage: the boxes its inventory charges plus pending reservations."""
    charged = (
        select(
            func.count().label("boxes"),
            cast(func.coalesce(func.sum(_INVENTORY.c.memory_bytes), 0), BigInteger).label("memory"),
            cast(func.coalesce(func.sum(_INVENTORY.c.cpu_millicores), 0), BigInteger).label("cpu"),
        )
        .where(_INVENTORY.c.backend == backend, _INVENTORY.c.released.is_(False))
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
            func.count().filter(_RESERVATIONS.c.state == "creating").label("creating"),
        )
        .where(_RESERVATIONS.c.backend == backend, ~_held_by_inventory())
        .subquery()
    )
    return select(
        (charged.c.boxes + pending.c.boxes).label("boxes"),
        (charged.c.memory + pending.c.memory).label("memory_bytes"),
        (charged.c.cpu + pending.c.cpu).label("cpu_millicores"),
        pending.c.creating.label("creating"),
    ).select_from(charged.join(pending, true()))


class SandboxQuotaRepository:
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
            ledger = select(_CAPACITY.c.inventory_at).where(_CAPACITY.c.backend == backend)
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
                usage = (await conn.execute(_usage_statement(backend))).one()
                refusal = refuse(dict(usage._mapping))
                if refusal is not None:
                    return refusal
            await conn.execute(insert(_RESERVATIONS).values(backend=backend, **reservation))
            return None

        return await transaction(work)

    async def mark_created(self, backend: str, key: str, sandbox_id: str) -> None:
        await execute(
            update(_RESERVATIONS)
            .where(_RESERVATIONS.c.backend == backend, _RESERVATIONS.c.reservation_key == key)
            .values(state="created", sandbox_id=sandbox_id, box_created_at=func.now())
        )

    async def release(self, backend: str, key: str) -> None:
        await execute(
            delete(_RESERVATIONS).where(
                _RESERVATIONS.c.backend == backend, _RESERVATIONS.c.reservation_key == key
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
        rows = [{"backend": backend, **box, "sandbox_id": key} for key, box in listed.items()]

        async def work(conn: AsyncConnection) -> int | None:
            await conn.execute(
                insert(_CAPACITY)
                .values(backend=backend)
                .on_conflict_do_nothing(index_elements=[_CAPACITY.c.backend])
            )
            newer = (
                await conn.execute(
                    update(_CAPACITY)
                    .where(
                        _CAPACITY.c.backend == backend,
                        or_(
                            _CAPACITY.c.inventory_at.is_(None),
                            _CAPACITY.c.inventory_at < taken_at,
                        ),
                    )
                    .values(inventory_at=taken_at)
                    .returning(_CAPACITY.c.backend)
                )
            ).first()
            if newer is None:
                return None
            await conn.execute(
                delete(_INVENTORY).where(
                    _INVENTORY.c.backend == backend,
                    _INVENTORY.c.sandbox_id != all_(literal(list(listed), ARRAY(Text))),
                )
            )
            if rows:
                upsert = insert(_INVENTORY)
                changed = upsert.excluded
                await conn.execute(
                    upsert.on_conflict_do_update(
                        index_elements=[_INVENTORY.c.backend, _INVENTORY.c.sandbox_id],
                        set_={
                            "cpu_millicores": changed.cpu_millicores,
                            "memory_bytes": changed.memory_bytes,
                            "released": changed.released,
                            "state": changed.state,
                            "session_id": changed.session_id,
                        },
                        # A box listed unchanged keeps its row version.
                        where=or_(
                            _INVENTORY.c.cpu_millicores.is_distinct_from(changed.cpu_millicores),
                            _INVENTORY.c.memory_bytes.is_distinct_from(changed.memory_bytes),
                            _INVENTORY.c.released.is_distinct_from(changed.released),
                            _INVENTORY.c.state.is_distinct_from(changed.state),
                            _INVENTORY.c.session_id.is_distinct_from(changed.session_id),
                        ),
                    ),
                    rows,
                )
            # Charged by the inventory from now on: moved, not released.
            await conn.execute(
                delete(_RESERVATIONS).where(
                    _RESERVATIONS.c.backend == backend, _held_by_inventory()
                )
            )
            released = await conn.execute(
                delete(_RESERVATIONS).where(
                    _RESERVATIONS.c.backend == backend,
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
                _INVENTORY.c.backend == backend, _INVENTORY.c.sandbox_id == sandbox_id
            )
        )

    async def ledger(self, backend: str) -> dict[str, Any]:
        """The backend's usage and when its inventory was taken, read together."""
        usage = _usage_statement(backend).subquery()
        row = await fetch_one(
            select(
                usage,
                select(_CAPACITY.c.inventory_at)
                .where(_CAPACITY.c.backend == backend)
                .scalar_subquery()
                .label("inventory_at"),
            )
        )
        assert row is not None
        return {**row, "inventory_at": to_iso(row["inventory_at"])}

    async def list_inventory(self, backend: str) -> list[dict[str, Any]]:
        return await fetch_all(
            select(_INVENTORY)
            .where(_INVENTORY.c.backend == backend)
            .order_by(_INVENTORY.c.sandbox_id)
        )

    async def list_pending_reservations(self, backend: str) -> list[dict[str, Any]]:
        """Reservations whose box the inventory does not charge, oldest first."""
        rows = await fetch_all(
            select(_RESERVATIONS)
            .where(_RESERVATIONS.c.backend == backend, ~_held_by_inventory())
            .order_by(_RESERVATIONS.c.reserved_at, _RESERVATIONS.c.reservation_key)
        )
        return [{**row, "reserved_at": to_iso(row["reserved_at"])} for row in rows]
