"""Options a host sends cannot take over what the runner and the platform own.

Native SDK JSON cannot replace the platform's session identity, the runner
refuses runner-controlled option keys, and the resume store accepts no write
before an activation that names its own session.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.claude_code_config import (
    apply_claude_engine_options,
)
from astrabox.core.service.orchestrator.sandbox_runner import (
    RunnerProtocolError,
    _DeferredHttpStoreTarget,
    _HttpStoreTarget,
    _real_session_factory,
)


def test_native_json_cannot_replace_platform_session_identity() -> None:
    with pytest.raises(APIError, match="platform-managed"):
        apply_claude_engine_options(
            {}, SimpleNamespace(engine_options={"sdk_options": {"session_id": "other"}})
        )


class _FakeLink:
    def is_connected(self) -> bool:
        return True

    async def send(self, frame: dict[str, Any]) -> bool:
        return True


def _opening(options: dict[str, Any]) -> dict[str, Any]:
    return {
        "slot_id": "slot-1",
        "options": options,
    }


async def test_resume_store_refuses_writes_until_matching_activation(
    monkeypatch: Any,
) -> None:
    requests: list[str] = []

    async def post(
        _target: _HttpStoreTarget,
        path: str,
        _payload: dict[str, Any],
    ) -> dict[str, Any]:
        requests.append(path)
        if path.endswith("/load"):
            return {
                "data": {
                    "entries": [{"type": "user", "uuid": "durable-entry"}],
                    "store_sequence": 7,
                }
            }
        return {"data": {"store_sequence": 8}}

    monkeypatch.setattr(_HttpStoreTarget, "_post", post)
    target = _DeferredHttpStoreTarget()
    store = {"base_url": "http://platform.internal"}
    key = {"project_key": "-work", "session_id": "sdk-session"}
    target.bind_resume_source("platform-session", store)

    assert await target.load(key) == [
        {"type": "user", "uuid": "durable-entry"}
    ]
    with pytest.raises(
        RunnerProtocolError,
        match="cannot flush before Session activation",
    ):
        await target.flush(key, [{"type": "assistant"}], "append-1")
    with pytest.raises(
        RunnerProtocolError,
        match="does not match resume source",
    ):
        target.activate("different-session", store)

    target.activate("platform-session", store)
    await target.flush(key, [{"type": "assistant"}], "append-1")

    assert requests == [
        "/api/v1/transcript/platform-session/load",
        "/api/v1/transcript/platform-session/append",
    ]


@pytest.mark.parametrize(
    "key",
    [
        # Exact durable resume is the platform contract. Letting a host ask
        # Claude for whichever conversation is "most recent" can bind the
        # wrong user's context after a runtime replacement.
        "continue_conversation",
        "hooks",
        "session_store",
        "user",
    ],
)
def test_runner_refuses_runner_controlled_option_keys(key: str) -> None:
    with pytest.raises(RunnerProtocolError, match="runner-controlled keys"):
        _real_session_factory(
            _opening({"cwd": "/w", key: "nope"}),
            _FakeLink(),
        )
