"""Script publication writes only where a complete runtime identity says, with the
mode it asks for.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.sandbox_script_writer import (
    install_verified_text_script,
)
from astrabox.seams.sandbox_data import SandboxCommandResult


class _CommandChannel:
    def __init__(self, target: Path, *, fault: str = "") -> None:
        self.target = target
        self.fault = fault
        self.calls: list[str] = []
        self.outputs: list[str] = []
        self.commands = self

    async def run(self, command: str, **_options: Any) -> SandboxCommandResult:
        self.calls.append(command)
        writing = "base64.b64decode" in command
        publishing = "os.replace(" in command
        releasing = '"released": True' in command
        if "os.mkdir(lock_path)" in command and self.fault == "acquire":
            return SandboxCommandResult(exit_code=1, stdout="", stderr="lock acquisition refused")
        if writing and self.fault == "write":
            raise OSError("command transport lost during write")
        if writing and self.fault == "later_write" and len(self.calls) == 3:
            return SandboxCommandResult(exit_code=1, stdout="", stderr="second chunk rejected")
        if releasing and self.fault == "release":
            return SandboxCommandResult(exit_code=1, stdout="", stderr="lock release refused")
        if publishing and self.fault in {"truncate", "same_size", "missing"}:
            staged, = self.target.parent.glob(f"{self.target.name}.tmp.*")
            if self.fault == "missing":
                staged.unlink()
            elif self.fault == "truncate":
                staged.write_bytes(b"")
            else:
                staged.write_bytes(b"!" * staged.stat().st_size)
        env = dict(os.environ)
        env["PATH"] = f"{Path(sys.executable).parent}:{env.get('PATH', '')}"
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await process.communicate()
        self.outputs.append(stdout.decode() + stderr.decode())
        return SandboxCommandResult(
            exit_code=int(process.returncode or 0),
            stdout=stdout.decode(),
            stderr=stderr.decode(),
        )


@pytest.mark.parametrize("identity", [
    {},
    {"sandbox_tenancy": "agent", "workspace_dir": "/workspace"},
    {"sandbox_tenancy": "agent", "workspace_dir": "/workspace",
     "linux_user": "conversation", "home_dir": "/home/conversation"},
])
async def test_incomplete_identity_cannot_write_to_the_box_workspace(
    tmp_path: Path, identity: dict[str, Any],
) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("unchanged")
    box = _CommandChannel(target)
    with pytest.raises(APIError, match="runtime identity is incomplete"):
        await install_verified_text_script(
            box, path=str(target), content="new instructions",
            runtime_identity=identity, error_code="AGENT_RUNTIME_ERROR",
            error_message="install failed",
        )
    assert box.calls == []
    assert target.read_text() == "unchanged"


async def test_private_profile_path_keeps_its_identity_outside_the_workspace(
    tmp_path: Path,
) -> None:
    home = tmp_path / "profile"
    target = home / "config" / "settings.json"
    box = _CommandChannel(target)
    await install_verified_text_script(
        box, path=str(target), content="profile settings",
        runtime_identity={
            "sandbox_tenancy": "agent", "linux_user": "profile",
            "home_dir": str(home), "workspace_dir": "/workspace",
            "workspace_source_dir": str(home / "workspace"),
        },
        mode=0o600, error_code="AGENT_RUNTIME_ERROR", error_message="install failed",
    )
    assert target.read_text() == "profile settings"
    assert target.stat().st_mode & 0o777 == 0o600
