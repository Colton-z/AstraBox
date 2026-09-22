"""The in-box control server prepares the directory the CLI spawns into.

The preparation exists because the host cannot do it for every box. A box the
host BUILT can be reached over its exec daemon; a box taken from a prewarm pool
was created before any session claimed it and carries no exec daemon at all — so
a host-side repair works on one kind of box and fails on the other, which is a
failure that only appears once pooling is switched on. The process that owns the
spawn is inside every box, so it does it there.

Ownership follows one rule: this process owns what it CREATES, and nothing else.
A directory it makes belongs to root, which the CLI's user could not write, so it
is handed over. A directory that already exists is left alone — it is either the
image's baked workspace, already owned by that user, or a mount, whose
permissions belong to whoever mounted it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from astrabox.core.service.orchestrator.sandbox_runner import (
    _ensure_working_directory,
)


class _Options:
    def __init__(self, cwd: str | None, env: dict[str, str] | None = None) -> None:
        self.cwd = cwd
        self.env = env or {}


def test_the_working_directory_is_created(tmp_path: Path) -> None:
    target = tmp_path / "workspace"
    _ensure_working_directory(_Options(str(target)))
    assert target.is_dir()


def test_a_directory_that_is_already_there_is_left_alone(tmp_path: Path) -> None:
    """The image bakes the workspace, so this is the normal path — and a no-op.

    It must not be a chown: the same branch is reached when a deployment has
    mounted durable storage over the workspace, and a mounted tree's ownership
    is the mounting side's to decide, not this box's.
    """
    target = tmp_path / "workspace"
    target.mkdir()
    seen: list[str] = []
    original_chown = os.chown
    try:
        os.chown = lambda *a, **k: seen.append("chown")  # type: ignore[assignment]
        _ensure_working_directory(_Options(str(target), {"USER": "agent"}))
    finally:
        os.chown = original_chown  # type: ignore[assignment]
    assert target.is_dir()
    assert seen == []


def test_no_working_directory_is_asked_for(tmp_path: Path) -> None:
    _ensure_working_directory(_Options(None))
    _ensure_working_directory(_Options("  "))


def test_a_created_directory_is_handed_to_the_cli_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Made by root, so it has to change hands or the CLI cannot write in it."""
    target = tmp_path / "workspace"
    seen: list[tuple[str, int, int]] = []

    class _Entry:
        pw_uid = 4242
        pw_gid = 4243

    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: _Entry() if name == "agent" else None)
    monkeypatch.setattr(
        os, "chown", lambda path, uid, gid: seen.append((str(path), uid, gid))
    )
    _ensure_working_directory(_Options(str(target), {"USER": "agent"}))
    assert seen == [(str(target), 4242, 4243)]


def test_an_unresolvable_user_does_not_fail_the_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best effort on purpose.

    A box whose user does not resolve, or whose server cannot chown, must still
    start: the CLI may not need the ownership at all. The loud failure belongs to
    the first write that genuinely cannot proceed, which names the path it tried.
    """
    target = tmp_path / "workspace"
    import pwd

    def _raise(_name: str) -> Any:
        raise KeyError("no such user")

    monkeypatch.setattr(pwd, "getpwnam", _raise)
    _ensure_working_directory(_Options(str(target), {"USER": "nobody-here"}))
    assert target.is_dir()


def test_no_user_means_no_chown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(os, "chown", lambda *a, **k: called.append("chown"))
    _ensure_working_directory(_Options(str(tmp_path / "workspace"), {}))
    assert called == []
