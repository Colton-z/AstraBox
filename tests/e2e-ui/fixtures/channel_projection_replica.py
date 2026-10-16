"""One delayed projector over a live E2E Session's actual output."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


async def main(session_id: str, deployment_id: str, directory: Path) -> None:
    from astrabox.bootstrap import bootstrap
    from astrabox.core.service.orchestrator.platform_service import AgentPlatformService
    from astrabox.deploy.onebox import (
        _export_backend_wiring,
        ensure_database_wiring,
        needs_sandbox_server,
    )

    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    bootstrap()
    platform = AgentPlatformService()
    session = await platform._sessions_repo.get_session(session_id)
    assert session is not None
    agent = await platform._agent_repo.get_agent(str(session.get("agent_id") or ""))
    assert agent is not None and str(agent.get("name") or "").startswith("__e2e_empty_reply_")
    ingress = platform.channel_ingress_service
    repo = ingress._channel_repo
    subscriptions = await repo.session_output_subscriptions(session_id)
    subscription = next(row for row in subscriptions if row["deployment_id"] == deployment_id)
    assert subscription["channel_name"] == "generic_json" and not subscription["streaming"]

    async def wait_for(name: str) -> None:
        async with asyncio.timeout(120):
            while not (directory / name).exists():
                await asyncio.sleep(0.1)

    create = repo.create_outbox_entry

    async def delayed_create(**kwargs):
        rows = await ingress._facts.read_output(session_id, after_seq=subscription["after_seq"])
        assert rows and rows[-1]["part"]["type"] == "start"
        assert kwargs["response_seq"] == rows[-1]["seq"]
        (directory / "paused.json").write_text(json.dumps({
            "subscription": subscription,
            "response_id": kwargs["response_id"],
            "response_seq": kwargs["response_seq"],
            "output": rows,
        }))
        await wait_for("resume")
        entry = await create(**kwargs)
        (directory / "created.json").write_text(json.dumps(entry))
        return entry

    repo.create_outbox_entry = delayed_create
    (directory / "ready").write_text("subscription captured before the new input")
    await wait_for("read")
    # Only the insertion is delayed. Projection, SQL and cursor CAS are real;
    # this process starts no reader, sweeper or outbound delivery task.
    due = await ingress._project_subscription(subscription)
    assert not due
    (directory / "done").write_text("the delayed start projector returned")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], Path(sys.argv[3])))
