"""Gate an actual provider observation while another replica renews the box.

Both replicas use the deployed service, repositories and sandbox provider. The
gate delays a real response; it never fabricates a lease or replaces storage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from datetime import datetime, timezone
from typing import Any


#: Where each owner of a box keeps its lease: the label the evidence uses,
#: the table and its key, the column naming the box, and the lease column.
_LEASES = {
    "sessions": ("sessions", "session_id", "sandbox_id", "expires_at"),
    "agents": ("agent_runtimes", "agent_id", "sandbox_id", "expires_at"),
    "assistant_workspace": (
        "assistant_workspaces", "assistant_id", "current_sandbox_id", "current_sandbox_expires_at",
    ),
}


async def run(session_id: str, owner_kind: str) -> None:
    from sqlalchemy import text

    from astrabox.bootstrap import bootstrap
    from astrabox.core.service.orchestrator.platform_service import AgentPlatformService
    from astrabox.deploy.onebox import _export_backend_wiring, ensure_database_wiring, needs_sandbox_server
    from astrabox.persistence.repository.assistant_catalog_repository import AssistantCatalogRepository
    from astrabox.persistence.sql.ops import execute

    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    bootstrap()
    delayed = AgentPlatformService()
    current = AgentPlatformService()
    session = await current._sessions_repo.get_session(session_id)
    assert session and session.get("sandbox_id"), "a READY test Session is required"
    sandbox_id = str(session["sandbox_id"])

    async def owners() -> list[dict[str, Any]]:
        groups = [
            ("sessions", await current._sessions_repo.list_sessions_by_sandbox_id(sandbox_id)),
            ("agents", await current._agent_repo.list_agents_by_sandbox_id(sandbox_id)),
            ("assistant_workspace",
             await current._assistant_workspace_service.list_workspaces_by_sandbox_id(sandbox_id)),
        ]
        return [{"collection": collection, "table": _LEASES[collection][0], "key": _LEASES[collection][1],
                 "binding": _LEASES[collection][2], "expiry": _LEASES[collection][3], "row": row}
                for collection, rows in groups for row in rows]

    initial = await owners()
    if owner_kind == "agent":
        agent = next(item["row"] for item in initial if item["collection"] == "agents")
        assert str(agent["name"]).startswith("__e2e_lease_realign_")
        assert sum(item["collection"] == "sessions" for item in initial) == 2
    else:
        workspace = next(item["row"] for item in initial if item["collection"] == "assistant_workspace")
        assistant = await AssistantCatalogRepository().get_assistant(str(workspace["assistant_id"]))
        assert assistant and str(assistant["display_name"]).startswith("__e2e_lease_realign_")
    probe = await current._runtime_manager.get_sandbox_lifecycle_probe(sandbox_id)
    assert probe.probe_status == "OK" and not current._runtime_manager._is_terminal_sandbox_lifecycle_probe(probe)

    # A lease the owners lost is repaired from real provider truth, and the
    # repair does not count as user activity. Each owner's lease column is
    # cleared where the owner still names this box, one row at a time.
    ordinary = []
    for _round in range(2):
        for item in await owners():
            suspect = (
                ", sandbox_liveness_suspect_at = now()" if item["collection"] == "sessions" else ""
            )
            # The names come from _LEASES above, never from the database.
            changed = await execute(text(
                f"UPDATE {item['table']} SET {item['expiry']} = NULL{suspect} "
                f"WHERE {item['key']} = :owner_id AND {item['binding']} = :sandbox_id"
            ).bindparams(owner_id=str(item["row"][item["key"]]), sandbox_id=sandbox_id))
            assert changed == 1, item["collection"]
        before = await owners()
        assert all(item["row"].get(item["expiry"]) is None for item in before)
        result = await current._sandbox_lifecycle_service.realign_live_sandbox_owners(sandbox_id)
        actual = await current._runtime_manager.get_sandbox_expires_at(sandbox_id)
        assert actual is not None
        after = await owners()
        for item in after:
            assert item["row"][item["expiry"]] == actual.isoformat(), item["collection"]
            if item["collection"] == "sessions":
                old = next(row["row"] for row in before if row["collection"] == "sessions"
                           and row["row"]["session_id"] == item["row"]["session_id"])
                assert item["row"]["updated_at"] == old["updated_at"]
                assert not item["row"].get("sandbox_liveness_suspect_at")
                for field in ("runtime_unavailable", "state", "last_error", "sandbox_id"):
                    assert item["row"].get(field) == old.get(field), field
        ordinary.append({"round": _round, "expires_at": actual.isoformat(),
                         "sessions": list(result.realigned_sessions),
                         "agents": list(result.realigned_agents),
                         "workspaces": list(result.realigned_assistant_workspaces)})

    observed = asyncio.Event()
    release = asyncio.Event()
    get_expiry = delayed._runtime_manager.get_sandbox_expires_at
    observation = {}

    async def held_expiry(target: str):
        assert target == sandbox_id
        value = await get_expiry(target)
        assert value is not None
        observation["expires_at"] = value
        observed.set()
        await release.wait()
        return value

    delayed._runtime_manager.get_sandbox_expires_at = held_expiry
    stale = asyncio.create_task(delayed._sandbox_lifecycle_service.realign_live_sandbox_owners(sandbox_id))
    stale.add_done_callback(lambda _task: observed.set())
    try:
        async with asyncio.timeout(30):
            await observed.wait()
            if stale.done():
                await stale
                raise AssertionError("realignment returned before the provider observation was released")
            old_expiry = observation["expires_at"]
            ttl = math.ceil((old_expiry - datetime.now(timezone.utc)).total_seconds()) + 120
            renewed = await current._runtime_manager.renew_sandbox_by_id(sandbox_id, ttl)
            assert renewed is not None and (renewed - old_expiry).total_seconds() >= 60
            await current._sandbox_lifecycle_service.realign_live_sandbox_owners(sandbox_id)
            published = await owners()
            assert all(item["row"][item["expiry"]] == renewed.isoformat() for item in published)
            release.set()
            await stale
    finally:
        release.set()
        if not stale.done():
            stale.cancel()
        await asyncio.gather(stale, return_exceptions=True)
    retained = await owners()
    assert all(item["row"][item["expiry"]] == renewed.isoformat() for item in retained), (
        "the delayed provider observation overwrote the completed renewal"
    )
    assert {(item["collection"], item["row"][item["key"]]) for item in retained} == {
        (item["collection"], item["row"][item["key"]]) for item in initial
    }
    print("ASTRABOX_LEASE_REALIGNMENT=" + json.dumps({
        "sandbox_id": sandbox_id, "owner_kind": owner_kind, "ordinary": ordinary,
        "observed_expires_at": old_expiry.isoformat(), "renewed_expires_at": renewed.isoformat(),
        "retained": [{"collection": item["collection"], "id": item["row"][item["key"]],
                      "sandbox_id": item["row"][item["binding"]],
                      "expires_at": item["row"][item["expiry"]]} for item in retained],
    }))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--owner-kind", choices=("agent", "assistant"), required=True)
    args = parser.parse_args()
    asyncio.run(run(args.session_id, args.owner_kind))
