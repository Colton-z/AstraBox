from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator.agent.prepared_capacity import (
    release_abandoned_unit_bindings,
)
from astrabox.persistence.repository.platform_mcp_binding_repository import (
    PlatformMCPBindingRepository,
)


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    monkeypatch.setenv("ASTRABOX_SANDBOX_LEASE_SECONDS", "3600")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_only_unclaimed_prepared_bindings_past_the_idle_lease_are_released() -> None:
    # The supplier discards an unclaimed unit when its idle lease ends without
    # telling the platform; a claimed unit's binding belongs to its Session, a
    # young one may still be waiting, and other bindings are not units at all.
    repo = PlatformMCPBindingRepository()
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    unit = {"scope_kind": "session", "source": "prepared_slot", "slot_id": "slot"}
    await repo.upsert_binding({**unit, "deployment_id": "abandoned", "created_at": long_ago})
    await repo.upsert_binding({**unit, "deployment_id": "waiting"})
    await repo.upsert_binding(
        {**unit, "deployment_id": "claimed", "created_at": long_ago, "session_id": "s-1"}
    )
    await repo.upsert_binding(
        {"deployment_id": "session-own", "session_id": "s-2", "created_at": long_ago}
    )

    assert await release_abandoned_unit_bindings() == {"prepared_unit_bindings_released": 1}

    assert await repo.get_binding("abandoned") is None
    for kept in ("waiting", "claimed", "session-own"):
        assert await repo.get_binding(kept) is not None, kept
