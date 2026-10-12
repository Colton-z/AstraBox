"""A changed Hermes profile reaches new conversations without ending running ones.

The resident backend is one supervised Hermes process serving every
conversation of an Assistant. What a changed Assistant needs from it depends on
when Hermes reads the input. The model, its provider and SOUL.md are read for
each new session, so they are written into the profile in place and the running
backend keeps serving the other conversations. The process environment and the
MCP servers are fixed when the process starts, so they need a restart; a restart
ends every turn running in the backend, so it waits until none is.

These pin the decision and its order: the box is asked which process
configuration its backend runs under BEFORE the new profile overwrites the
evidence, a per-session change never restarts, and a process change restarts
only after the backend reported itself idle.
"""

from __future__ import annotations

import shlex
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.model.astrabox_models import AgentView
from astrabox.core.service.orchestrator.engine import hermes
from astrabox.seams.sandbox_data import SandboxCommandResult


class _Box:
    """A box that answers commands, recording what it was asked to run."""

    def __init__(
        self,
        *,
        standing: str = "PROCESS_UNCHANGED",
        fresh: bool = False,
        restart_output: str = "started",
        events: list[str] | None = None,
    ) -> None:
        self.commands: list[str] = []
        self.events = events if events is not None else []
        self._standing = standing
        self._fresh = fresh
        self._restart_output = restart_output

    async def run(self, command: str, **_kwargs: Any) -> SandboxCommandResult:
        self.commands.append(command)
        if "PROCESS_UNCHANGED" in command:
            return _ok(self._standing)
        if "supervisorctl" in command:
            self.events.append("restart")
            return _ok(self._restart_output)
        if " publish " in command:
            return _ok(f"HERMES_PROFILE_INITIALIZED fresh={int(self._fresh)}")
        if "astrabox-hermes-config-merge" in command:
            self.events.append("merge_in_place")
            # The merge prints the SOUL marker first, then the config marker.
            return _ok("ASTRABOX_HERMES_SOUL_READY\nASTRABOX_HERMES_CONFIG_READY")
        return _ok("HERMES_PROFILE_READY")


def _ok(stdout: str) -> SandboxCommandResult:
    return SandboxCommandResult(exit_code=0, stdout=stdout, stderr="")


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["ABSENT", "PROCESS_UNCHANGED", "PROCESS_CHANGED"])
async def test_the_box_is_asked_which_process_configuration_it_runs(verdict: str) -> None:
    """Compared in the box against the recorded digest; the profile is never read."""

    box = _Box(standing=verdict)

    answer = await hermes._hermes_process_standing(
        box,
        env_path="/home/u/.hermes/astrabox-hermes.env",
        digest_path="/home/u/.hermes/astrabox-hermes-process.sha256",
        digest="d" * 64,
    )

    assert answer == verdict
    assert len(box.commands) == 1
    probe = box.commands[0]
    assert "/home/u/.hermes/astrabox-hermes.env" in probe
    assert "/home/u/.hermes/astrabox-hermes-process.sha256" in probe
    assert "d" * 64 in probe
    assert 'cat "$1"' not in probe, "the profile carries the model credential"


@pytest.mark.asyncio
async def test_an_unreadable_standing_answer_fails_loudly() -> None:
    """Not knowing is not the same as unchanged."""

    class _Mute(_Box):
        async def run(self, command: str, **_kwargs: Any) -> SandboxCommandResult:
            self.commands.append(command)
            return _ok("")

    with pytest.raises(APIError) as excinfo:
        await hermes._hermes_process_standing(
            _Mute(), env_path="/p/env", digest_path="/p/digest", digest="x" * 64
        )

    assert excinfo.value.code == "HERMES_PROFILE_SETUP_FAILED"


@pytest.mark.asyncio
async def test_a_refused_restart_is_an_error_not_a_shrug() -> None:
    """A backend that did not restart is still serving the profile just replaced."""

    box = _Box(restart_output="astrabox-hermes: ERROR (no such process)")

    with pytest.raises(APIError) as excinfo:
        await hermes._restart_hermes_backend(box)

    assert excinfo.value.code == "HERMES_GATEWAY_START_FAILED"


def _sandbox(box: _Box) -> Any:
    return SimpleNamespace(sandbox_id="sb-1", commands=SimpleNamespace(run=box.run))


_MODEL = "hermes-model"


