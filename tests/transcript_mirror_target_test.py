"""Deferred transcript-mirror target: the claim writes, the box adopts.

The in-box reader is fed the HOST writer's own serialization
(``transcript_mirror.mirror_target_payload``), never a hand-written imitation
of it — a payload the producer does not emit proves nothing about the pair.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine import transcript_mirror

_MIRROR_PATH = (
    Path(__file__).resolve().parents[1]
    / "astrabox"
    / "core"
    / "service"
    / "orchestrator"
    / "runtime"
    / "astrabox-transcript-mirror"
)


def _load_mirror(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> Any:
    """Import the in-box program fresh under exactly this environment."""

    for name in (
        "_ASTRABOX_TRANSCRIPT_BACKEND_BASE_URL",
        "_ASTRABOX_PLATFORM_SESSION_ID",
        "_ASTRABOX_TRANSCRIPT_PROJECT_KEY",
        "ASTRABOX_TRANSCRIPT_MIRROR_TARGET_FILE",
        "ASTRABOX_TRANSCRIPT_MIRROR_TARGET_GRACE_SECONDS",
        "ASTRABOX_TRANSCRIPT_MIRROR_ROOT",
        "ASTRABOX_TRANSCRIPT_MIRROR_GLOB",
        "ASTRABOX_TRANSCRIPT_MIRROR_NAMESPACE",
        "ASTRABOX_TRANSCRIPT_MIRROR_STATE_DIR",
        "ASTRABOX_TRANSCRIPT_MIRROR_POLL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    # The program ships without a .py suffix (it is an executable in the
    # image), so the loader has to be named: importlib infers one from the
    # extension and returns no spec at all for this path.
    spec = importlib.util.spec_from_loader(
        "astrabox_transcript_mirror_under_test",
        importlib.machinery.SourceFileLoader(
            "astrabox_transcript_mirror_under_test", str(_MIRROR_PATH)
        ),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop("astrabox_transcript_mirror_under_test", None)
    spec.loader.exec_module(module)
    return module


def _image_env(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "sessions"
    root.mkdir(exist_ok=True)
    return {
        "ASTRABOX_TRANSCRIPT_MIRROR_ROOT": str(root),
        "ASTRABOX_TRANSCRIPT_MIRROR_GLOB": "*.jsonl",
        "ASTRABOX_TRANSCRIPT_MIRROR_NAMESPACE": "codex/",
        "ASTRABOX_TRANSCRIPT_MIRROR_STATE_DIR": str(tmp_path / "state"),
        "ASTRABOX_TRANSCRIPT_MIRROR_POLL_SECONDS": "0.01",
    }


class _Manager:
    deployment_settings = SimpleNamespace(
        mcp_proxy_base_url="http://backend.mirror.test:8000"
    )


# ── configuration gates ───────────────────────────────────────────────────


def test_deferred_and_per_session_targets_together_are_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_mirror(
        monkeypatch,
        {
            **_image_env(tmp_path),
            "ASTRABOX_TRANSCRIPT_MIRROR_TARGET_FILE": str(tmp_path / "target.json"),
            "_ASTRABOX_PLATFORM_SESSION_ID": "session-1",
        },
    )
    assert module.main() == 78


def test_env_mode_still_refuses_missing_session_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The cold path's contract is unchanged: without a deferred target file,
    # the three per-session values remain required.
    module = _load_mirror(monkeypatch, _image_env(tmp_path))
    assert module.main() == 78


# ── the producer/consumer pair ────────────────────────────────────────────


def test_reader_adopts_exactly_what_the_claim_writer_produces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_CAPABILITY_REQUIRED", "false")
    payload = transcript_mirror.mirror_target_payload(
        _Manager(), "session-42", cwd="/workspace"
    )
    target = tmp_path / "target.json"
    target.write_bytes(payload)
    module = _load_mirror(
        monkeypatch,
        {
            **_image_env(tmp_path),
            "ASTRABOX_TRANSCRIPT_MIRROR_TARGET_FILE": str(target),
        },
    )
    assert module.read_deferred_target(target) == (
        "http://backend.mirror.test:8000",
        "session-42",
        "/workspace",
    )


def test_a_capability_gated_store_url_survives_the_file_hop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_CAPABILITY_REQUIRED", "true")
    # Local mode lets the capability fence sign with its dev key; the test
    # cares about URL shape, not the trust root.
    monkeypatch.setenv("ASTRABOX_LOCAL_MODE", "1")
    payload = transcript_mirror.mirror_target_payload(
        _Manager(), "session-42", cwd="/workspace"
    )
    target = tmp_path / "target.json"
    target.write_bytes(payload)
    module = _load_mirror(
        monkeypatch,
        {
            **_image_env(tmp_path),
            "ASTRABOX_TRANSCRIPT_MIRROR_TARGET_FILE": str(target),
        },
    )
    resolved = module.read_deferred_target(target)
    assert resolved is not None
    base_url, session_id, project_key = resolved
    # The capability token is part of the base URL, exactly as the env-mode
    # delivery carries it; the reader must not need to know it is there.
    assert base_url.startswith("http://backend.mirror.test:8000/")
    assert base_url != "http://backend.mirror.test:8000"
    assert (session_id, project_key) == ("session-42", "/workspace")


def test_an_incomplete_target_is_not_yet_a_target(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    spec = importlib.util.spec_from_loader(
        "mirror_reader_probe",
        importlib.machinery.SourceFileLoader(
            "mirror_reader_probe", str(_MIRROR_PATH)
        ),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.read_deferred_target(target) is None  # missing
    target.write_text("{not json")
    assert module.read_deferred_target(target) is None  # torn write
    target.write_text('{"base_url": "http://x", "session_id": ""}')
    assert module.read_deferred_target(target) is None  # incomplete


# ── the fail-loud bound ───────────────────────────────────────────────────


def test_a_session_log_with_no_target_turns_fatal_past_the_grace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _image_env(tmp_path)
    (tmp_path / "sessions" / "rollout-1.jsonl").write_text('{"item":1}\n')
    module = _load_mirror(
        monkeypatch,
        {
            **env,
            "ASTRABOX_TRANSCRIPT_MIRROR_TARGET_FILE": str(tmp_path / "target.json"),
            "ASTRABOX_TRANSCRIPT_MIRROR_TARGET_GRACE_SECONDS": "0.05",
        },
    )
    assert module.await_deferred_target() == 78


def test_an_unusable_target_file_turns_fatal_past_the_grace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "target.json"
    target.write_text("{never valid")
    module = _load_mirror(
        monkeypatch,
        {
            **_image_env(tmp_path),
            "ASTRABOX_TRANSCRIPT_MIRROR_TARGET_FILE": str(target),
            "ASTRABOX_TRANSCRIPT_MIRROR_TARGET_GRACE_SECONDS": "0.05",
        },
    )
    assert module.await_deferred_target() == 78


def test_an_idle_unclaimed_box_waits_without_a_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No session log and no target file is the healthy pre-claim state.

    The wait must survive many grace windows untriggered — a mirror that
    turned fatal on an idle box would kill every prepared box before its
    claim arrived.
    """

    target = tmp_path / "target.json"
    module = _load_mirror(
        monkeypatch,
        {
            **_image_env(tmp_path),
            "ASTRABOX_TRANSCRIPT_MIRROR_TARGET_FILE": str(target),
            "ASTRABOX_TRANSCRIPT_MIRROR_TARGET_GRACE_SECONDS": "0.02",
        },
    )

    calls = {"n": 0}
    real_sleep = module.time.sleep

    def _sleep(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 20:
            # Long past several grace windows: still waiting, so end the loop
            # by supplying the target the claim would have written.
            monkeypatch.setenv("ASTRABOX_TRANSCRIPT_CAPABILITY_REQUIRED", "false")
            target.write_bytes(
                transcript_mirror.mirror_target_payload(
                    _Manager(), "session-7", cwd="/workspace"
                )
            )
        real_sleep(min(seconds, 0.001))

    monkeypatch.setattr(module.time, "sleep", _sleep)
    assert module.await_deferred_target() == (
        "http://backend.mirror.test:8000",
        "session-7",
        "/workspace",
    )

def test_an_unclaimed_prepared_box_waits_past_the_ordinary_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prepared unit's own conversation file is not undeliverable work.

    A prepared unit that holds a started engine writes its session file at
    once, and nothing can bind a destination until a Session claims the unit.
    Read as orphaned work, that file kills the mirror two minutes later — which
    is why a prepared unit could not hold a started engine at all. The platform
    states how long being unclaimed is legitimate; the mirror honours it, and
    goes back to failing loudly the moment that deadline passes.
    """

    from astrabox.core.service.orchestrator.engine.transcript_mirror import (
        unclaimed_marker_payload,
    )

    env = _image_env(tmp_path)
    target = tmp_path / "target.json"
    module = _load_mirror(
        monkeypatch,
        {**env, "ASTRABOX_TRANSCRIPT_MIRROR_TARGET_FILE": str(target)},
    )
    root = Path(env["ASTRABOX_TRANSCRIPT_MIRROR_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "live.jsonl").write_text('{"type":"x"}\n')

    # Written by the producer, not imitated here.
    target.write_bytes(unclaimed_marker_payload(timedelta(minutes=30)))
    assert module.read_deferred_target(target) is None, "not a usable target yet"
    remaining = module.read_unclaimed_deadline(target)
    assert remaining is not None and remaining > 0

    expired = tmp_path / "expired.json"
    expired.write_bytes(unclaimed_marker_payload(timedelta(seconds=-5)))
    overdue = module.read_unclaimed_deadline(expired)
    assert overdue is not None and overdue < 0, (
        "a deadline in the past must read as overdue, not as permission to wait"
    )
