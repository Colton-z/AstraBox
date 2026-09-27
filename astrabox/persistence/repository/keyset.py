"""Read a set one keyset page at a time: all of it, or a budget per tick.

A sweep that must visit a whole set reads it in pages ordered by a unique key
and asks each next page for the rows after the last key it saw. A single
capped read in an order the sweep does not advance (``updated_at``, which the
sweep itself need not move) returns the same first page on every tick, and
the rows past it are never visited.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

#: Rows per page for a sweep that reads its whole set in one pass.
SWEEP_PAGE_SIZE = 200


async def iter_keyset_pages(
    fetch_page: Callable[[str | None], Awaitable[list[dict[str, Any]]]],
    *,
    key: str,
    page_size: int = SWEEP_PAGE_SIZE,
) -> AsyncIterator[dict[str, Any]]:
    """Yield every row ``fetch_page`` can return, in ``key`` order.

    ``fetch_page(after)`` returns at most ``page_size`` rows whose ``key`` is
    greater than ``after`` (every row for ``None``), sorted by ``key``. A page
    shorter than ``page_size`` is the last. A page that does not move past the
    previous key raises instead of looping forever.
    """

    after: str | None = None
    while True:
        page = await fetch_page(after)
        for row in page:
            yield row
        if len(page) < page_size:
            return
        last = str(page[-1].get(key) or "")
        if not last or (after is not None and last <= after):
            raise RuntimeError(f"keyset page on {key!r} did not advance past {after!r}")
        after = last


class KeysetCursor:
    """Where a budgeted sweep resumes in a set it cannot finish in one tick.

    :meth:`page` reads at most ``limit`` rows after the last row the sweep
    visited, in ``key`` order, and starts from the first row again once the
    end is reached. Rows that stay in the set after a visit are met again on
    the next lap instead of filling every page, so each row is visited within
    ``ceil(rows / limit)`` ticks.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        self.after: str | None = None
        self._before: str | None = None
        self._rows: list[dict[str, Any]] = []
        self._limit = 0

    async def page(
        self,
        fetch_page: Callable[[str | None, int], Awaitable[list[dict[str, Any]]]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Read the next page and record every row on it as visited.

        ``fetch_page(after, limit)`` returns at most ``limit`` rows whose
        ``key`` is greater than ``after`` (every row for ``None``), sorted by
        ``key``. A sweep that stops partway through the page reports how far
        it got with :meth:`visited`.
        """

        self._before = self.after
        rows = await fetch_page(self.after, limit)
        if not rows and self.after is not None:
            self._before = None
            rows = await fetch_page(None, limit)
        self._rows, self._limit = list(rows), limit
        self.visited(len(rows))
        return rows

    def visited(self, count: int) -> None:
        """Resume after the first ``count`` rows of the last page.

        A page read to its end that came back short was the last one, so the
        next read starts the set again.
        """

        rows = self._rows
        if count >= len(rows) and len(rows) < self._limit:
            self.after = None
            return
        if count <= 0:
            self.after = self._before
            return
        last = str(rows[min(count, len(rows)) - 1].get(self.key) or "")
        if not last or (self._before is not None and last <= self._before):
            raise RuntimeError(f"keyset page on {self.key!r} did not advance past {self._before!r}")
        self.after = last


async def read_filtered_page(
    fetch_page: Callable[[Any, int], Awaitable[list[dict[str, Any]]]],
    *,
    after: Any,
    key_of: Callable[[dict[str, Any]], Any],
    keep: Callable[[dict[str, Any]], bool],
    limit: int,
    page_size: int = SWEEP_PAGE_SIZE,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of ``limit`` rows that ``keep`` accepts, and whether more follow.

    For a listing whose filter is applied after the read (a permission, a
    search): raw pages are read after ``after`` until ``limit`` rows are
    kept or the set ends, so a page is short only at the end of the set.
    ``fetch_page(after, size)`` returns at most ``size`` rows in key order
    after ``after``; ``key_of`` gives a row's key in the form ``after`` takes.
    """

    kept: list[dict[str, Any]] = []
    while True:
        page = await fetch_page(after, page_size)
        for row in page:
            if keep(row):
                kept.append(row)
                if len(kept) > limit:
                    return kept[:limit], True
        if len(page) < page_size:
            return kept, False
        after = key_of(page[-1])


class InvalidListCursor(ValueError):
    """A list cursor this module did not produce: a request error, not a server one."""


def encode_list_cursor(fields: dict[str, str]) -> str:
    """The opaque ``next_cursor`` a listing hands back: the last row's key fields."""

    raw = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_list_cursor(cursor: str | None, keys: Sequence[str]) -> tuple[str, ...] | None:
    """The key fields ``cursor`` carries, in ``keys`` order; ``None`` for no cursor.

    Raises :class:`InvalidListCursor` for a cursor this module did not produce.
    """

    value = str(cursor or "").strip()
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise InvalidListCursor("unrecognised list cursor") from exc
    if not isinstance(payload, dict) or not all(isinstance(payload.get(key), str) for key in keys):
        raise InvalidListCursor("unrecognised list cursor")
    return tuple(str(payload[key]) for key in keys)


__all__ = [
    "SWEEP_PAGE_SIZE",
    "InvalidListCursor",
    "KeysetCursor",
    "decode_list_cursor",
    "encode_list_cursor",
    "iter_keyset_pages",
    "read_filtered_page",
]
