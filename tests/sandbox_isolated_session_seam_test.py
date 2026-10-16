"""An ``advanced`` permission level needs a live isolation attestation from the backend."""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.seams.sandbox import (
    SandboxProvider,
    register_sandbox,
)


class _Bare(SandboxProvider):
    """A backend with no isolation concept — the default posture.

    The abstract lifecycle pairs are stubbed; nothing here reaches a box.
    """

    name = "bare-iso-test"

    def connection_config(self, **kwargs: Any) -> Any:
        return None

    def secret_material(self, *, settings: Any) -> str:
        return ""

    def build_dataplane(self, **kwargs: Any) -> Any:
        raise RuntimeError("no dataplane")

    async def connect(self, sandbox_id: str) -> Any:
        raise RuntimeError("no connect")

    async def kill(self, sandbox_id: str) -> bool:
        raise RuntimeError("no kill")


def test_advanced_permission_requires_a_live_isolation_attestation() -> None:
    class _Unproved(_Bare):
        name = "unproved-advanced-level"
        supported_permission_levels = ("default", "advanced")

    with pytest.raises(RuntimeError, match="read_isolation_capability"):
        register_sandbox(_Unproved())
