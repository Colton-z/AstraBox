from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox import cli
from astrabox.cli import serve as cli_serve
from astrabox.deploy import opensandbox_snapshot_check as snapshot_check


class _Files:
    def __init__(self, scene: dict[str, Any]) -> None:
        self.scene = scene

    async def write_file(
        self,
        path: str,
        data: str,
        *,
        mode: int,
    ) -> None:
        self.scene["written"] = (path, data, mode)

    async def read_file(self, path: str) -> str:
        assert path == self.scene["written"][0]
        return str(self.scene["written"][1])


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    scene: dict[str, Any] = {
        "closed": 0,
        "killed": [],
        "states": ["Paused", "Running"],
    }

    class FakeSandbox:
        def __init__(self) -> None:
            self.id = "sandbox-check"
            self.files = _Files(scene)

        @classmethod
        async def create(cls, **kwargs: Any) -> "FakeSandbox":
            scene["create"] = kwargs
            return cls()

        @classmethod
        async def connect(cls, sandbox_id: str, **kwargs: Any) -> "FakeSandbox":
            scene["connect"] = (sandbox_id, kwargs)
            return cls()

        async def pause(self) -> None:
            scene["paused"] = True

        async def close(self) -> None:
            scene["closed"] += 1

    class FakeManager:
        @classmethod
        async def create(cls, **kwargs: Any) -> "FakeManager":
            scene.setdefault("manager_configs", []).append(kwargs)
            return cls()

        async def get_sandbox_info(self, sandbox_id: str) -> SimpleNamespace:
            assert sandbox_id == "sandbox-check"
            state = scene["states"].pop(0)
            return SimpleNamespace(
                status=SimpleNamespace(state=state, reason=None, message=None)
            )

        async def kill_sandbox(self, sandbox_id: str) -> None:
            scene["killed"].append(sandbox_id)

        async def resume_sandbox(self, sandbox_id: str) -> None:
            scene["resume"] = sandbox_id

        async def close(self) -> None:
            scene["manager_closed"] = scene.get("manager_closed", 0) + 1

    connection = SimpleNamespace(get_api_key=lambda: "")
    settings = SimpleNamespace(sandbox_ready_timeout_seconds=120)
    monkeypatch.setattr(snapshot_check, "Sandbox", FakeSandbox)
    monkeypatch.setattr(snapshot_check, "SandboxManager", FakeManager)
    monkeypatch.setattr(snapshot_check, "load_astrabox_settings", lambda: settings)
    monkeypatch.setattr(
        snapshot_check._config,
        "sdk_connection_config",
        lambda _settings, **_kwargs: connection,
    )
    return scene


async def test_snapshot_check_uses_the_sdk_mode_and_removes_the_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene = _install_fake_sdk(monkeypatch)

    result = await snapshot_check.verify_opensandbox_snapshots(
        image="registry.test/agent:commit",
        timeout_seconds=180,
    )

    assert result["state"] == "PASS"
    assert result["sandbox_id"] == "sandbox-check"
    assert result["image"] == "registry.test/agent:commit"
    assert set(result["timings_seconds"]) == {
        "create",
        "pause",
        "read",
        "resume",
        "write",
    }
    assert scene["create"]["entrypoint"] == ["/opt/astrabox/boot.sh"]
    # The OpenSandbox API expects chmod-style octal digits, not Python's
    # integer value for an octal literal (0o600 == 384).
    assert scene["written"][2] == 600
    assert scene["paused"] is True
    assert scene["resume"] == "sandbox-check"
    assert scene["connect"][0] == "sandbox-check"
    assert scene["killed"] == ["sandbox-check"]


async def test_snapshot_check_can_target_the_bundled_lifecycle_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene = _install_fake_sdk(monkeypatch)

    def connection_config(_settings: object, **kwargs: object) -> object:
        scene["connection_kwargs"] = kwargs
        return SimpleNamespace(get_api_key=lambda: "")

    monkeypatch.setattr(snapshot_check._config, "sdk_connection_config", connection_config)

    await snapshot_check.verify_opensandbox_snapshots(
        image="agent:test",
        lifecycle_base_url="http://127.0.0.1:8990",
    )

    assert scene["connection_kwargs"] == {
        "lifecycle_base_url_override": "http://127.0.0.1:8990"
    }


async def test_snapshot_check_reports_the_failing_lifecycle_stage_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene = _install_fake_sdk(monkeypatch)
    scene["states"] = ["Failed"]

    with pytest.raises(
        snapshot_check.SnapshotVerificationError,
        match="reached Failed while waiting for Paused",
    ):
        await snapshot_check.verify_opensandbox_snapshots(
            image="registry.test/agent:commit",
            timeout_seconds=180,
        )

    assert scene["killed"] == ["sandbox-check"]
    assert "resume" not in scene


@pytest.mark.parametrize("timeout", [0, 721])
async def test_snapshot_check_refuses_a_deadline_outside_the_product_budget(
    timeout: int,
) -> None:
    with pytest.raises(ValueError, match="between 1 and 720"):
        await snapshot_check.verify_opensandbox_snapshots(timeout_seconds=timeout)


def test_snapshot_cli_writes_atomic_non_secret_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "evidence" / "snapshot.json"

    async def fake_verify(**kwargs: Any) -> dict[str, Any]:
        assert kwargs == {
            "image": "agent:test",
            "lifecycle_base_url": "http://127.0.0.1:8990",
            "timeout_seconds": 90,
        }
        return {"image": "agent:test", "state": "PASS"}

    monkeypatch.setattr(snapshot_check, "verify_opensandbox_snapshots", fake_verify)
    args = argparse.Namespace(
        image="agent:test",
        lifecycle_base_url="http://127.0.0.1:8990",
        timeout_seconds=90,
        json_out=output,
    )

    assert cli_serve._cmd_verify_opensandbox_snapshots(args) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "image": "agent:test",
        "state": "PASS",
    }
    assert not output.with_name(f".{output.name}.tmp").exists()


def test_snapshot_cli_parses_an_explicit_lifecycle_url() -> None:
    args = cli._build_parser().parse_args(
        [
            "verify-opensandbox-snapshots",
            "--lifecycle-base-url",
            "http://127.0.0.1:8990",
        ]
    )

    assert args.lifecycle_base_url == "http://127.0.0.1:8990"
    assert args.timeout_seconds == snapshot_check.MAX_VERIFICATION_SECONDS
