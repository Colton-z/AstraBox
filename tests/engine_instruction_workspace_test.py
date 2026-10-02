"""Native instruction files stay in each conversation's own workspace."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.core.service.orchestrator.engine import deepseek_harness as dsh
from astrabox.core.service.orchestrator.engine import pi, pi_client
from astrabox.core.service.orchestrator.engine.base import (
    EnginePreparationContext,
    EngineStartupContext,
)
from tests.sandbox_script_writer_test import _CommandChannel


@pytest.mark.parametrize("engine", ["pi", "deepseek_harness"])
@pytest.mark.parametrize("prepared", [False, True], ids=["cold", "prepared"])
@pytest.mark.parametrize("tenancy", ["conversation", "agent"])
@pytest.mark.parametrize("subdirectory", ["", "project"])
async def test_instruction_publication_uses_the_owned_workspace_before_vendor_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    engine: str, prepared: bool, tenancy: str, subdirectory: str,
) -> None:
    adapter = pi.PiEngineAdapter() if engine == "pi" else dsh.DeepSeekHarnessEngineAdapter()
    common = tmp_path / "visible-workspace"
    common.mkdir()
    unrelated = common / "AGENTS.md"
    unrelated.write_text("unrelated box content\n")
    installed: list[tuple[Path, str]] = []

    for number in range(2):
        home = tmp_path / f"conversation-{number}"
        source = home / "workspace"
        visible = common if tenancy == "agent" else source
        cwd = visible / subdirectory
        target = source / subdirectory / "AGENTS.md"
        instructions = f"Only conversation {number} receives this instruction. 中文\n"
        identity = {
            "sandbox_tenancy": tenancy, "linux_user": f"conversation{number}",
            "home_dir": str(home), "workspace_dir": str(visible),
            "workspace_source_dir": str(source), "uid": 20000 + number,
        }
        box = _CommandChannel(target)
        native_starts: list[str] = []

        async def vendor_start(*args: Any, **kwargs: Any) -> Any:
            assert target.read_text() == instructions
            assert unrelated.read_text() == "unrelated box content\n"
            for previous, content in installed:
                assert previous.read_text() == content
            native_starts.append("started")
            return SimpleNamespace(close=AsyncMock())

        async def park(*args: Any, **kwargs: Any) -> str:
            assert kwargs["cwd"] == str(cwd)
            await vendor_start()
            return "parked-child"

        async def create(*args: Any, **kwargs: Any) -> str:
            assert kwargs["cwd"] == str(cwd)
            return "native-conversation"

        async def publish(**kwargs: Any) -> Any:
            assert kwargs["terminal_cwd"] == str(cwd)
            return SimpleNamespace()

        monkeypatch.setattr(pi, "_await_rendered_models_config", AsyncMock())
        monkeypatch.setattr(pi, "_park_pi_child", park)
        monkeypatch.setattr(pi_client, "connect_pi_client", vendor_start)
        monkeypatch.setattr(pi, "_publish_runtime", publish)
        monkeypatch.setattr(dsh.DshApiLink, "connect", vendor_start)
        monkeypatch.setattr(dsh, "create_harness_session", create)
        monkeypatch.setattr(dsh, "_publish_runtime", publish)
        common_inputs = dict(
            template=SimpleNamespace(
                system=instructions, engine_options={}, model_config={},
                runtime_template_name=f"test/{engine}:fixed",
            ),
            sandbox=box, sandbox_id="one-box", cwd=str(cwd),
            runtime_identity=identity,
            model_access=SimpleNamespace(base_url="https://model.test/v1", model_name="test-model"),
            model_credential="placeholder",
        )
        if prepared:
            await adapter.prepare_runtime(EnginePreparationContext(
                **common_inputs, slot_id=f"slot-{number}",
                placement="shared_slot" if tenancy == "agent" else "conversation_box",
                runtime_env={}, runner_uri=None, preparation_fingerprint="test-fingerprint",
            ))
        else:
            await adapter.activate_runtime(EngineStartupContext(
                **common_inputs, session_id=f"session-{number}",
                workspace_plan=SimpleNamespace(), resume_session_key=None,
            ))
        assert native_starts == ["started"]
        assert target.read_text() == instructions
        assert target.stat().st_mode & 0o777 == 0o644
        installed.append((target, instructions))

    assert unrelated.read_text() == "unrelated box content\n"
    assert not list(tmp_path.rglob("*.install.lock"))
    assert not list(tmp_path.rglob("*.tmp.*"))
