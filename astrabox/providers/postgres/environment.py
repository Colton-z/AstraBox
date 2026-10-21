"""The PostgreSQL notice channel for the database's Environment."""

from __future__ import annotations

from astrabox.config.settings import validate_environment_name


def notice_channel(name: str) -> str:
    """A native identifier below PostgreSQL's 63-byte limit."""
    return "astrabox_" + validate_environment_name(name) + "_session"
