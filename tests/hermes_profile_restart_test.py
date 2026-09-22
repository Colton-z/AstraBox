"""A changed Hermes profile must reach the backend that already read one.

Hermes composes its agent — the model route, the API key, and everything
`astrabox-hermes-config-merge` writes into `~/.hermes/config.json` — once, when
it starts. The PTY design never had to think about this: every conversation was
a new process, so the newest profile was always the one in effect. The resident
backend is one supervised process for the life of the box, so an Assistant whose
model changed would keep being answered by the old one, silently, until the box
next restarted.

These pin the decision that closes that, and both halves of it: the box is
asked what profile it already holds BEFORE the new one overwrites it, and only a
genuine change spends a restart. Restarting on every wake would give back the
startup time this whole workstream bought; restarting on none would answer from
a configuration the owner replaced.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.model.astrabox_models import AgentView
from astrabox.core.service.orchestrator.engine import hermes


class _Box:
    """A box that answers commands, recording what it was asked to run."""

    def __init__(self, *, standing: str = "UNCHANGED", restart_output: str = "started") -> None:
        self.commands: list[str] = []
        self._standing = standing
        self._restart_output = restart_output

    async def run(self, command: str, **_kwargs: Any) -> Any:
        self.commands.append(command)
        if "sha256sum" in command:
            return SimpleNamespace(error=None, stdout=f"{self._standing}\n")
        if "supervisorctl" in command:
            return SimpleNamespace(error=None, stdout=self._restart_output)
        if " publish " in command:
            return SimpleNamespace(error=None, stdout="HERMES_PROFILE_INITIALIZED fresh=0\n")
        return SimpleNamespace(error=None, stdout="HERMES_PROFILE_READY\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["ABSENT", "UNCHANGED", "CHANGED"])
async def test_the_box_is_asked_which_profile_it_already_holds(verdict: str) -> None:
    """The comparison happens in the box, against the digest about to be written."""

    box = _Box(standing=verdict)

    answer = await hermes._hermes_profile_env_standing(
        box.run, path="/home/u/.hermes/astrabox-hermes.env", digest="d" * 64
    )

    assert answer == verdict
    assert len(box.commands) == 1
    probe = box.commands[0]
    assert "sha256sum" in probe, "the profile must not be read back out of the box"
    assert "/home/u/.hermes/astrabox-hermes.env" in probe
    assert "d" * 64 in probe


@pytest.mark.asyncio
async def test_an_unreadable_standing_answer_fails_loudly() -> None:
    """Not knowing is not the same as unchanged.

    Defaulting to "unchanged" here would be the quiet degradation this codebase
    forbids: the backend would keep serving a profile nobody could confirm.
    """

    class _Mute(_Box):
        async def run(self, command: str, **_kwargs: Any) -> Any:
            self.commands.append(command)
            return SimpleNamespace(error=None, stdout="")

    with pytest.raises(APIError) as excinfo:
        await hermes._hermes_profile_env_standing(
            _Mute().run, path="/p/env", digest="x" * 64
        )

    assert excinfo.value.code == "HERMES_PROFILE_SETUP_FAILED"


@pytest.mark.asyncio
async def test_the_restart_names_the_supervised_program() -> None:
    box = _Box()

    await hermes._restart_hermes_backend(box.run)

    assert len(box.commands) == 1
    assert "supervisorctl restart astrabox-hermes" in box.commands[0]


@pytest.mark.asyncio
async def test_a_refused_restart_is_an_error_not_a_shrug() -> None:
    """A backend that did not restart is still serving the profile just replaced."""

    box = _Box(restart_output="astrabox-hermes: ERROR (no such process)")

    with pytest.raises(APIError) as excinfo:
        await hermes._restart_hermes_backend(box.run)

    assert excinfo.value.code == "HERMES_GATEWAY_START_FAILED"


def _sandbox(box: _Box) -> Any:
    return SimpleNamespace(sandbox_id="sb-1", commands=SimpleNamespace(run=box.run))


async def _prepare(box: _Box, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run the real profile preparation against a fake box.

    Only the collaborators that reach outside it are replaced — the skill
    repos and the file install. Everything that decides whether the backend
    must restart is the real code.
    """

    async def _no_repos(*_args: Any, **_kwargs: Any) -> list[str]:
        return []

    async def _no_install(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(hermes, "_prepare_hermes_skill_repos", _no_repos)
    monkeypatch.setattr(hermes, "_install_hermes_profile_env_file", _no_install)
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
        deployment_settings=SimpleNamespace(),
        template=AgentView(engine_kind="assistant"),
        model_access=hermes.ResolvedModelAccess(
            configuration={},
            base_url="https://gateway.test/v1",
            model_name="hermes-model",
            credential="k",
            credential_kind="bearer",
            endpoint_provider="litellm",
        ),
        model_api_key="model-key",
        user_id="user-1",
        conversation_user_id="user-1",
        assistant_id="asst-1",
        runtime_state_store={
            "base_url": "https://backend.test/api/v1/runtime-state",
            "owner": {
                "user_id": "user-1",
                "subject_kind": "assistant",
                "subject_id": "asst-1",
                "engine_kind": "assistant",
            },
        },
    )


@pytest.mark.asyncio
async def test_a_changed_profile_restarts_the_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point, pinned where dropping the call would show.

    Without this the helpers above stay green while an Assistant is answered by
    a backend still running under the profile it was configured away from.
    """

    box = _Box(standing="CHANGED")

    await _prepare(box, monkeypatch)

    assert any("supervisorctl restart astrabox-hermes" in c for c in box.commands), (
        "a profile whose content moved must reach the backend that read the old one"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["UNCHANGED", "ABSENT"])
async def test_an_unmoved_profile_does_not_spend_a_restart(
    verdict: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every ordinary wake, and the first materialization, cost nothing.

    ``ABSENT`` is the first materialization: `astrabox-hermes-serve` is still
    waiting for this very file, and restarting would fight its readiness gate.
    ``UNCHANGED`` is every wake after — restarting there would give back the
    startup time this transport was built to save.
    """

    box = _Box(standing=verdict)

    await _prepare(box, monkeypatch)

    assert not [c for c in box.commands if "supervisorctl" in c]
