from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e import _engine_profile as profiles


def _evidence(
    path: Path,
    *,
    state: str = "CONFIGURED",
) -> Path:
    example = {
        "agent_id": "agent-1",
        "agent_name": "Investment Research (example)",
        "child_completion": {
            "engine_reasons": [],
            "engine_statuses": [],
        },
        "contracts": {
            "permission_modes": True,
            "background_subagent": False,
        },
        "decisions": {"approve": "accept"},
        "engine_kind": "example",
        "environment_name": "environment-1",
        "image": "registry.example/sandbox-example:sha",
        "instructions": {"controllable_child": "in the background"},
        "mirror_entry_types": ["message"],
        "model": "model-1",
        "modes": {"unattended": "unrestricted"},
        "presentation": "decision",
        "tools": {"write": ["edit", "write"]},
    }
    other = {
        **example,
        "agent_id": "agent-2",
        "agent_name": "Investment Research (other)",
        "engine_kind": "other",
        "environment_name": "environment-2",
        "image": "registry.example/sandbox-other:sha",
    }
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "state": state,
                "profiles": [example, other],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _clear_profile_cache() -> Iterator[None]:
    profiles.current_profile.cache_clear()
    yield
    profiles.current_profile.cache_clear()


def test_profile_reads_only_the_selected_configured_deployment_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _evidence(tmp_path / "matrix.json")
    monkeypatch.setenv(profiles.MATRIX_EVIDENCE_ENV, str(evidence))
    monkeypatch.setenv(profiles.ENGINE_KIND_ENV, "example")

    assert profiles.engine_kind() == "example"
    assert profiles.agent_id() == "agent-1"
    assert profiles.permission_mode("unattended") == "unrestricted"
    assert profiles.permission_mode("plan") is None
    assert profiles.tool_names("write") == ("edit", "write")
    assert profiles.contract_supported("background_subagent") is False
    assert profiles.child_completion() == ((), ())
    assert profiles.instruction("controllable_child") == "in the background"


@pytest.mark.parametrize("state", ["FAIL", "", "READY", "PASS", "PARTIALLY_PROBED"])
def test_profile_rejects_evidence_that_is_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    evidence = _evidence(tmp_path / "matrix.json", state=state)
    monkeypatch.setenv(profiles.MATRIX_EVIDENCE_ENV, str(evidence))
    monkeypatch.setenv(profiles.ENGINE_KIND_ENV, "example")

    with pytest.raises(RuntimeError, match="not CONFIGURED"):
        profiles.current_profile()


def test_profile_refuses_a_symlinked_evidence_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _evidence(tmp_path / "matrix.json")
    link = tmp_path / "matrix-link.json"
    link.symlink_to(target)
    monkeypatch.setenv(profiles.MATRIX_EVIDENCE_ENV, str(link))
    monkeypatch.setenv(profiles.ENGINE_KIND_ENV, "example")

    with pytest.raises(RuntimeError, match="absolute regular evidence file"):
        profiles.current_profile()
