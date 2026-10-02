"""Script publication must not expose incomplete or unverified content."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.sandbox_script_writer import (
    install_verified_text_script,
)


class _CommandChannel:
    def __init__(self, target: Path, *, fault: str = "") -> None:
        self.target = target
        self.fault = fault
        self.calls: list[str] = []
        self.outputs: list[str] = []
        self.commands = self

    async def run(self, command: str) -> Any:
        self.calls.append(command)
        writing = "base64.b64decode" in command
        publishing = "os.replace(" in command
        releasing = '"released": True' in command
        if "os.mkdir(lock_path)" in command and self.fault == "acquire":
            return SimpleNamespace(error="lock acquisition refused", logs=None)
        if writing and self.fault == "write":
            raise OSError("command transport lost during write")
        if writing and self.fault == "later_write" and len(self.calls) == 3:
            return SimpleNamespace(error="second chunk rejected", logs=None)
        if releasing and self.fault == "release":
            return SimpleNamespace(error="lock release refused", logs=None)
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
        return SimpleNamespace(
            error=f"exit {process.returncode}" if process.returncode else None,
            logs=SimpleNamespace(
                stdout=[SimpleNamespace(text=stdout.decode())],
                stderr=[SimpleNamespace(text=stderr.decode())],
            ),
        )


async def _install(box: _CommandChannel, content: str, *, mode: int = 0o600) -> None:
    await install_verified_text_script(
        SimpleNamespace(sandbox=box), path=str(box.target), content=content,
        runtime_identity=None,
        mode=mode, error_code="AGENT_RUNTIME_ERROR", error_message="install failed",
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


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "instructions\n", "你好 ' $HOME `uname`\nPY\n" * 2000])
@pytest.mark.parametrize("mode", [0o600, 0o644, 0o755])
async def test_publication_preserves_bytes_mode_and_bounds_command_count(
    tmp_path: Path, content: str, mode: int,
) -> None:
    target = tmp_path / "new directory" / "agent ' $HOME 文件.md"
    box = _CommandChannel(target)
    await _install(box, content, mode=mode)

    assert target.read_bytes() == content.encode("utf-8")
    assert target.stat().st_mode & 0o777 == mode
    assert not Path(f"{target}.install.lock").exists()
    assert not list(target.parent.glob(f"{target.name}.tmp.*"))
    encoded_size = 4 * ((len(content.encode("utf-8")) + 2) // 3)
    assert len(box.calls) == max(1, (encoded_size + 15_999) // 16_000) + 3
    assert max(map(len, box.calls)) < 18_000


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["truncate", "same_size", "missing", "write"])
async def test_failed_staging_keeps_the_previous_file_and_releases_lock(
    tmp_path: Path, fault: str,
) -> None:
    target = tmp_path / "instructions.md"
    target.write_text("previous instructions")
    box = _CommandChannel(target, fault=fault)
    with pytest.raises(APIError) as error:
        await _install(box, "new instructions")
    assert error.value.code == "AGENT_RUNTIME_ERROR"
    assert "install failed" in error.value.message
    assert target.read_text() == "previous instructions"
    assert not Path(f"{target}.install.lock").exists()
    if fault in {"truncate", "same_size"}:
        assert '"ok": false' in error.value.message


@pytest.mark.asyncio
async def test_replace_failure_is_reported_and_preserves_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "occupied"
    target.mkdir()
    sentinel = target / "keep"
    sentinel.write_text("user content")
    with pytest.raises(APIError, match="install failed"):
        await _install(_CommandChannel(target), "new instructions")
    assert sentinel.read_text() == "user content"
    assert not Path(f"{target}.install.lock").exists()


@pytest.mark.asyncio
async def test_a_failed_later_chunk_cannot_publish_a_partial_script(tmp_path: Path) -> None:
    target = tmp_path / "instructions.md"
    target.write_text("previous instructions")
    box = _CommandChannel(target, fault="later_write")
    with pytest.raises(APIError, match="second chunk rejected"):
        await _install(box, "A" * 30_001)
    assert target.read_text() == "previous instructions"
    assert len(box.calls) == 4
    assert not Path(f"{target}.install.lock").exists()


@pytest.mark.asyncio
async def test_release_failure_is_not_reported_as_success(tmp_path: Path) -> None:
    target = tmp_path / "instructions.md"
    with pytest.raises(APIError, match="failed to release script install lock"):
        await _install(_CommandChannel(target, fault="release"), "new instructions")
    assert target.read_text() == "new instructions"
    assert Path(f"{target}.install.lock").is_dir()


@pytest.mark.asyncio
async def test_published_content_is_checked_after_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A filesystem fault after rename must fail the final verification too.
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "original_replace = os.replace\n"
        "def replace(source, target):\n"
        "    original_replace(source, target)\n"
        "    Path(target).write_bytes(b'corrupt')\n"
        "os.replace = replace\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(hooks))
    target = tmp_path / "instructions.md"
    with pytest.raises(APIError, match='"ok": false'):
        await _install(_CommandChannel(target), "new instructions")
    assert target.read_bytes() == b"corrupt"
    assert not Path(f"{target}.install.lock").exists()


@pytest.mark.asyncio
async def test_concurrent_installs_never_publish_partial_content(tmp_path: Path) -> None:
    target = tmp_path / "instructions.md"
    original = b"previous instructions"
    target.write_bytes(original)
    contents = ["A" * 30_001, "B" * 30_001]
    observed = {original}
    first = asyncio.create_task(_install(_CommandChannel(target), contents[0]))
    second = asyncio.create_task(_install(_CommandChannel(target), contents[1]))
    try:
        while not first.done() or not second.done():
            observed.add(target.read_bytes())
            await asyncio.sleep(0.001)
        await asyncio.gather(first, second)
    finally:
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
    assert observed <= {original, *(content.encode() for content in contents)}
    assert target.read_text() in contents
    assert not Path(f"{target}.install.lock").exists()


@pytest.mark.asyncio
async def test_lock_acquisition_failure_keeps_another_installers_lock(tmp_path: Path) -> None:
    target = tmp_path / "instructions.md"
    lock = Path(f"{target}.install.lock")
    lock.write_text("do not remove")
    with pytest.raises(APIError, match="install failed"):
        await _install(_CommandChannel(target, fault="acquire"), "new instructions")
    assert lock.read_text() == "do not remove"
    assert not target.exists()
