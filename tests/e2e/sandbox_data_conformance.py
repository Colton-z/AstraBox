"""Run the data-plane contract against a box of the built-in OpenSandbox provider.

Executed inside the server container, where the deployment's sandbox wiring is.
The box boots the engine profile's own image with the entrypoint its engine
declares, so every bundled image's execd and workload account are what the
contract is checked against.
"""

from __future__ import annotations

import asyncio
import json
import sys
from uuid import uuid4


def _image_entrypoint(engine_kind: str) -> tuple[str, ...]:
    """The entrypoint the platform boots this engine's image with."""
    from astrabox.core.service.orchestrator.engine.codex import CODEX_IMAGE_ENTRYPOINT
    from astrabox.core.service.orchestrator.engine.deepseek_harness import DSH_IMAGE_ENTRYPOINT
    from astrabox.core.service.orchestrator.engine.pi import PI_IMAGE_ENTRYPOINT
    from astrabox.providers.sandbox_image import AIO_IMAGE_ENTRYPOINT

    return {
        "claude_code": AIO_IMAGE_ENTRYPOINT,
        "codex": CODEX_IMAGE_ENTRYPOINT,
        "deepseek_harness": DSH_IMAGE_ENTRYPOINT,
        "pi": PI_IMAGE_ENTRYPOINT,
    }[engine_kind]


async def run(image: str, engine_kind: str) -> None:
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
    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        SANDBOX_IMAGE_WORKLOAD_USER,
    )
    from astrabox.providers.open_sandbox.executor import create_open_sandbox_box
    from astrabox.providers.open_sandbox.sandbox import OpenSandboxSandboxProvider
    from astrabox.testing.sandbox_data_conformance import (
        DataContractBox,
        check_data_contract,
    )

    provider = OpenSandboxSandboxProvider()
    limits, requests = provider.fit_resources(*sandbox_create_resources(shared=False))
    handle = await create_open_sandbox_box(
        execution_target="astrabox",
        session_id=f"e2e-data-contract-{uuid4().hex}",
        assignment_id=uuid4().hex,
        image=image,
        entrypoint=_image_entrypoint(engine_kind),
        env={},
        resource_limits=limits,
        resource_requests=requests,
        cwd=None,
        require_execd_command_stream=True,
    )
    try:
        passed = await check_data_contract(
            DataContractBox(
                files=handle.files,
                commands=handle.commands,
                scratch=f"/tmp/astrabox-data-contract-{uuid4().hex}",
                account=SANDBOX_IMAGE_WORKLOAD_USER,
                processes=handle.processes if provider.supports_process_sessions else None,
            )
        )
        print(json.dumps({"sandbox_id": handle.sandbox_id, "passed": passed}), flush=True)
    finally:
        destruction = await provider.confirm_destroyed(handle.sandbox_id)
        await handle.close()
        print(
            json.dumps({"sandbox_id": handle.sandbox_id, "destroyed": destruction.confirmed}),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1], sys.argv[2]))
