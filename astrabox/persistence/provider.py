"""Select one relational persistence provider without importing a default backend."""

from __future__ import annotations

import inspect
from functools import lru_cache
from typing import cast

from astrabox.config.settings import get_settings
from astrabox.providers import PERSISTENCE_GROUP, load_provider
from astrabox.seams.relational_persistence import RelationalPersistenceProvider


@lru_cache(maxsize=1)
def get_persistence() -> RelationalPersistenceProvider:
    """Load and validate the explicitly configured relational provider.

    Construction validates configuration without contacting the database. Schema
    initialization is a separate awaited startup step. No backend is substituted
    for an unknown or incomplete provider.
    """
    settings = get_settings()
    target = load_provider(PERSISTENCE_GROUP, settings.persistence_provider)
    if not isinstance(target, type):
        raise RuntimeError("a persistence entry point must name a provider class")
    provider = target(
        database_url=settings.resolved_db_url, environment_name=settings.environment_name,
    )
    if getattr(provider, "environment_name", None) != settings.environment_name:
        raise RuntimeError("persistence provider must bind the configured Environment explicitly")
    for name in ("get_engine", "pool_report", "translate_error"):
        if not callable(getattr(provider, name, None)):
            raise RuntimeError(f"persistence provider is missing required operation {name!r}")
    for name in ("initialize_schema", "dispose"):
        if not inspect.iscoroutinefunction(getattr(provider, name, None)):
            raise RuntimeError(f"persistence provider requires async operation {name!r}")
    if not isinstance(getattr(provider, "database_url", None), str) or not provider.database_url.strip():
        raise RuntimeError("persistence provider requires a nonempty native database_url")
    statements = getattr(provider, "statements", None)
    for name in ("upsert_primary_key", "insert_if_absent_primary_key"):
        if not callable(getattr(statements, name, None)):
            raise RuntimeError(f"persistence provider requires statements.{name}")
    if not inspect.iscoroutinefunction(getattr(statements, "insert_if_absent", None)):
        raise RuntimeError("persistence provider requires async statements.insert_if_absent")
    coordination = getattr(provider, "coordination", None)
    if not callable(getattr(coordination, "runtime_lock", None)):
        raise RuntimeError("persistence provider requires coordination.runtime_lock")
    if not inspect.iscoroutinefunction(getattr(coordination, "listen", None)):
        raise RuntimeError("persistence provider requires async coordination.listen")
    if not inspect.iscoroutinefunction(getattr(coordination, "notify", None)):
        raise RuntimeError("persistence provider requires async transactional coordination.notify")
    return cast(RelationalPersistenceProvider, provider)
