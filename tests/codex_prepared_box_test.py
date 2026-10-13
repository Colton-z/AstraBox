"""Codex prepared boxes: spawn fingerprint contract and the claim path.

The fingerprint tests pin WHICH facts are box-frozen: the create fixes the
image, entrypoint, catalog and gateway allowlist, while model, instructions,
sandbox mode, approval policy and collaboration mode ride ``thread/start`` at
claim — an input added to the wrong side surfaces here before it either
retires warm boxes needlessly or serves stale ones.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent import prepared_boxes as pb
from astrabox.core.service.orchestrator.engine import codex, provisioning, startup


def _template(**overrides: Any) -> Any:
    base = dict(
        agent_id="agent-1",
        engine_kind="codex",
        agent_prewarm_fingerprint="fp-1",
        runtime_generation="fp-1",
        prepared_capacity=1,
        model_config={},
        system="Be terse.",
        engine_options={"model_catalog": {"models": []}},
        runtime_template_name="astrabox/sandbox-codex:latest",
        sandbox_backend="open_sandbox",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Manager:
    def __init__(self) -> None:
        self.allocations: list[tuple[str, Any]] = []

    def resolve_model_access(self, model_config: Any) -> Any:
        return SimpleNamespace(
            base_url="https://gw.test",
            credential="sk-shared",
            credential_kind="bearer",
            model_name="gpt-5.3-codex",
        )

    async def record_startup_allocation(self, session_id: str, allocation: Any) -> None:
        self.allocations.append((session_id, allocation))

    async def resolve_session_egress_credentials(self, session_id: str, **kwargs: Any) -> list[Any]:
        return []

    @property
    def deployment_settings(self) -> Any:
        return SimpleNamespace(mcp_proxy_base_url="http://backend.codex.test:8000")


def _plan(resume: str | None = None) -> Any:
    return SimpleNamespace(
        resume_engine_session_key=resume,
        cwd="/workspace",
        subject_kind="deployment_conversation",
        agent_id="agent-1",
        assistant_id=None,
        user_id=None,
        engine_kind="codex",
    )


@pytest.fixture(autouse=True)
def _platform_claim_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _mounts(**_kwargs: Any) -> tuple[tuple[str, str], ...]:
        return ()

    async def _ready(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(provisioning, "plan_workspace_mounts", _mounts)
    monkeypatch.setattr(provisioning, "workspace_is_ready", _ready)


# ── spawn fingerprint ─────────────────────────────────────────────────────


def test_spawn_fingerprint_is_stable_for_the_same_frozen_facts() -> None:
    a = codex._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", catalog='{"models": []}'
    )
    b = codex._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", catalog='{"models": []}'
    )
    assert a == b


@pytest.mark.parametrize(
    ("kwargs", "template_overrides"),
    [
        ({"catalog": '{"models": ["other"]}'}, {}),
        ({"base_url": "https://other-gw.test"}, {}),
        ({}, {"runtime_template_name": "astrabox/sandbox-codex:next"}),
    ],
)
def test_spawn_fingerprint_retires_a_box_when_a_frozen_fact_changes(
    kwargs: dict[str, Any], template_overrides: dict[str, Any]
) -> None:
    base = codex._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", catalog='{"models": []}'
    )
    changed = codex._slot_spawn_fingerprint(
        _template(**template_overrides),
        base_url=kwargs.get("base_url", "https://gw.test"),
        catalog=kwargs.get("catalog", '{"models": []}'),
    )
    assert changed != base


def test_thread_borne_facts_do_not_touch_the_fingerprint() -> None:
    base = codex._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", catalog='{"models": []}'
    )
    changed = codex._slot_spawn_fingerprint(
        _template(
            system="Entirely different instructions.",
            engine_options={
                "model_catalog": {"models": []},
                "config": {"approval_policy": "never"},
                "turn_start": {
                    "collaborationMode": {
                        "mode": "plan",
                        "settings": {
                            "reasoning_effort": None,
                            "developer_instructions": None,
                        },
                    }
                },
            },
            model_config={"model": "another-model"},
        ),
        base_url="https://gw.test",
        catalog='{"models": []}',
    )
    # They ride thread/start onto the live runtime; retiring a warm box over
    # them would rebuild boxes for a change activation already carries.
    assert changed == base


# ── claim gates ───────────────────────────────────────────────────────────


def test_a_stale_spawn_fingerprint_discards_the_claimed_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = {
        "slot_id": "slot-abc",
        "state": "claimed",
        "placement": pb.PLACEMENT_CONVERSATION_BOX,
        "engine_initialized": True,
        "sandbox_id": "box-77",
        "spawn_fingerprint": "spawn-of-an-older-catalog",
        "gateway_substitution": True,
        "runtime_identity": {"workspace_dir": "/workspace"},
    }

    discards: list[str] = []

    async def _discard(template: Any, manifest: dict[str, Any], *, reason: str, **_kwargs: Any) -> None:
        discards.append(reason)

    provisioned = provisioning.ProvisionedEngineSandbox(
        sandbox=SimpleNamespace(sandbox_id="box-77"),
        sandbox_id="box-77",
        runtime_identity={"workspace_dir": "/workspace"},
        cwd="/workspace",
        model_credential="placeholder",
        prepared_manifest=claimed,
    )

    async def _provision(*_args: Any, **_kwargs: Any) -> Any:
        return provisioned

    monkeypatch.setattr(startup, "provision_engine_sandbox", _provision)
    monkeypatch.setattr(startup, "_discard_claimed_unit", _discard)
    with pytest.raises(APIError, match="prepared Codex box") as caught:
        asyncio.run(
            startup.start_platform_runtime(
                _Manager(),
                codex.CodexEngineAdapter(),
                session_id="session-1",
                assignment_id="assignment-1",
                template=_template(),
                workspace_plan=_plan(),
                user_id=None,
                permission_mode=None,
                progress_callback=None,
                callback_url=None,
            )
        )
    assert caught.value.code == "AGENT_PREWARM_CONFIG_INVALID"
    assert len(discards) == 1 and caught.value.code in discards[0]
