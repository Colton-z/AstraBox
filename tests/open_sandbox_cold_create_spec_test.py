"""The platform declares a box and OpenSandbox realizes that declaration.

The provider is a supplier boundary: it translates one complete neutral
``SandboxCreateSpec`` and never decides which Session gets a box. Durable
assignment identity makes replay converge on the same box, while an unnamed
create is refused instead of inventing ownership.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
import astrabox.providers.open_sandbox.executor as executor_module
import astrabox.providers.open_sandbox.sandbox as sandbox_module
from astrabox.providers.open_sandbox.sandbox import (
    OpenSandboxHandle,
    OpenSandboxSandboxProvider,
)
from astrabox.seams.sandbox import SandboxCreateSpec

PLANNED = (("/workspace", "/astrabox/workspaces/2b8f1d/workspace"),)
RESOURCE_LIMITS = {"cpu": "4", "memory": "4Gi"}
RESOURCE_REQUESTS = {"cpu": "200m", "memory": "768Mi"}


def _spec() -> SandboxCreateSpec:
    return SandboxCreateSpec(
        session_id="s-1",
        assignment_id="assignment-s-1",
        resource_limits=dict(RESOURCE_LIMITS),
        resource_requests=dict(RESOURCE_REQUESTS),
        image="astrabox/claude:test",
        entrypoint=("/opt/astrabox/boot.sh",),
        cwd="/workspace",
        env={"IS_SANDBOX": "1"},
        workspace_mounts=PLANNED,
        requires_command_channel=True,
    )


@pytest.mark.asyncio
async def test_the_provider_realizes_exactly_what_the_platform_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []

    async def create_box(**kwargs: object) -> SimpleNamespace:
        seen.append(dict(kwargs))
        return SimpleNamespace(sandbox_id="sbx-cold")

    monkeypatch.setattr(executor_module, "create_open_sandbox_box", create_box)
    monkeypatch.setattr(sandbox_module, "workspace_claim_name", lambda: "workspace-claim")

    handle = await OpenSandboxSandboxProvider().create_sandbox(_spec())

    assert handle.sandbox_id == "sbx-cold"
    assert len(seen) == 1, "one platform declaration produces exactly one create"
    created = seen[0]
    assert created["session_id"] == "s-1"
    assert created["assignment_id"] == "assignment-s-1"
    assert created["image"] == "astrabox/claude:test"
    assert created["entrypoint"] == ("/opt/astrabox/boot.sh",)
    assert created["cwd"] == "/workspace"
    assert created["env"] == {"IS_SANDBOX": "1"}
    assert created["resource_limits"] == RESOURCE_LIMITS
    assert created["resource_requests"] == RESOURCE_REQUESTS
    assert created["require_execd_command_stream"] is True
    assert created["volumes"] == [
        {
            "name": "astrabox-workspace-0",
            "pvc": {
                "claimName": "workspace-claim",
                "createIfNotExists": True,
                "deleteOnSandboxTermination": False,
            },
            "mountPath": "/workspace",
            "subPath": "astrabox/workspaces/2b8f1d/workspace",
            "readOnly": False,
        }
    ]


@pytest.mark.asyncio
async def test_a_create_without_durable_assignment_identity_refuses() -> None:
    with mock.patch.object(executor_module, "Sandbox") as sandbox_cls:
        sandbox_cls.create = AsyncMock()
        with pytest.raises(APIError) as caught:
            await executor_module.create_open_sandbox_box(
                session_id="s-1",
                assignment_id="",
                image="astrabox/claude:test",
                env={},
                resource_limits=dict(RESOURCE_LIMITS),
                resource_requests=dict(RESOURCE_REQUESTS),
                cwd=None,
            )

    assert caught.value.code == "SANDBOX_ASSIGNMENT_INVALID"
    sandbox_cls.create.assert_not_awaited()


@pytest.mark.parametrize("field_name", ("resource_limits", "resource_requests"))
def test_a_create_without_an_explicit_resource_envelope_refuses(
    field_name: str,
) -> None:
    fields = {
        "session_id": "s-1",
        "assignment_id": "assignment-s-1",
        "resource_limits": dict(RESOURCE_LIMITS),
        "resource_requests": dict(RESOURCE_REQUESTS),
    }
    fields[field_name] = {}

    with pytest.raises(ValueError, match=f"non-empty {field_name}"):
        SandboxCreateSpec(**fields)
