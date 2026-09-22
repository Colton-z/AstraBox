"""The composition root refuses a NAS setting the selected backend cannot honor.

``ASTRABOX_NAS_ENABLED`` / ``ASTRABOX_NAS_ENDPOINT`` do two things together: they
turn on the runtime NFS mount of the storage tree, and they move the agent's cwd
to that mount's target (``/root/workspace``). Those halves live in different
modules — the mounts short-circuit on the provider's ``uses_create_oss_mounts``
flag, the cwd move reads only settings — so on a create-time-mounting backend the
cwd move survives while the mount does not, and the agent ends up working in an
unmounted, unreadable directory.

That combination is refused at bootstrap rather than logged and ignored, which is
what keeps the pair from being an inert knob. These tests pin the three answers:
refuse on a create-mounting backend, allow on a runtime-mounting one, and stay
out of the way when the pair is unset (the default, and every other test).
"""

from __future__ import annotations

from typing import Any

import pytest

import astrabox.seams.sandbox as sandbox_seam
from astrabox.bootstrap import BootstrapConfigError, _assert_nas_knobs_are_honored
from astrabox.seams.sandbox import SandboxProvider, register_sandbox


class _Provider(SandboxProvider):
    """Every abstract stubbed; only the capability flag matters to this gate."""

    def __init__(self, name: str, *, create_mounts: bool) -> None:
        self.name = name
        self.uses_create_oss_mounts = create_mounts

    def connection_config(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def secret_material(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def build_dataplane(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def connect(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def kill(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _isolated_sandbox_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_seam, "_BACKENDS", dict(sandbox_seam._BACKENDS))


@pytest.fixture(autouse=True)
def _clean_nas_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ASTRABOX_NAS_ENABLED", "ASTRABOX_NAS_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)


def test_create_time_mounting_backend_refuses_the_nas_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_sandbox(_Provider("create-mounts", create_mounts=True))
    monkeypatch.setenv("ASTRABOX_NAS_ENABLED", "true")
    monkeypatch.setenv("ASTRABOX_NAS_ENDPOINT", "nas.example.internal:/export")

    with pytest.raises(BootstrapConfigError) as excinfo:
        _assert_nas_knobs_are_honored("create-mounts")

    message = str(excinfo.value)
    # The operator must learn WHICH backend refused and what to do about it —
    # a bare "invalid configuration" would send them reading source.
    assert "create-mounts" in message
    assert "ASTRABOX_NAS_ENABLED" in message
    assert "/root/workspace" in message


def test_endpoint_alone_is_refused_too(monkeypatch: pytest.MonkeyPatch) -> None:
    # An endpoint set without the enable flag mounts nothing on ANY backend, so
    # it is just as misleading; the gate reads the pair, not the flag alone.
    register_sandbox(_Provider("create-mounts", create_mounts=True))
    monkeypatch.setenv("ASTRABOX_NAS_ENDPOINT", "nas.example.internal:/export")

    with pytest.raises(BootstrapConfigError):
        _assert_nas_knobs_are_honored("create-mounts")


def test_runtime_mounting_backend_accepts_the_nas_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The knobs are not dead: a provider leaving uses_create_oss_mounts at its
    # False default performs the runtime mount, and the cwd move is then correct.
    register_sandbox(_Provider("runtime-mounts", create_mounts=False))
    monkeypatch.setenv("ASTRABOX_NAS_ENABLED", "true")
    monkeypatch.setenv("ASTRABOX_NAS_ENDPOINT", "nas.example.internal:/export")

    _assert_nas_knobs_are_honored("runtime-mounts")


def test_unset_pair_never_resolves_a_backend() -> None:
    # The default path must not gain a provider lookup — an unknown or unset
    # backend name would otherwise start failing at bootstrap for everyone.
    def _explode(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("resolved a provider with the NAS pair unset")

    original = sandbox_seam.sandbox_for_name
    sandbox_seam.sandbox_for_name = _explode  # type: ignore[assignment]
    try:
        _assert_nas_knobs_are_honored("no-such-backend")
    finally:
        sandbox_seam.sandbox_for_name = original  # type: ignore[assignment]
