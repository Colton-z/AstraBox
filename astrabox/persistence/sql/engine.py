"""SQLAlchemy engines owned by the selected relational persistence provider."""

from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from astrabox.persistence.provider import get_persistence


def get_engine() -> AsyncEngine:
    """The running loop's stable selected repository engine."""
    return get_persistence().get_engine()


async def dispose_engine() -> None:
    """Dispose the selected provider's engines for this event loop."""
    await get_persistence().dispose()


def pool_report(*, holders: int = 5) -> str:
    """Describe the selected provider's pools without opening a connection."""
    return get_persistence().pool_report(holders=holders)


def database_url() -> str:
    """The selected provider's native, normalized database URL."""
    return get_persistence().database_url


def redacted_url(url: str) -> str:
    """The database URL with credentials hidden for diagnostic messages."""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - never disclose an unparseable credential prefix
        return url.split("@", 1)[-1]
