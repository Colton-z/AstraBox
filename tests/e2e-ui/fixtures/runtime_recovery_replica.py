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
import faulthandler
import json
import os
from pathlib import Path



async def wait_file(path: Path, *, timeout: float = 120) -> None:
    async with asyncio.timeout(timeout):
        while not path.exists():
            await asyncio.sleep(0.1)


def inherit_serving_environment() -> None:
    """Use the launcher's complete environment without starting another launcher."""
    serving: list[Path] = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            argv = (process / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if any(
            index + 1 < len(argv)
            and Path(os.fsdecode(value)).name == "astrabox"
            and argv[index + 1] == b"serve"
            for index, value in enumerate(argv[:2])
        ):
            serving.append(process)
    if len(serving) != 1:
        raise RuntimeError(f"expected one serving AstraBox process, found {len(serving)}")
    environment = {
        os.fsdecode(key): os.fsdecode(value)
        for entry in (serving[0] / "environ").read_bytes().split(b"\0") if entry
        for key, _, value in [entry.partition(b"=")]
    }
    os.environ.clear()
    os.environ.update(environment)


async def run(args: argparse.Namespace) -> None:
    directory = Path(args.directory)
    (directory / f"{args.mode}.pid").write_text(str(os.getpid()))
    configuration_log = (directory / f"{args.mode}.configuration.log").open("w")
    faulthandler.dump_traceback_later(45, file=configuration_log)
    print("inheriting serving configuration", file=configuration_log, flush=True)
    inherit_serving_environment()
    print("importing service composition", file=configuration_log, flush=True)
    from astrabox.bootstrap import bootstrap
    from astrabox.common.utils.user_context import UserContext
    from astrabox.core.service.orchestrator.service_registry import get_platform_service, get_assistant_service

    print("composing providers", file=configuration_log, flush=True)
    bootstrap()
    platform = get_platform_service()
    faulthandler.cancel_dump_traceback_later()
    print("configured; no Session action started", file=configuration_log, flush=True)
    configuration_log.close()
    (directory / f"{args.mode}.ready").write_text("configured; no Session action started")
    request_file = directory / f"{args.mode}.request.json"
    await wait_file(request_file, timeout=360)
    request = json.loads(request_file.read_text())
    args.session_id = str(request["session_id"])
    args.assistant_id = str(request.get("assistant_id") or "")
    session = await platform._sessions_repo.get_session(args.session_id)
    if not session:
        raise RuntimeError("replica requires its parent E2E Session")
    if args.mode in {"follower", "winner"} and session.get("sandbox_id"):
        raise RuntimeError("replica requires the test Session's cleared dead binding")
    agent = await platform._agent_repo.get_agent(str(session.get("agent_id") or ""))
    if not agent or not str(agent.get("name") or "").startswith("__e2e_runtime_recovery_"):
        raise RuntimeError("replica requires its specifically named E2E Agent")
    user = UserContext(user_id=str(session["user_id"]), org_id=session.get("org_id"))
    created: list[str] = []
    create = platform._runtime_manager.create_runtime

    def accepted(session_id: str) -> None:
        from astrabox.core.service.orchestrator.bootstrap_reconciler import STARTUP_ALLOCATION_GRACE_SECONDS
        from astrabox.common.utils.settings import load_astrabox_settings

        (directory / "accepted.json").write_text(json.dumps({
            "session_id": session_id,
            "grace_seconds": STARTUP_ALLOCATION_GRACE_SECONDS,
            "scan_interval_seconds": load_astrabox_settings().expiration_watcher_interval_seconds,
        }))

    async def observed_create(session_id: str, *positional, **keywords):
        if args.mode in {"before-allocation", "assistant-before-allocation"}:
            accepted(session_id)
            (directory / "before-create.json").write_text(json.dumps({"session_id": session_id}))
            await wait_file(directory / "allocate")
        runtime = await create(session_id, *positional, **keywords)
        created.append(runtime.sandbox_id)
        if args.mode in {"winner", "after-allocation"}:
            (directory / "created.json").write_text(json.dumps({"sandbox_id": runtime.sandbox_id}))
            await wait_file(directory / "publish")
        return runtime

    platform._runtime_manager.create_runtime = observed_create
    lifecycle = platform._session_lifecycle
    if args.mode == "follower":
        refuse = lifecycle._refuse_while_startup_waits_on_user

        async def gated_refuse(session):
            # The last step before the recovery claims the Session it read.
            (directory / "admitted").write_text("the real recovery read the old Session")
            await wait_file(directory / "recover")
            return await refuse(session)

        lifecycle._refuse_while_startup_waits_on_user = gated_refuse

    spawned: list[asyncio.Task] = []
    spawn = lifecycle._spawn_background_task

    def tracked_spawn(coro, *, name=None):
        task = spawn(coro, name=name)
        spawned.append(task)
        return task

    lifecycle._spawn_background_task = tracked_spawn
    # The real lifecycle entry performs ownership and admission before the
    # claim. A standalone request does not start deployment-global background
    # sweeps, so it skips the platform's bootstrap.
    if args.mode in {"before-allocation", "after-allocation"}:
        created_session = await lifecycle.create_session(
            user, session_kind="agent_chat", workspace_ref=session["workspace_ref"],
            permission_mode=session.get("permission_mode"), agent_id=session["agent_id"],
        )
        accepted(created_session["session_id"])
    elif args.mode == "assistant-before-allocation":
        if not args.assistant_id:
            raise RuntimeError("Assistant startup fixture requires its owned Assistant id")
        await get_assistant_service().wake_workspace(user, args.assistant_id)
    else:
        await lifecycle.recover_session(user, args.session_id)
    # recover_session answers once its recovery task runs; the task writes
    # READY. Leaving asyncio.run before it finishes cancels it, and the
    # Session reads CREATING until the creation timeout.
    async with asyncio.timeout(120):
        await asyncio.gather(*spawned)
    if args.mode == "winner":
        current = await platform._sessions_repo.get_session(args.session_id)
        if not current or current.get("state") != "READY" or not created:
            state = current.get("state") if current else None
            error = current.get("last_error") if current else None
            raise RuntimeError(f"recovery did not create and publish its runtime: state={state} last_error={error} created={created}")
    (directory / f"{args.mode}.json").write_text(json.dumps({"created": created}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--mode", choices=("winner", "follower", "before-allocation", "after-allocation", "assistant-before-allocation"), required=True)
    asyncio.run(run(parser.parse_args()))
