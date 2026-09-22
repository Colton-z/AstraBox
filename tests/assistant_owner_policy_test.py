"""Assistant single-owner policy: program restriction + ownership boundary.

An assistant is one owner's long-lived personal runtime. It runs the resident
Assistant program only (``claude_code`` is the per-Session Agent program), and every
catalog/workspace operation requires the caller to BE the owner — missing and
foreign assistants share one non-disclosing 404.
"""

from __future__ import annotations

import shlex
from types import SimpleNamespace
from typing import Any

import pytest

import astrabox.providers as providers
from astrabox.common.utils.errors import APIError
from astrabox.seams.sandbox_disposal import SandboxDestruction
from astrabox.core.service.orchestrator.assistant.assistant_service import (
    AssistantService,
)


_SPAWNED: list[Any] = []


def _run_now(coroutine, *, name: str | None = None):
    """Collect a spawned commit so a test can await it deliberately.

    The production spawner detaches the commit from the request, which is the
    point: the rollback must run whether or not a caller is listening. A test
    that asserts on the commit's outcome therefore has to say when it wants it,
    rather than race a task it never named.
    """
    import asyncio

    task = asyncio.ensure_future(coroutine)
    _SPAWNED.append(task)
    return task


async def _drain_spawned() -> None:
    import asyncio

    pending, _SPAWNED[:] = list(_SPAWNED), []
    if pending:
        await asyncio.gather(*pending)


providers.register_builtin_providers()


