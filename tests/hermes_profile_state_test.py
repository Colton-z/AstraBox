"""A Hermes profile file deleted after it was listed is absent from the profile archive.

The image's state mirror lists the profile's files and then reads them, while
Hermes keeps running. SQLite deletes a store's WAL when its last connection
checkpoints it, and Hermes deletes files of its own, so a listed file can be
gone when it is read. Treated as an I/O failure, it fails the whole native
state save, and a new Assistant box whose first save fails is replaced. A
deletion is a change of state, so the fingerprint records the file as absent,
and the archive restores without it.

``hermes_cli`` is the Hermes package inside the image; its SQLite backup
helpers are replaced with plain copies, which is all this race involves.
"""

from __future__ import annotations

import importlib
import io
import shutil
import sys
import tarfile
import types
from pathlib import Path
from types import ModuleType

import pytest

_IMAGE = Path(__file__).resolve().parents[1] / "containers" / "sandbox-hermes"
#: Files that disappear right after a check finds them.
_VANISHING = {"kanban.db-wal", "jobs.json"}


def _copy(source: Path, target: Path) -> bool:
    shutil.copyfile(source, target)
    return True


def _profile_state(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    backup = types.ModuleType("hermes_cli.backup")
    backup._should_exclude = lambda _path: False  # type: ignore[attr-defined]
    backup.verify_sqlite_integrity = lambda _path, max_bytes=0: {"valid": True}  # type: ignore[attr-defined]
    copying = types.ModuleType("hermes_cli.backup_sqlite")
    copying._safe_copy_db = _copy  # type: ignore[attr-defined]
    restoring = types.ModuleType("hermes_cli.backup_restore")
    restoring._safe_restore_db = _copy  # type: ignore[attr-defined]
    for name, module in {
        "hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.backup": backup,
        "hermes_cli.backup_sqlite": copying, "hermes_cli.backup_restore": restoring,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.syspath_prepend(str(_IMAGE))
    monkeypatch.delitem(sys.modules, "hermes_profile_state", raising=False)
    return importlib.import_module("hermes_profile_state")


def _home(root: Path) -> Path:
    for name, data in {
        "memories/MEMORY.md": b"kept", "memories/gone.md": b"deleted while packing",
        "cron/jobs.json": b"{}", "kanban.db": b"store", "kanban.db-wal": b"wal",
    }.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return root


def _vanish_after_check(monkeypatch: pytest.MonkeyPatch) -> None:
    checked = Path.is_file

    def is_file(self: Path) -> bool:
        present = checked(self)
        if present and self.name in _VANISHING:
            self.unlink()
        return present

    monkeypatch.setattr(Path, "is_file", is_file)


def test_a_profile_file_deleted_after_it_was_listed_is_absent_not_a_failed_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _profile_state(monkeypatch)
    home = _home(tmp_path / "home")
    _vanish_after_check(monkeypatch)

    fingerprint = state.stamp(home)
    assert ("kanban.db-wal", None) in fingerprint
    assert not (home / "kanban.db-wal").exists()

    (home / "cron/jobs.json").write_bytes(b"{}")
    listed = state._regular_files

    def listing_then_deletion(directory: Path) -> list[Path]:
        files = listed(directory)
        for path in files:
            if path.name == "gone.md":
                path.unlink()
        return files

    monkeypatch.setattr(state, "_regular_files", listing_then_deletion)
    payload = state.pack(home)

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        names = set(archive.getnames())
    assert names == {"memories/MEMORY.md", "kanban.db", state.MANIFEST_NAME}

    restored = tmp_path / "restored"
    restored.mkdir()
    state.restore(payload, restored)
    assert (restored / "memories/MEMORY.md").read_bytes() == b"kept"
    assert (restored / "kanban.db").read_bytes() == b"store"
    assert not (restored / "memories/gone.md").exists()
    assert not (restored / "cron/jobs.json").exists()