async def _prepare(
    box: _Box,
    monkeypatch: pytest.MonkeyPatch,
    *,
    template: AgentView | None = None,
    model: str = _MODEL,
    model_api_key: str = "model-key",
    written: list[str] | None = None,
    installed: list[str] | None = None,
) -> str:
    """Run the real profile preparation against a fake box.

    Only the collaborators that reach outside it are replaced — the file
    installs, whose content lands in ``written``, and the wait for the backend
    to go idle, which records itself in the box's event order. Everything that
    decides what the profile says and how it takes effect is the real code.
    """

    async def _record_profile_env(*_args: Any, content: str, **_kwargs: Any) -> None:
        if written is not None:
            written.append(content)

    async def _no_install(*_args: Any, path: str, **_kwargs: Any) -> None:
        if installed is not None:
            installed.append(path)

    async def _wait_until_idle() -> None:
        box.events.append("waited_until_idle")

    monkeypatch.setattr(hermes, "_install_hermes_profile_env_file", _record_profile_env)
    monkeypatch.setattr(hermes, "install_verified_text_script", _no_install)

    return await hermes._prepare_hermes_profile(
        _sandbox(box),
        identity={
            "sandbox_id": "sb-1",
            "sandbox_tenancy": "conversation",
            "linux_user": "u1",
            "home_dir": "/home/conversations/user-1/asst-1",
            "config_dir": "/home/conversations/user-1/asst-1/.hermes",
            "workspace_dir": "/home/conversations/user-1/asst-1/workspace",
            "workspace_source_dir": "/home/conversations/user-1/asst-1/workspace",
        },
        deployment_settings=SimpleNamespace(mcp_proxy_base_url="https://proxy.test"),
        template=template or AgentView(engine_kind="assistant"),
        model_access=hermes.ResolvedModelAccess(
            configuration={},
            base_url="https://gateway.test/v1",
            model_name=model,
            credential="k",
            credential_kind="bearer",
            endpoint_provider="litellm",
        ),
        model_api_key=model_api_key,
        runtime_state_store={
            "base_url": "https://backend.test/api/v1/runtime-state",
            "owner": {
                "user_id": "user-1",
                "subject_kind": "assistant",
                "subject_id": "asst-1",
                "engine_kind": "assistant",
            },
        },
        mcp_deployment_id="deployment-1",
        wait_until_backend_idle=_wait_until_idle,
    )


@pytest.mark.asyncio
async def test_a_process_change_restarts_only_after_the_backend_is_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restart ends every running turn, so the wait comes first, always."""

    box = _Box(standing="PROCESS_CHANGED")

    await _prepare(box, monkeypatch)

    assert box.events == ["waited_until_idle", "restart"]


@pytest.mark.asyncio
async def test_a_per_session_change_is_written_in_place_without_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The running backend keeps serving; the next session reads the new files."""

    box = _Box(standing="PROCESS_UNCHANGED")
    installed: list[str] = []

    await _prepare(box, monkeypatch, installed=installed)

    assert box.events == ["merge_in_place"]
    assert not any(path.endswith("astrabox-hermes-process.sha256") for path in installed), (
        "the recorded digest already names this process configuration"
    )
    merge = next(c for c in box.commands if "astrabox-hermes-config-merge" in c)
    argv = shlex.split(merge)
    assert argv[:3] == ["runuser", "-u", "u1"], "the merge runs as the profile's account"
    assert "/home/conversations/user-1/asst-1/.hermes/astrabox-hermes.env" in argv


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("standing", "fresh"),
    [("ABSENT", False), ("PROCESS_CHANGED", True), ("PROCESS_UNCHANGED", True)],
)
async def test_a_backend_that_has_not_started_is_left_to_its_launcher(
    standing: str, fresh: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No running backend: nothing to wait for, restart, or patch in place.

    ``ABSENT`` is the first materialization, and ``fresh=1`` a box whose
    initialization this publish just released; either way
    `astrabox-hermes-serve` merges the profile and then starts Hermes under it.
    """

    box = _Box(standing=standing, fresh=fresh)

    await _prepare(box, monkeypatch)

    assert box.events == []


@pytest.mark.asyncio
async def test_only_process_inputs_move_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the fingerprint separates is exactly what needs a restart.

    The model and the system prompt are read per session: equal fingerprints,
    so the attachment every other conversation streams on is kept. The model
    credential in the process environment and the MCP servers are read at
    start: a different fingerprint.
    """

    async def fingerprint(**kwargs: Any) -> str:
        return await _prepare(_Box(standing="ABSENT"), monkeypatch, **kwargs)

    base = await fingerprint()
    assert await fingerprint(model="another-model") == base
    assert (
        await fingerprint(template=AgentView(engine_kind="assistant", system="You are Quill."))
        == base
    )
    assert await fingerprint(model_api_key="another-key") != base
    assert (
        await fingerprint(
            template=AgentView(
                engine_kind="assistant",
                mcp_servers={"search": {"type": "http", "url": "https://mcp.test/mcp"}},
            )
        )
        != base
    )
