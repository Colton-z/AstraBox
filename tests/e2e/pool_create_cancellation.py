"""Hold a real lifecycle response before the SDK learns its allocated box id."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from uuid import uuid4

import httpx


class HeldCreateResponse(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.real = httpx.AsyncHTTPTransport()
        self.held = asyncio.Event()
        self.sandbox_id = ""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.real.handle_async_request(request)
        if request.method == "POST" and request.url.path.rstrip("/") == "/v1/sandboxes":
            await response.aread()
            assert response.status_code == 202, response.status_code
            self.sandbox_id = str(response.json()["id"])
            self.held.set()
            try:
                await asyncio.Event().wait()
            finally:
                await response.aclose()
        return response

    async def aclose(self) -> None:
        # Several SDK clients borrow the transport; the fixture closes it once.
        pass


async def run(image: str) -> None:
    from astrabox.deploy.onebox import (
        _export_backend_wiring,
        ensure_database_wiring,
        needs_sandbox_server,
    )

    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    from astrabox.bootstrap import bootstrap

    bootstrap()
    from astrabox.core.service.orchestrator.engine.provisioning import sandbox_create_resources
    from astrabox.providers.open_sandbox.agent_pool import OpenSandboxClientPoolRegistry
    from astrabox.providers.open_sandbox.executor import create_open_sandbox_box
    from astrabox.providers.open_sandbox.sandbox import OpenSandboxSandboxProvider
    from astrabox.seams.sandbox import (
        SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
        SandboxClientPoolMember,
        SandboxClientPoolSpec,
    )

    transport = HeldCreateResponse()
    registry = OpenSandboxClientPoolRegistry(transport=transport)
    provider = OpenSandboxSandboxProvider()
    spec = SandboxClientPoolSpec(
        pool_name=f"e2e-cancel-create-{uuid4().hex}",
        creation_image=image,
        max_idle=1,
        idle_timeout_seconds=300,
        preparation_timeout_seconds=30,
    )
    members: list[SandboxClientPoolMember] = []

    async def creator(member: SandboxClientPoolMember):
        members.append(member)
        limits, requests = provider.fit_resources(*sandbox_create_resources(shared=False))
        return await create_open_sandbox_box(
            session_id=member.session_id, assignment_id=member.assignment_id,
            image=image, env={}, resource_limits=limits, resource_requests=requests,
            cwd=None, transport=transport,
        )

    async def preparer(_handle):
        raise AssertionError("an unreturned create cannot have reached preparation")

    try:
        await registry.ensure(spec, creator=creator, preparer=preparer)
        await asyncio.wait_for(transport.held.wait(), 60)
        member, = members
        before = await provider.find_sandbox_by_assignment(member.assignment_id)
        assert before is not None and before.sandbox_id == transport.sandbox_id
        assert before.session_id == member.session_id
        print(json.dumps({"pool": spec.pool_name, "allocated_sandbox": before.sandbox_id,
                          "stage": "native create response held before SDK receives id"}), flush=True)
        started = time.monotonic()
        await asyncio.wait_for(registry.retire(spec.pool_name), 60)
        elapsed = time.monotonic() - started
        assert await provider.find_sandbox_by_assignment(member.assignment_id) is None
        probe = await provider.probe(transport.sandbox_id)
        assert probe.probe_status == SANDBOX_LIFECYCLE_PROBE_NOT_FOUND, probe
        print(json.dumps({"sandbox_id": transport.sandbox_id, "retirement_seconds": elapsed,
                          "physical_sandbox_absent": True}), flush=True)
    finally:
        # Preserve native resources on assertion failure; only stop this process's scheduler.
        await registry.shutdown()
        await transport.real.aclose()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
