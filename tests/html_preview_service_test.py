from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from astrabox.core.service.orchestrator.html_preview_service import HtmlPreviewService


_VISIBLE_ROOT = "/workspace"
_SOURCE_ROOT = "/home/conversations/conv_one/workspace"


class _CommandRunner:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command: str) -> Any:
        self.commands.append(command)
        payload = {
            "ok": True,
            "resolved_path": f"{_SOURCE_ROOT}/site/index.html",
        }
        return SimpleNamespace(
            error=None,
            logs=SimpleNamespace(
                stdout=[SimpleNamespace(text=json.dumps(payload))],
                stderr=[],
            ),
        )


class _Artifacts:
    def __init__(self) -> None:
        self.artifact: dict[str, Any] | None = None

    async def upsert_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        self.artifact = dict(artifact)
        return dict(artifact)

    async def get_artifact(
        self, session_id: str, artifact_id: str
    ) -> dict[str, Any] | None:
        if not self.artifact:
            return None
        if self.artifact["session_id"] != session_id:
            return None
        if self.artifact["artifact_id"] != artifact_id:
            return None
        return dict(self.artifact)


class _RuntimeManager:
    async def resolve_enhanced_server_endpoint(
        self, *, sandbox_id: str, port: int
    ) -> str:
        assert sandbox_id == "box-one"
        assert port == 8080
        return "preview.example.test"


class _Service(HtmlPreviewService):
    def __init__(self, *, runner: _CommandRunner, artifacts: _Artifacts) -> None:
        super().__init__(
            sessions_repo=object(),
            session_snapshots_repo=object(),
            artifacts_repo=artifacts,
            runtime_manager=_RuntimeManager(),
            binding_repo=object(),
        )
        self._runner = runner
        self.mapped_source_dirs: list[str] = []

    async def _resolve_binding_context(self, deployment_id: str) -> dict[str, Any]:
        assert deployment_id == "session-one"
        return {
            "deployment_id": deployment_id,
            "scope_kind": "session",
            "session_id": deployment_id,
            "user_id": "user-one",
            "sandbox_id": "box-one",
            "sandbox": SimpleNamespace(commands=self._runner),
            "root_path": _VISIBLE_ROOT,
            "source_root_path": _SOURCE_ROOT,
            "runtime_identity": {
                "linux_user": "conv_one",
                "home_dir": "/home/conversations/conv_one",
                "workspace_dir": _VISIBLE_ROOT,
                "workspace_source_dir": _SOURCE_ROOT,
                "file_root_dir": _VISIBLE_ROOT,
                "file_root_source_dir": _SOURCE_ROOT,
                "sandbox_tenancy": "agent",
            },
        }

    async def _install_nginx_preview_mapping(
        self,
        command_runner: Any,
        *,
        source_dir: str,
        preview_id: str,
    ) -> None:
        assert command_runner is self._runner
        assert len(preview_id) == 24
        self.mapped_source_dirs.append(source_dir)


async def test_shared_preview_keeps_public_paths_visible_and_uses_private_backing() -> None:
    runner = _CommandRunner()
    artifacts = _Artifacts()
    service = _Service(runner=runner, artifacts=artifacts)

    published = await service.publish_html_preview(
        deployment_id="session-one",
        path="/workspace/site/index.html",
    )

    assert published["path"] == "/workspace/site/index.html"
    assert artifacts.artifact is not None
    assert artifacts.artifact["source_dir"] == "/workspace/site"
    assert service.mapped_source_dirs == [f"{_SOURCE_ROOT}/site"]
    assert _SOURCE_ROOT in runner.commands[0]

    redirect = await service.resolve_preview_redirect(
        deployment_id="session-one",
        preview_id=published["preview_id"],
        asset_path="assets/app.js",
    )

    assert redirect == (
        "https://preview.example.test/astrabox-preview/"
        f"{published['preview_id']}/assets/app.js"
    )
    assert service.mapped_source_dirs == [
        f"{_SOURCE_ROOT}/site",
        f"{_SOURCE_ROOT}/site",
    ]
