"""Configuration contract for the PostgreSQL production default."""

from __future__ import annotations

import pytest

from astrabox.config.settings import AstraBoxSettings, get_settings
from astrabox.persistence.repository import backend
from astrabox.persistence.repository.backend import active_backend_name
from astrabox.persistence.repository.sqlite.engine import resolve_database_url


def test_postgresql_default_requires_deployment_credentials(monkeypatch) -> None:
    monkeypatch.delenv("ASTRABOX_DB_BACKEND", raising=False)
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    settings = AstraBoxSettings(_env_file=None)
    assert settings.db_backend == "postgresql"
    with pytest.raises(ValueError, match="ASTRABOX_DB_URL"):
        _ = settings.resolved_db_url


def test_database_url_selects_the_matching_builtin(monkeypatch) -> None:
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.setenv(
        "ASTRABOX_DB_URL",
        "postgresql://astrabox:astrabox@127.0.0.1:55432/astrabox",
    )
    get_settings.cache_clear()
    try:
        assert active_backend_name() == "postgresql"
    finally:
        get_settings.cache_clear()


def test_a_configured_plugin_backend_is_not_replaced_by_its_urls_builtin(monkeypatch) -> None:
    # A plugin over MongoDB is configured with a mongodb:// URL; the URL must
    # not silently select the builtin Mongo backend instead of the plugin.
    resolved: list[str] = []
    monkeypatch.setattr(backend, "_resolve_backend", lambda name: resolved.append(name))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "enterprise_mongo")
    monkeypatch.setenv("ASTRABOX_DB_URL", "mongodb://db.internal:27017/astrabox")
    get_settings.cache_clear()
    try:
        assert active_backend_name() == "enterprise_mongo"
    finally:
        get_settings.cache_clear()
    assert resolved == ["enterprise_mongo"]


def test_sync_postgresql_urls_are_normalized_to_asyncpg() -> None:
    assert resolve_database_url("postgres://u:p@db:5432/name") == (
        "postgresql+asyncpg://u:p@db:5432/name"
    )
    assert resolve_database_url("postgresql://u:p@db:5432/name") == (
        "postgresql+asyncpg://u:p@db:5432/name"
    )
