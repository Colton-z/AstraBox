"""dsh-superagent's place in the shared Assistant state protocol.

The native store is the bundle's own database, ``orchestrator.sqlite`` in its
state directory under DSH_HOME, copied through SQLite's online backup so a
running Executor never leaves a torn file, with the Node SQLite the bundle runs
on (``superagent_sqlite.mjs``). The profile archive is the rest of
what the harness keeps for this profile: its Session logs, its user settings
layer, its storages and the bundle's other state. Left out are the platform's
own files, the harness credential store (credentials reach the box from the
platform on every activation), the bundle's command shims and git
configuration (its setup writes them again), Ticket checkouts, which are
workspace content and can be prepared again, and each profile's installed
packages (its package.json, pnpm files and node_modules), which every
activation installs from the Assistant's configuration; the profile's own
plugin configuration (cordis.patch.yml) is kept.

Run as ``astrabox_state_gate``/``astrabox_state_mirror`` with this layout:
``python3 superagent_state.py gate {publish,ready,restore} --profile-env ...``
or ``python3 superagent_state.py mirror {ready,mirror,save} --profile-env ...``.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any

import astrabox_state_gate as gate
import astrabox_state_mirror as mirror

STATE_DIR = "state/superagent"
NATIVE = f"{STATE_DIR}/orchestrator.sqlite"
MANIFEST_NAME = "astrabox-profile.json"
#: Paths under DSH_HOME that are never part of the profile archive.
_EXCLUDED_TOP = frozenset({".credentials.yaml"})
_EXCLUDED_STATE = frozenset({"worker-bin", "worker-gitconfig", "work"})
#: A profile's installed packages, under ``profiles/<name>/``.
_EXCLUDED_PROFILE = frozenset({"package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "node_modules"})
_NATIVE_FILES = frozenset({
    "orchestrator.sqlite", "orchestrator.sqlite-wal", "orchestrator.sqlite-shm",
    "orchestrator.sqlite-journal",
})


_SQLITE = str(Path(__file__).with_name("superagent_sqlite.mjs"))


def _node_sqlite(*arguments: str) -> None:
    result = subprocess.run(
        ["node", "--no-warnings", _SQLITE, *arguments], capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-500:] or f"superagent_sqlite.mjs {arguments[0]} failed")


def _snapshot_native(home: Path, destination: Path) -> None:
    _node_sqlite("snapshot", str(home / NATIVE), str(destination))


def _restore_native(source: Path, home: Path) -> None:
    _node_sqlite("check", str(source))
    state = home / STATE_DIR
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in _NATIVE_FILES:
        (state / name).unlink(missing_ok=True)
    shutil.copyfile(source, state / "orchestrator.sqlite")
    os.chmod(state / "orchestrator.sqlite", 0o600)


def _verify_native(home: Path) -> None:
    """The database belongs to whichever profile holds it; nothing in it names a home."""


def _governed(relative: PurePosixPath) -> bool:
    parts = relative.parts
    if not parts or parts[0].startswith("astrabox-") or parts[0].startswith(".astrabox-"):
        return False
    if parts[0] in _EXCLUDED_TOP:
        return False
    if parts[:2] == ("state", "superagent") and len(parts) >= 3:
        return parts[2] not in _EXCLUDED_STATE and parts[2] not in _NATIVE_FILES
    if parts[0] == "profiles" and len(parts) >= 3:
        return parts[2] not in _EXCLUDED_PROFILE
    return True


def _files(home: Path) -> list[Path]:
    found: list[Path] = []
    for parent, directories, names in os.walk(home, followlinks=False):
        base = Path(parent)
        directories[:] = sorted(
            name for name in directories
            if _governed(PurePosixPath((base / name).relative_to(home).as_posix()))
        )
        for name in sorted(names):
            path = base / name
            if path.is_file() and not path.is_symlink() and _governed(
                PurePosixPath(path.relative_to(home).as_posix())
            ):
                found.append(path)
    return found


def _stamp_profile(home: Path) -> tuple[Any, ...]:
    stamp = []
    for path in _files(home):
        metadata = path.stat()
        stamp.append((str(path.relative_to(home)), metadata.st_size, metadata.st_mtime_ns))
    return tuple(stamp)


def _pack_profile(home: Path) -> bytes:
    files = _files(home)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        manifest = json.dumps(
            {"files": [path.relative_to(home).as_posix() for path in files]}, sort_keys=True
        ).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
        for path in files:
            archive.add(path, arcname=path.relative_to(home).as_posix(), recursive=False)
    return buffer.getvalue()


def _restore_profile(payload: bytes, home: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = archive.getmembers()
        if not members or members[0].name != MANIFEST_NAME:
            raise RuntimeError("dsh-superagent profile archive has no manifest")
        for member in members[1:]:
            relative = PurePosixPath(member.name)
            if (
                not member.isfile() or relative.is_absolute() or ".." in relative.parts
                or not _governed(relative)
            ):
                raise RuntimeError(f"dsh-superagent profile archive holds an invalid entry {member.name!r}")
        with tempfile.TemporaryDirectory(prefix=".astrabox-profile-restore-", dir=home) as staging:
            archive.extractall(staging, members=members[1:], filter="data")
            for member in members[1:]:
                destination = home / member.name
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.replace(Path(staging) / member.name, destination)


LAYOUT = gate.StateLayout(
    tag="DSH_SUPERAGENT",
    label="dsh-superagent",
    marker=Path("/run/astrabox-dsh-superagent/profile-ready.json"),
    home_name=".dsh",
    env_name="astrabox-dsh-superagent.env",
    home_key="dsh_home",
    native_name=NATIVE,
    native_stamp_names=(NATIVE, f"{NATIVE}-wal"),
    snapshot_native=_snapshot_native,
    restore_native=_restore_native,
    verify_native=_verify_native,
    pack_profile=_pack_profile,
    restore_profile=_restore_profile,
    stamp_profile=_stamp_profile,
)


if __name__ == "__main__":
    role, sys.argv = sys.argv[1] if len(sys.argv) > 1 else "", sys.argv[:1] + sys.argv[2:]
    if role == "gate":
        raise SystemExit(gate.main(LAYOUT, __doc__))
    if role == "mirror":
        raise SystemExit(mirror.main(LAYOUT, __doc__))
    print("usage: superagent_state.py {gate,mirror} ...", file=sys.stderr)
    raise SystemExit(64)
