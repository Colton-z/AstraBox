"""Bring the database schema to the version this build ships.

The schema is versioned with Alembic; revisions live in
``astrabox/persistence/sql/alembic/versions``. :func:`upgrade_to_head` runs at
server start. Replicas that start together serialize on a PostgreSQL advisory
lock held for the whole upgrade, so each revision runs once.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

from astrabox.persistence.sql.engine import database_url

_SCRIPT_LOCATION = Path(__file__).resolve().parent / "alembic"


def alembic_config(url: str | None = None) -> Config:
    """An Alembic configuration for this package's revisions and ``url``."""
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION))
    # ConfigParser interpolation treats "%" specially; percent-encoded
    # passwords must reach Alembic unchanged.
    config.set_main_option("sqlalchemy.url", database_url(url).replace("%", "%%"))
    return config


async def upgrade_to_head(url: str | None = None) -> None:
    """Apply every pending revision; returns once the schema is current."""
    config = alembic_config(url)
    # Alembic's command API is synchronous and drives its own event loop for
    # the async engine in env.py, so it runs on a worker thread.
    await asyncio.to_thread(command.upgrade, config, "head")
