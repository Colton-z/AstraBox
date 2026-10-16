"""Every table AstraBox stores, registered on :data:`astrabox.persistence.sql.base.Base.metadata`.

Each domain declares its tables in its own module here; importing this package
imports them all, which is what the schema migrations and the test database
need.
"""

from __future__ import annotations

from astrabox.persistence.sql.models import (  # noqa: F401 - registration
    platform,
    environments,
    agents,
    assistants,
    deployments,
    channels,
    artifacts,
    mcp,
    vaults,
    sandboxes,
    sessions,
)
