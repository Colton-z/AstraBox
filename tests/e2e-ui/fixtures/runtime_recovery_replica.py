"""Independent deployed service replica for the explicit-recovery race E2E.

Only scheduling is gated: the follower has read the original Session before
the winner creates its real sandbox; the winner pauses before durable binding.
Both replicas use the deployment's actual repositories, provider and engine.
Like a serving replica, each process exits only after the background work its
request spawned has finished.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path



async def wait_file(path: Path) -> None:
    async with asyncio.timeout(120):
        while not path.exists():
            await asyncio.sleep(0.1)


async def run(args: argparse.Namespace) -> None:
    directory = Path(args.directory)
    (directory / f"{args.mode}.pid").write_text(str(os.getpid()))
    from astrabox.bootstrap import bootstrap
    from astrabox.common.utils.user_context import UserContext
    from astrabox.core.service.orchestrator.platform_service import AgentPlatformService
    from astrabox.deploy.onebox import _export_backend_wiring, ensure_database_wiring, needs_sandbox_server

    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    bootstrap()
    platform = AgentPlatformService()
    session = await platform._sessions_repo.get_session(args.session_id)
    if not session or session.get("sandbox_id"):
        raise RuntimeError("replica requires the test Session's cleared dead binding")
    agent = await platform._agent_repo.get_agent(str(session.get("agent_id") or ""))
    if not agent or not str(agent.get("name") or "").startswith("__e2e_runtime_recovery_"):
        raise RuntimeError("replica requires its specifically named E2E Agent")
    user = UserContext(user_id=str(session["user_id"]), org_id=session.get("org_id"))
    created: list[str] = []
    create = platform._runtime_manager.create_runtime

    async def observed_create(*positional, **keywords):
        runtime = await create(*positional, **keywords)
        created.append(runtime.sandbox_id)
        if args.mode == "winner":
            (directory / "created.json").write_text(json.dumps({"sandbox_id": runtime.sandbox_id}))
            await wait_file(directory / "publish")
        return runtime

    platform._runtime_manager.create_runtime = observed_create
    if args.mode == "follower":
        build = platform._session_kernel._build_lifecycle_worker

        def gated_worker():
            worker = build()
            recover = worker._recover_session_direct

            async def gated_recover(**keywords):
                (directory / "admitted").write_text("the real worker read the old Session")
                await wait_file(directory / "recover")
                return await recover(**keywords)

            worker._recover_session_direct = gated_recover
            return worker

        platform._session_kernel._build_lifecycle_worker = gated_worker

    kernel = platform._session_kernel
    spawned: list[asyncio.Task] = []
    spawn = kernel._spawn_background_task

    def tracked_spawn(coro, *, name=None):
        task = spawn(coro, name=name)
        spawned.append(task)
        return task

    kernel._spawn_background_task = tracked_spawn
    # The real kernel entry performs ownership/admission and command dispatch.
    # A standalone request does not start deployment-global background sweeps.
    await kernel.recover_session(user, args.session_id)
    # recover_session answers before its StartSessionStartup worker finishes.
    # That worker writes the READY Session row first and then appends
    # session.startup_completed, which moves the lifecycle snapshot that
    # GET /sessions/{id} renders. Leaving asyncio.run between those writes
    # cancels the worker, so the Session reads CREATING forever.
    async with asyncio.timeout(120):
        await asyncio.gather(*spawned)
    if args.mode == "winner":
        current = await platform._sessions_repo.get_session(args.session_id)
        if not current or current.get("state") != "READY":
            state = current.get("state") if current else None
            error = current.get("last_error") if current else None
            raise RuntimeError(f"recovery did not publish READY: state={state} last_error={error}")
    (directory / f"{args.mode}.json").write_text(json.dumps({"created": created}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--mode", choices=("winner", "follower"), required=True)
    asyncio.run(run(parser.parse_args()))
