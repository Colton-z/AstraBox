"""The workspace view check names an existing OpenSandbox service as the cause.

With an existing lifecycle server on Docker, a sandbox's `/workspace` is an
empty directory rather than a mergerfs view, and the Lifecycle API cannot say
which runtime that server uses. The check that catches it is the only place
the combination shows, so its error must name the service and where the
limitation is documented; on the bundled server the same failure has other
causes and must not be blamed on an external service.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrabox.core.service.orchestrator.runtime.storage import mergerfs
from astrabox.deploy import sandbox_server
from astrabox.seams.storage import WorkspaceRef

_EXTERNAL = "https://sandbox-control.example.com"


def _box(filesystem: str) -> SimpleNamespace:
    async def run(_command: str) -> SimpleNamespace:
        return SimpleNamespace(stdout=f"{filesystem}\n")

    return SimpleNamespace(commands=SimpleNamespace(run=run))


_REF = WorkspaceRef("agent", "agent-1", "session-1")


async def test_an_empty_workspace_on_an_existing_service_names_that_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_OPENAPI_BASE_URL", _EXTERNAL)

    with pytest.raises(RuntimeError) as raised:
        await mergerfs.workspace_router.prepare(_REF, box=_box("ext4"), box_path="/workspace")

    message = str(raised.value)
    assert "is not a mergerfs view" in message
    assert _EXTERNAL in message
    assert mergerfs.EXISTING_SERVICE_WORKSPACES_DOC in message
    assert mergerfs.EXISTING_SERVICE_WORKSPACES_DOC.endswith(
        "#connect-an-existing-opensandbox-service"
    )


async def test_the_bundled_server_is_not_blamed_as_an_existing_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The entry point hands AstraBox the bundled server's address when it
    # starts that server, so this is what the setting holds then.
    monkeypatch.setenv("ASTRABOX_SANDBOX_OPENAPI_BASE_URL", sandbox_server.lifecycle_base_url())

    with pytest.raises(RuntimeError) as raised:
        await mergerfs.workspace_router.prepare(_REF, box=_box("ext4"), box_path="/workspace")

    assert "existing OpenSandbox service" not in str(raised.value)


async def test_a_mergerfs_view_passes_on_an_existing_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_OPENAPI_BASE_URL", _EXTERNAL)

    await mergerfs.workspace_router.prepare(_REF, box=_box("fuse.mergerfs"), box_path="/workspace")