class _FakeCatalogRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def create_assistant(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.rows[payload["assistant_id"]] = dict(payload)
        return dict(payload)

    async def soft_delete(self, assistant_id: str) -> bool:
        row = self.rows.pop(assistant_id, None)
        return row is not None

    async def get_assistant(self, assistant_id: str) -> dict[str, Any] | None:
        row = self.rows.get(assistant_id)
        return dict(row) if row else None

    async def list_assistants(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows.values()]

    async def update_assistant(
        self,
        assistant_id: str,
        updates: dict[str, Any],
    ) -> bool:
        row = self.rows.get(assistant_id)
        if row is None:
            return False
        row.update(updates)
        return True


class _FakeTemplateService:
    async def get_environment(self, name: str) -> Any:
        # The Assistant selects an Environment, including its Agent program and
        # model access; it does not reference a separate template.
        return {"name": name, "enabled": True, "engine_kind": "assistant"}


class _FakeWorkspaceService:
    def __init__(self) -> None:
        self.materialized: list[str] = []
        self.workspace: dict[str, Any] | None = None
        self.post_commit_failures: list[dict[str, Any]] = []
        self.hibernated: list[str] = []
        self.transitions: list[dict[str, Any]] = []
        self.released: list[dict[str, Any]] = []
        #: whether the release compare-and-set wins (False = a concurrent
        #: transition took the workspace out of RECOVERY_REQUIRED first).
        self.release_wins = True
        self.hibernate_calls: list[dict[str, Any]] = []
        self.recovery_required: list[dict[str, Any]] = []
        self.adopted: list[dict[str, Any]] = []
        self.hibernation_begun: list[dict[str, Any]] = []
        self.release_required: list[dict[str, Any]] = []
        self.hibernate_restored: list[dict[str, Any]] = []
        #: whether the hibernate compare-and-set wins (False = the row moved to a
        #: different sandbox while this hibernate was reading it).
        self.hibernate_wins = True

    async def materialize_workspace_if_absent(self, **kwargs: Any) -> dict[str, Any]:
        self.materialized.append(str(kwargs.get("assistant_id")))
        if self.workspace is None:
            self.workspace = {
                "assistant_id": kwargs.get("assistant_id"),
                "engine_kind": kwargs.get("engine_kind"),
                "state": "MATERIALIZING",
                "current_sandbox_id": None,
                "provisioning_session_id": kwargs.get(
                    "provisioning_session_id"
                ),
            }
        return dict(self.workspace or {})

    async def claim_materialization(self, **kwargs: Any) -> bool:
        self.transitions.append(
            {
                "expected_state": kwargs.get("expected_state"),
                "new_state": "MATERIALIZING",
                "extra_updates": {
                    "provisioning_session_id": kwargs.get(
                        "provisioning_session_id"
                    )
                },
            }
        )
        if self.workspace is not None:
            self.workspace.update(
                {
                    "state": "MATERIALIZING",
                    "provisioning_session_id": kwargs.get(
                        "provisioning_session_id"
                    ),
                }
            )
        return True

    async def mark_materialization_failed(self, **kwargs: Any) -> bool:
        return True

    async def transition_state(self, **kwargs: Any) -> bool:
        self.transitions.append(dict(kwargs))
        return True

    async def has_assistant_workspace(self, *, assistant_id: str) -> bool:
        _ = assistant_id
        return False

    async def get_workspace(
        self, *, user_id: str, assistant_id: str
    ) -> dict[str, Any] | None:
        _ = (user_id, assistant_id)
        return self.workspace

    async def list_user_workspaces(
        self, *, user_id: str
    ) -> list[dict[str, Any]]:
        _ = user_id
        return [dict(self.workspace)] if self.workspace is not None else []

    async def mark_post_commit_failure(self, **kwargs: Any) -> bool:
        self.post_commit_failures.append(dict(kwargs))
        return True

    async def finish_hibernation(self, **kwargs: Any) -> bool:
        self.hibernated.append(str(kwargs.get("assistant_id")))
        self.hibernate_calls.append(dict(kwargs))
        if self.workspace is not None:
            self.workspace.update(
                {
                    "state": "HIBERNATING",
                    "current_sandbox_id": None,
                    "runtime_identity": None,
                    "provisioning_session_id": None,
                    "provisioning_sandbox_generation": None,
                }
            )
        return True

    async def mark_recovery_required(self, **kwargs: Any) -> bool:
        self.recovery_required.append(dict(kwargs))
        return True

    async def adopt_undestroyed_sandbox(self, **kwargs: Any) -> bool:
        self.adopted.append(dict(kwargs))
        return True

    async def begin_hibernation(self, **kwargs: Any) -> bool:
        self.hibernation_begun.append(dict(kwargs))
        if self.hibernate_wins and self.workspace is not None:
            self.workspace.update(
                {
                    "state": "HIBERNATING",
                    "hibernated_at": kwargs.get("hibernated_at"),
                }
            )
        return self.hibernate_wins

    async def mark_hibernation_release_required(self, **kwargs: Any) -> bool:
        self.release_required.append(dict(kwargs))
        if self.workspace is not None:
            self.workspace["state"] = "RECOVERY_REQUIRED"
        return True

    async def restore_ready_after_hibernation_failure(self, **kwargs: Any) -> bool:
        self.hibernate_restored.append(dict(kwargs))
        if self.workspace is not None:
            self.workspace.update({"state": "READY", "hibernated_at": None})
        return True

    async def release_recovered_sandbox(self, **kwargs: Any) -> bool:
        self.released.append(dict(kwargs))
        if self.release_wins and self.workspace is not None:
            self.workspace["current_sandbox_id"] = None
        return self.release_wins


class _FakeSessionKernel:
    """Records the hidden bootstrap sessions a rebuild asks for."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create_session(self, user: Any, environment_name: str, **kwargs: Any):
        _ = (user, environment_name)
        self.created.append(dict(kwargs))
        return {"session_id": kwargs["session_id"]}


def _service(
    *, runtime_manager: Any = None, session_kernel: Any = None
) -> tuple[AssistantService, _FakeCatalogRepo, _FakeWorkspaceService]:
    catalog = _FakeCatalogRepo()
    workspace_service = _FakeWorkspaceService()
    service = AssistantService(
        agent_config=_FakeTemplateService(),
        session_kernel=session_kernel,
        runtime_manager=runtime_manager,
        catalog_repo=catalog,  # type: ignore[arg-type]
        workspace_service=workspace_service,  # type: ignore[arg-type]
        spawn_background_task=_run_now,
    )
    return service, catalog, workspace_service


def _user(user_id: str = "owner-1") -> Any:
    return SimpleNamespace(user_id=user_id)


async def test_create_defaults_to_the_assistant_engine() -> None:
    service, catalog, _workspace = _service()

    created = await service.create_assistant(
        _user(),
        {"display_name": "Jarvis", "environment_name": "env-1"},
    )

    assert created["engine_kind"] == "assistant"
    assert catalog.rows[created["assistant_id"]]["owner_id"] == "owner-1"


@pytest.mark.parametrize(
    ("field", "value", "capability_name"),
    [
        (
            "plugin_repos_override",
            [{"url": "https://example.invalid/plugin.git", "protocol": "https"}],
            "plugin_repos",
        ),
        ("skill_manifest_override", ["repo/skill"], "skills"),
    ],
)
async def test_assistant_rejects_configuration_its_engine_does_not_consume(
    field: str,
    value: Any,
    capability_name: str,
) -> None:
    service, _catalog, _workspace = _service()

    with pytest.raises(APIError) as raised:
        await service.create_assistant(
            _user(),
            {
                "display_name": "Jarvis",
                "environment_name": "env-1",
                field: value,
            },
        )

    assert raised.value.status_code == 400
    assert capability_name in raised.value.message


async def test_assistant_accepts_the_mcp_configuration_hermes_consumes() -> None:
    service, catalog, _workspace = _service()

    created = await service.create_assistant(
        _user(),
        {
            "display_name": "Jarvis",
            "environment_name": "env-1",
            "mcp_config_override": {"search": {"type": "http", "url": "https://mcp.invalid"}},
        },
    )

    assert catalog.rows[created["assistant_id"]]["mcp_config_override"] == {
        "search": {"type": "http", "url": "https://mcp.invalid"}
    }


async def test_create_rejects_an_environment_without_engine_identity() -> None:
    service, _catalog, _workspace = _service()

    class _InvalidEnvironmentService(_FakeTemplateService):
        async def get_environment(self, name: str) -> dict[str, Any] | None:
            return {"name": name, "enabled": True}

    service._agent_config = _InvalidEnvironmentService()

    with pytest.raises(APIError) as raised:
        await service.create_assistant(
            _user(),
            {"display_name": "Jarvis", "environment_name": "env-1"},
        )

    assert raised.value.code == "ASSISTANT_ENVIRONMENT_INVALID"
    assert raised.value.status_code == 500


async def test_create_rejects_claude_code_structurally() -> None:
    service, _catalog, _workspace = _service()

    with pytest.raises(APIError) as raised:
        await service.create_assistant(
            _user(),
            {
                "display_name": "Jarvis",
                "environment_name": "env-1",
                "engine_kind": "claude_code",
            },
        )

    assert raised.value.code == "ASSISTANT_ENGINE_UNSUPPORTED"
    assert raised.value.status_code == 400
    assert "assistant_chat support" in raised.value.message


async def test_foreign_and_missing_assistants_share_a_non_disclosing_404() -> None:
    service, _catalog, _workspace = _service()
    created = await service.create_assistant(
        _user("owner-1"),
        {"display_name": "Jarvis", "environment_name": "env-1"},
    )

    with pytest.raises(APIError) as foreign:
        await service.get_assistant(_user("intruder"), created["assistant_id"])
    with pytest.raises(APIError) as missing:
        await service.get_assistant(_user("owner-1"), "asst_does_not_exist")

    assert foreign.value.code == missing.value.code == "ASSISTANT_NOT_FOUND"
    assert foreign.value.status_code == missing.value.status_code == 404


async def test_listing_shows_only_the_callers_assistants() -> None:
    service, _catalog, workspace = _service()
    mine = await service.create_assistant(
        _user("owner-1"), {"display_name": "Mine", "environment_name": "env-1"}
    )
    await service.create_assistant(
        _user("owner-2"), {"display_name": "Theirs", "environment_name": "env-1"}
    )
    workspace.workspace = {
        "assistant_id": mine["assistant_id"],
        "state": "READY",
        "current_sandbox_id": "sandbox-mine",
    }

    listed = await service.list_assistants(_user("owner-1"))

    assert [row["assistant_id"] for row in listed] == [mine["assistant_id"]]
    assert listed[0]["workspace_state"] == "READY"
    assert listed[0]["current_sandbox_id"] == "sandbox-mine"


async def test_wake_rejects_a_legacy_claude_code_assistant_loudly() -> None:
    service, catalog, _workspace = _service()
    # A pre-policy row: engine_kind was immutable once materialized, so this
    # cannot be edited into compliance — wake must fail loud, not fall back.
    catalog.rows["asst_legacy"] = {
        "assistant_id": "asst_legacy",
        "owner_id": "owner-1",
        "display_name": "Old",
        "environment_name": "env-1",
        "engine_kind": "claude_code",
    }

    with pytest.raises(APIError) as raised:
        await service.wake_workspace(_user("owner-1"), "asst_legacy")

    assert raised.value.code == "ASSISTANT_ENGINE_UNSUPPORTED"
    assert raised.value.status_code == 409


class _UnconfirmedKillRuntimeManager:
    """A destroy that ran and could not be confirmed — the id survives on it."""

    async def destroy_sandbox_by_id(self, sandbox_id: str) -> SandboxDestruction:
        return SandboxDestruction.unconfirmed(
            sandbox_id, detail="the control plane did not answer the confirming probe"
        )

    async def terminate_runtime(
        self, runtime_key: str, *, fallback_sandbox_id: str | None = None
    ) -> SandboxDestruction:
        return SandboxDestruction.unconfirmed(
            fallback_sandbox_id or f"{runtime_key}-orphan",
            detail="the control plane did not answer the confirming probe",
        )


class _WorkspaceReleaseRuntimeManager:
    """Expose command results to the real engine save barrier and record release."""

    def __init__(
        self,
        *,
        save_error: Exception | None = None,
        destroys: bool = True,
    ) -> None:
        self.save_error = save_error
        self._destroys = destroys
        self.calls: list[str] = []
        self.terminated: list[str] = []
        self.save_commands: list[list[str]] = []
        self.stopped_programs: set[str] = set()
        self.destroyed: list[str] = []

    async def connect_sandbox_only(self, sandbox_id: str) -> Any:
        self.calls.append("connect")

        class _Box:
            def __init__(inner_self) -> None:
                inner_self.commands = SimpleNamespace(run=self._run_command)

            async def close(inner_self) -> None:
                self.calls.append("close")

        return _Box()

    async def _run_command(self, command: str) -> Any:
        argv = shlex.split(command)
        programs = {"astrabox-hermes-state-mirror", "astrabox-hermes"}
        if argv[:2] == ["supervisorctl", "stop"] and argv[2] in programs:
            self.stopped_programs.add(argv[2])
            self.calls.append(f"stop:{argv[2]}")
            return SimpleNamespace(exit_code=0, stdout="", error=None)
        if argv[:2] == ["bash", "-lc"]:
            program = shlex.split(argv[2])[3].rstrip(";")
            assert program in programs
            self.calls.append(f"status:{program}")
            state = "STOPPED" if program in self.stopped_programs else "RUNNING"
            return SimpleNamespace(exit_code=0, stdout=f"{program} {state}", error=None)
        if argv[:1] == ["runuser"] and "astrabox-hermes-state-save" in argv:
            assert self.stopped_programs == programs
            self.calls.append("save")
            self.save_commands.append(argv)
            if self.save_error is not None:
                raise self.save_error
            return SimpleNamespace(
                exit_code=0,
                stdout="HERMES_STATE_SAVE_COMPLETE snapshot_id=owner-snapshot",
                error=None,
            )
        raise AssertionError(f"unexpected sandbox command: {command}")

    async def destroy_sandbox_by_id(self, sandbox_id: str) -> SandboxDestruction:
        self.calls.append("destroy")
        self.destroyed.append(sandbox_id)
        if self._destroys:
            return SandboxDestruction.confirmed_gone(
                sandbox_id, detail="the fake control plane confirmed destruction"
            )
        return SandboxDestruction.unconfirmed(
            sandbox_id,
            detail="the control plane did not confirm destruction",
        )

    async def get_sandbox_lifecycle_probe(self, sandbox_id: str) -> Any:
        _ = sandbox_id
        return SimpleNamespace(
            probe_status="OK",
            sandbox_state="RUNNING",
            error_text=None,
        )

    @staticmethod
    def _is_terminal_sandbox_lifecycle_probe(probe: Any) -> bool:
        return str(getattr(probe, "probe_status", "") or "") == "NOT_FOUND"

    async def terminate_runtime(
        self, runtime_key: str, *, fallback_sandbox_id: str | None = None
    ) -> SandboxDestruction:
        self.calls.append("terminate")
        self.terminated.append(runtime_key)
        return SandboxDestruction.confirmed_gone(
            fallback_sandbox_id or runtime_key,
            detail="the fake control plane confirmed destruction",
        )


def _ready_workspace(assistant_id: str, sandbox_id: str = "sbx-1") -> dict[str, Any]:
    return {
        "assistant_id": assistant_id,
        "state": "READY",
        "current_sandbox_id": sandbox_id,
        "engine_kind": "assistant",
        "runtime_identity": {
            "linux_user": "asst_owner",
            "home_dir": "/home/conversations/owner/asst",
            "config_dir": "/home/conversations/owner/asst/.hermes",
            "workspace_dir": "/workspace",
            "workspace_source_dir": "/home/conversations/owner/asst/workspace",
            "sandbox_tenancy": "agent",
        },
    }


_NATIVE_SAVE_CALLS = [
    "connect",
    "stop:astrabox-hermes-state-mirror", "status:astrabox-hermes-state-mirror",
    "stop:astrabox-hermes", "status:astrabox-hermes",
    "save", "close",
]


async def test_hibernate_commits_the_workspace_then_releases_its_box() -> None:
    """Hibernate saves native state before releasing compute."""
    runtime = _WorkspaceReleaseRuntimeManager()
    service, _catalog, workspace = _service(runtime_manager=runtime)
    created = await service.create_assistant(
        _user("owner-1"), {"display_name": "Jarvis", "environment_name": "env-1"}
    )
    workspace.workspace = _ready_workspace(created["assistant_id"])

    outcome = await service.hibernate_workspace(
        _user("owner-1"), created["assistant_id"]
    )

    assert outcome["hibernated"] is True
    assert outcome["released"] is True
    assert outcome["previous_sandbox_id"] == "sbx-1"
    assert outcome["sandbox_id"] is None
    assert runtime.calls == [*_NATIVE_SAVE_CALLS, "destroy"]
    (save,) = runtime.save_commands
    assert save[:8] == [
        "runuser", "-u", "asst_owner", "--", "env",
        "HOME=/home/conversations/owner/asst", "USER=asst_owner", "LOGNAME=asst_owner",
    ]
    assert save[-4:] == [
        "astrabox-hermes-state-save",
        "/home/conversations/owner/asst/.hermes/astrabox-hermes.env",
        "asst_owner", "/home/conversations/owner/asst",
    ]
    assert '/opt/astrabox/hermes/hermes_state_mirror.py save --profile-env "$1"' in save[12]
    assert runtime.destroyed == ["sbx-1"]
    assert workspace.workspace is not None
    assert workspace.workspace["state"] == "HIBERNATING"
    assert workspace.workspace["current_sandbox_id"] is None


async def test_hibernate_keeps_the_pointer_when_release_is_unconfirmed() -> None:
    """A committed workspace is not a licence to forget a possibly-live box."""
    runtime = _WorkspaceReleaseRuntimeManager(destroys=False)
    service, _catalog, workspace = _service(runtime_manager=runtime)
    created = await service.create_assistant(
        _user("owner-1"), {"display_name": "Jarvis", "environment_name": "env-1"}
    )
    workspace.workspace = _ready_workspace(created["assistant_id"])

    outcome = await service.hibernate_workspace(
        _user("owner-1"), created["assistant_id"]
    )

    assert outcome["hibernated"] is False
    assert outcome["released"] is False
    assert outcome["recovery_required"] is True
    assert outcome["sandbox_id"] == "sbx-1"
    assert runtime.calls == [*_NATIVE_SAVE_CALLS, "destroy"]
    assert workspace.workspace is not None
    assert workspace.workspace["state"] == "RECOVERY_REQUIRED"
    assert workspace.workspace["current_sandbox_id"] == "sbx-1"

    runtime._destroys = True
    retried = await service.hibernate_workspace(_user("owner-1"), created["assistant_id"])
    assert retried["released"] is True
    assert runtime.calls == [*_NATIVE_SAVE_CALLS, "destroy", "destroy"]
    assert runtime.destroyed == ["sbx-1", "sbx-1"]
    assert len(runtime.save_commands) == 1
    assert workspace.workspace["current_sandbox_id"] is None


async def test_hibernate_does_no_io_when_its_owner_claim_loses() -> None:
    """A stale reader must not commit or destroy the replacement workspace."""
    runtime = _WorkspaceReleaseRuntimeManager()
    service, _catalog, workspace = _service(runtime_manager=runtime)
    created = await service.create_assistant(
        _user("owner-1"), {"display_name": "Jarvis", "environment_name": "env-1"}
    )
    workspace.workspace = _ready_workspace(created["assistant_id"])
    workspace.hibernate_wins = False

    with pytest.raises(APIError) as raised:
        await service.hibernate_workspace(_user("owner-1"), created["assistant_id"])

    assert raised.value.code == "ASSISTANT_WORKSPACE_HIBERNATE_CONFLICT"
    assert runtime.calls == []
    assert workspace.workspace["state"] == "READY"


async def test_a_native_state_save_failure_retains_the_same_box_for_retry() -> None:
    """A failed native save keeps admission closed and retains compute for retry."""
    runtime = _WorkspaceReleaseRuntimeManager(save_error=RuntimeError("medium down"))
    service, _catalog, workspace = _service(runtime_manager=runtime)
    created = await service.create_assistant(
        _user("owner-1"), {"display_name": "Jarvis", "environment_name": "env-1"}
    )
    workspace.workspace = _ready_workspace(created["assistant_id"])

    with pytest.raises(APIError) as raised:
        await service.hibernate_workspace(_user("owner-1"), created["assistant_id"])

    assert raised.value.code == "ASSISTANT_WORKSPACE_CONVERGENCE_FAILED"
    assert raised.value.status_code == 503
    assert raised.value.data == {
        "sandbox_id": "sbx-1", "retryable": True, "phase": "hermes_native_state_save",
    }
    assert runtime.calls == _NATIVE_SAVE_CALLS
    assert runtime.destroyed == []
    assert workspace.hibernate_restored == []
    assert workspace.release_required == []
    assert workspace.hibernated == []
    assert workspace.workspace is not None
    assert workspace.workspace["state"] == "HIBERNATING"
    assert workspace.workspace["current_sandbox_id"] == "sbx-1"

    runtime.save_error = None
    outcome = await service.hibernate_workspace(_user("owner-1"), created["assistant_id"])
    assert outcome["released"] is True
    assert runtime.calls == [*_NATIVE_SAVE_CALLS, *_NATIVE_SAVE_CALLS, "destroy"]
    assert runtime.destroyed == ["sbx-1"]
    assert workspace.workspace["current_sandbox_id"] is None


async def test_wake_materializes_a_new_box_after_hibernation_finished() -> None:
    """A finished hibernate retains no old compute binding."""
    runtime = _WorkspaceReleaseRuntimeManager()
    kernel = _FakeSessionKernel()
    service, _catalog, workspace = _service(
        runtime_manager=runtime, session_kernel=kernel
    )
    created = await service.create_assistant(
        _user("owner-1"), {"display_name": "Jarvis", "environment_name": "env-1"}
    )
    workspace.workspace = {
        "assistant_id": created["assistant_id"],
        "engine_kind": "assistant",
        "state": "HIBERNATING",
        "current_sandbox_id": None,
        "provisioning_session_id": None,
    }

    outcome = await service.wake_workspace(_user("owner-1"), created["assistant_id"])

    assert outcome["state"] == "MATERIALIZING"
    assert runtime.calls == []
    assert len(kernel.created) == 1
    (claim,) = workspace.transitions
    assert claim["expected_state"] == "HIBERNATING"
    assert claim["new_state"] == "MATERIALIZING"


async def test_wake_redrives_an_interrupted_commit_before_materializing() -> None:
    """HIBERNATING plus a pointer requires repeating the native save barrier."""
    runtime = _WorkspaceReleaseRuntimeManager()
    kernel = _FakeSessionKernel()
    service, _catalog, workspace = _service(
        runtime_manager=runtime, session_kernel=kernel
    )
    created = await service.create_assistant(
        _user("owner-1"), {"display_name": "Jarvis", "environment_name": "env-1"}
    )
    workspace.workspace = {
        **_ready_workspace(created["assistant_id"]),
        "state": "HIBERNATING",
        "hibernated_at": "2026-08-23T00:00:00+00:00",
        "provisioning_session_id": None,
    }

    outcome = await service.wake_workspace(_user("owner-1"), created["assistant_id"])

    assert outcome["state"] == "MATERIALIZING"
    assert runtime.calls == [*_NATIVE_SAVE_CALLS, "destroy"]
    assert runtime.destroyed == ["sbx-1"]
    assert len(kernel.created) == 1
    assert workspace.workspace is not None
    assert workspace.workspace["state"] == "MATERIALIZING"
    assert workspace.workspace["current_sandbox_id"] is None


async def test_hibernating_a_materializing_workspace_destroys_the_box_it_built() -> None:
    """Hibernation terminates a box that has not reached ``mark_ready``.

    Sandbox creation precedes the ``current_sandbox_id`` write in ``mark_ready``.
    A MATERIALIZING workspace with an empty pointer can therefore own a running
    box that is named only by ``provisioning_session_id``. Hibernation must
    terminate that runtime before the transition clears its last durable name.
    """
    runtime = _ConfirmedKillRuntimeManager()
    service, _catalog, workspace = _service(runtime_manager=runtime)
    created = await service.create_assistant(
        _user("owner-1"), {"display_name": "Jarvis", "environment_name": "env-1"}
    )
    workspace.workspace = {
        "assistant_id": created["assistant_id"],
        "state": "MATERIALIZING",
        "current_sandbox_id": None,
        "provisioning_session_id": "hidden-1",
    }

    outcome = await service.hibernate_workspace(
        _user("owner-1"), created["assistant_id"]
    )

    assert runtime.calls == [
        {"runtime_key": "hidden-1", "fallback_sandbox_id": None}
    ], "the bootstrap's own runtime is where the box's id still lives"
    assert outcome["hibernated"] is True
    # And the publish is fenced on what it believes the pointer holds.
    (hibernate,) = workspace.hibernate_calls
    assert hibernate["destroyed_sandbox_id"] is None


async def test_a_bootstrap_box_that_survives_hibernate_is_adopted_not_forgotten() -> None:
    """An orphan with no pointer to live on gets one, instead of a log line."""
    service, _catalog, workspace = _service(
        runtime_manager=_UnconfirmedKillRuntimeManager()
    )
    created = await service.create_assistant(
        _user("owner-1"), {"display_name": "Jarvis", "environment_name": "env-1"}
    )
    workspace.workspace = {
        "assistant_id": created["assistant_id"],
        "state": "MATERIALIZING",
        "current_sandbox_id": None,
        "provisioning_session_id": "hidden-1",
    }

    outcome = await service.hibernate_workspace(
        _user("owner-1"), created["assistant_id"]
    )

    assert outcome["hibernated"] is False
    assert workspace.hibernated == [], "a live box must not be declared absent"
    (adopted,) = workspace.adopted
    assert adopted["sandbox_id"] == "hidden-1-orphan"
    assert outcome["sandbox_id"] == "hidden-1-orphan"


async def test_deleting_an_assistant_destroys_its_sandbox_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Soft-delete severs the name by unreachability, so it goes last.

    ``get_assistant`` excludes deleted rows, so after the soft-delete every
    path that could act on this workspace — wake (the only retry of a pending
    destruction), hibernate, the console — answers 404 forever, while the
    workspace row keeps pointing at a live box nothing can read and act on.
    """
    runtime = _ConfirmedKillRuntimeManager()
    service, catalog, workspace = _service(runtime_manager=runtime)
    created = await service.create_assistant(
        _user("owner-1"), {"display_name": "Jarvis", "environment_name": "env-1"}
    )
    workspace.workspace = {
        "assistant_id": created["assistant_id"],
        "state": "READY",
        "current_sandbox_id": "sbx-1",
        "provisioning_session_id": "hidden-1",
    }

    destroy = runtime.destroy_sandbox_by_id

    async def destroy_before_soft_delete(sandbox_id: str) -> SandboxDestruction:
        assert await catalog.get_assistant(created["assistant_id"]) is not None
        assert workspace.workspace["current_sandbox_id"] == sandbox_id == "sbx-1"
        return await destroy(sandbox_id)

    monkeypatch.setattr(runtime, "destroy_sandbox_by_id", destroy_before_soft_delete)

    result = await service.delete_assistant(_user("owner-1"), created["assistant_id"])

    assert result["deleted"] is True
    assert runtime.calls == [{"sandbox_id": "sbx-1"}]
    assert workspace.workspace["current_sandbox_id"] is None
    assert created["assistant_id"] not in catalog.rows


async def test_deleting_is_refused_while_the_sandbox_cannot_be_confirmed_gone() -> None:
    """The refusal IS the recoverable direction: the row stays retryable."""
    service, catalog, workspace = _service(
        runtime_manager=_UnconfirmedKillRuntimeManager()
    )
    created = await service.create_assistant(
        _user("owner-1"), {"display_name": "Jarvis", "environment_name": "env-1"}
    )
    workspace.workspace = {
        "assistant_id": created["assistant_id"],
        "state": "READY",
        "current_sandbox_id": "sbx-1",
        "provisioning_session_id": "hidden-1",
    }

    with pytest.raises(APIError) as raised:
        await service.delete_assistant(_user("owner-1"), created["assistant_id"])

    assert raised.value.code == "ASSISTANT_SANDBOX_UNDESTROYED"
    assert (raised.value.data or {})["sandbox_id"] == "sbx-1"
    assert created["assistant_id"] in catalog.rows, (
        "the catalog row is what keeps wake and a retried delete reachable"
    )
    assert workspace.workspace["current_sandbox_id"] == "sbx-1"
    assert workspace.released == []

    confirmed = _ConfirmedKillRuntimeManager()
    service._runtime_manager = confirmed
    retried = await service.delete_assistant(_user("owner-1"), created["assistant_id"])

    assert retried["deleted"] is True
    assert confirmed.calls == [{"sandbox_id": "sbx-1"}]
    assert workspace.workspace["current_sandbox_id"] is None
    assert created["assistant_id"] not in catalog.rows


class _FakeSessionKernel:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create_session(self, user: Any, template_name: str, **kwargs: Any) -> dict[str, Any]:
        self.created.append({"user": user.user_id, "template": template_name, **kwargs})
        return {"session_id": kwargs["session_id"]}


class _ConfirmedKillRuntimeManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def destroy_sandbox_by_id(self, sandbox_id: str) -> SandboxDestruction:
        self.calls.append({"sandbox_id": sandbox_id})
        return SandboxDestruction.confirmed_gone(
            sandbox_id, detail="the control plane no longer knows it"
        )

    async def terminate_runtime(
        self, runtime_key: str, *, fallback_sandbox_id: str | None = None
    ) -> SandboxDestruction:
        self.calls.append(
            {"runtime_key": runtime_key, "fallback_sandbox_id": fallback_sandbox_id}
        )
        if not fallback_sandbox_id:
            return SandboxDestruction.nothing_named(detail="no sandbox was named")
        return SandboxDestruction.confirmed_gone(
            fallback_sandbox_id, detail="the control plane no longer knows it"
        )


class _RaisingRuntimeManager:
    async def destroy_sandbox_by_id(self, sandbox_id: str) -> SandboxDestruction:
        raise RuntimeError("control plane unreachable")

    async def terminate_runtime(
        self, *_args: Any, **_kwargs: Any
    ) -> SandboxDestruction:
        raise RuntimeError("control plane unreachable")


async def _recovering_workspace(
    *, runtime_manager: Any = None
) -> tuple[Any, Any, Any, str]:
    """An owned assistant whose workspace is RECOVERY_REQUIRED with the
    previous sandbox's destruction still unconfirmed (the pointer retained)."""
    kernel = _FakeSessionKernel()
    service, _catalog, workspace = _service(runtime_manager=runtime_manager)
    service._session_kernel = kernel  # type: ignore[attr-defined]
    created = await service.create_assistant(
        _user("owner-1"), {"display_name": "Jarvis", "environment_name": "env-1"}
    )
    workspace.workspace = {
        "assistant_id": created["assistant_id"],
        "engine_kind": "assistant",
        "state": "RECOVERY_REQUIRED",
        "current_sandbox_id": "sbx-old",
        "provisioning_session_id": "hidden-1",
    }
    return service, workspace, kernel, created["assistant_id"]


async def test_wake_retries_the_destruction_and_rebuilds_once_it_is_confirmed() -> None:
    """The retained pointer exists so the destruction can be retried against
    that exact sandbox, and wake is the retry: kill it, release the pointer,
    then bootstrap in the same call. Nothing else clears the pointer, so
    without this a RECOVERY_REQUIRED workspace would never be wakeable again."""
    runtime_manager = _ConfirmedKillRuntimeManager()
    service, workspace, kernel, assistant_id = await _recovering_workspace(
        runtime_manager=runtime_manager
    )

    outcome = await service.wake_workspace(_user("owner-1"), assistant_id)

    # The kill targets exactly the sandbox the pointer named.
    assert runtime_manager.calls == [{"sandbox_id": "sbx-old"}]
    (release,) = workspace.released
    assert release["sandbox_id"] == "sbx-old"
    # …and the same call goes on to rebuild.
    assert outcome["state"] == "MATERIALIZING"
    assert len(kernel.created) == 1
    (transition,) = workspace.transitions
    assert transition["expected_state"] == "RECOVERY_REQUIRED"
    assert transition["new_state"] == "MATERIALIZING"


async def test_wake_keeps_the_pointer_when_the_kill_is_unconfirmed() -> None:
    """An unconfirmed kill must not release the pointer: it is the sandbox's
    last surviving name, so releasing it early would leave a running box
    nothing can ever address again. Report the retryable posture instead."""
    service, workspace, kernel, assistant_id = await _recovering_workspace(
        runtime_manager=_UnconfirmedKillRuntimeManager()
    )

    outcome = await service.wake_workspace(_user("owner-1"), assistant_id)

    assert outcome["state"] == "RECOVERY_REQUIRED"
    assert outcome["retryable"] is True
    assert outcome["recovery_pending_sandbox_id"] == "sbx-old"
    assert workspace.released == []
    assert kernel.created == []
    assert workspace.transitions == []


async def test_wake_keeps_the_pointer_when_the_kill_raises() -> None:
    # A control-plane failure is exactly as unconfirmed as a False return.
    service, workspace, kernel, assistant_id = await _recovering_workspace(
        runtime_manager=_RaisingRuntimeManager()
    )

    outcome = await service.wake_workspace(_user("owner-1"), assistant_id)

    assert outcome["state"] == "RECOVERY_REQUIRED"
    assert outcome["retryable"] is True
    assert workspace.released == []
    assert kernel.created == []


async def test_wake_without_a_runtime_manager_cannot_confirm_and_does_not_release() -> None:
    # No way to kill means no way to confirm; releasing on faith would strand
    # the box.
    service, workspace, kernel, assistant_id = await _recovering_workspace()

    outcome = await service.wake_workspace(_user("owner-1"), assistant_id)

    assert outcome["state"] == "RECOVERY_REQUIRED"
    assert outcome["retryable"] is True
    assert workspace.released == []
    assert kernel.created == []


async def test_wake_defers_when_the_release_loses_its_compare_and_set() -> None:
    # A concurrent transition took the workspace out of RECOVERY_REQUIRED
    # between the read and the release; this wake owns nothing and must not
    # bootstrap on top of whatever now owns it.
    runtime_manager = _ConfirmedKillRuntimeManager()
    service, workspace, kernel, assistant_id = await _recovering_workspace(
        runtime_manager=runtime_manager
    )
    workspace.release_wins = False

    outcome = await service.wake_workspace(_user("owner-1"), assistant_id)

    assert outcome["state"] == "RECOVERY_REQUIRED"
    assert outcome["retryable"] is True
    assert len(workspace.released) == 1
    assert kernel.created == []


async def test_wake_rebuilds_once_recovery_confirmed_the_destruction() -> None:
    """RECOVERY_REQUIRED with the pointer cleared is rebuild-ready —
    wake proceeds through the normal MATERIALIZING bootstrap."""
    kernel = _FakeSessionKernel()
    service, catalog, workspace = _service()
    service._session_kernel = kernel  # type: ignore[attr-defined]
    created = await service.create_assistant(
        _user("owner-1"), {"display_name": "Jarvis", "environment_name": "env-1"}
    )
    workspace.workspace = {
        "assistant_id": created["assistant_id"],
        "engine_kind": "assistant",
        "state": "RECOVERY_REQUIRED",
        "current_sandbox_id": None,
    }

    outcome = await service.wake_workspace(_user("owner-1"), created["assistant_id"])

    assert outcome["state"] == "MATERIALIZING"
    assert len(kernel.created) == 1
    (transition,) = workspace.transitions
    assert transition["expected_state"] == "RECOVERY_REQUIRED"
    assert transition["new_state"] == "MATERIALIZING"
