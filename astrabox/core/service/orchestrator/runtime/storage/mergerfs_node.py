"""Run node-local workspace views using mergerfs's supported runtime interface.

This executable has no application dependencies. The storage controller owns
placement and sandbox handoff; this process owns only its FUSE mounts. Recreating
a mount does not reconnect an existing sandbox's disconnected FUSE handles.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

VERSION = "2.42.0"
OPTIONS = {
    "passthrough.io": "rw",
    "cache.files": "auto-full",
    "cache.entry": "0",
    "cache.attr": "0",
    "cache.negative-entry": "0",
    "cache.readdir": "false",
    "cache.writeback": "false",
    "minfreespace": "0",
}
SHARED_FILESYSTEMS = {
    "nfs",
    "nfs4",
    "cifs",
    "smb3",
    "ceph",
    "fuse.ceph",
    "glusterfs",
    "fuse.glusterfs",
    "lustre",
    "gpfs",
    "beegfs",
    "fuse.juicefs",
}


def _run(*args: str) -> str:
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=10
    ).stdout.strip()


def _absolute(value: str) -> Path:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value or value == "/":
        raise ValueError(f"Expected a normalized absolute non-root path: {value!r}")
    return Path(value)


def _directory(path: Path, *, create: bool = False, mode: int = 0o755) -> None:
    """Walk with directory handles so a symlink cannot redirect directory creation."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=mode, dir_fd=fd)
                except FileExistsError:
                    pass
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
    finally:
        os.close(fd)


def _mounts(raw: str) -> list[list[str]]:
    mounts = json.loads(raw)
    if not isinstance(mounts, list) or not mounts:
        raise ValueError("mounts must be a non-empty ordered list of [box_path, storage_subpath]")
    seen: set[str] = set()
    for item in mounts:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(x, str) for x in item)
        ):
            raise ValueError("Each mount must be [box_path, storage_subpath]")
        box_path, subpath = item
        _absolute(box_path)
        parts = PurePosixPath(subpath)
        if parts.is_absolute() or ".." in parts.parts or not parts.parts or str(parts) != subpath:
            raise ValueError(f"Invalid workspace subpath: {subpath!r}")
        if any(char in subpath for char in ":=,*?[]\\\n\r\x00"):
            raise ValueError("Workspace paths cannot contain mergerfs branch syntax")
        if parts.name == ".mergerfs":
            raise ValueError("Workspace leaf cannot be the mergerfs runtime control name")
        if box_path in seen:
            raise ValueError(f"Duplicate sandbox mount: {box_path}")
        seen.add(box_path)
    return mounts


def _control(root: Path) -> Path:
    _directory(root)
    control = root / ".control"
    _directory(control, create=True, mode=0o700)
    if control.stat().st_uid != 0 or stat.S_IMODE(control.stat().st_mode) != 0o700:
        raise ValueError("Workspace control directory must be root-owned mode 0700")
    return control


@contextlib.contextmanager
def _lock(control: Path, name: str, *, nonblocking: bool = False) -> Iterator[None]:
    fd = os.open(control / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield
    finally:
        os.close(fd)


def _load(control: Path) -> dict[str, Any]:
    with (control / "state.json").open() as stream:
        state: dict[str, Any] = json.load(stream)
    _mounts(json.dumps(state["mounts"]))
    return state


def _save(control: Path, state: dict[str, Any]) -> None:
    fd, filename = tempfile.mkstemp(dir=control, prefix="state-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, control / "state.json")
        directory = os.open(control, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


def _source(root: Path, index: int) -> str:
    identity = hashlib.sha256(str(root).encode()).hexdigest()[:20]
    return f"astrabox-workspace-{identity}-{index}"


def _mounted(view: Path) -> dict[str, Any] | None:
    result = subprocess.run(
        ["findmnt", "--json", "--mountpoint", str(view), "--output", "TARGET,SOURCE,FSTYPE"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return None
    result.check_returncode()
    entries = json.loads(result.stdout)["filesystems"]
    if len(entries) != 1 or entries[0]["target"] != str(view):
        raise RuntimeError(f"Unexpected mount stack at {view}")
    return dict(entries[0])


def _branch(data: Path, subpath: str, *, create: bool = False) -> str:
    path = data / subpath
    _directory(path, create=create)
    if any(char in str(path) for char in ":=,*?[]\\\n\r\x00"):
        raise ValueError("Data root cannot contain mergerfs branch syntax")
    return f"{path.parent}=RW"


def _read_view(root: Path, data: Path, index: int, item: list[str]) -> dict[str, Any]:
    view = root / str(index)
    mounted = _mounted(view)
    if (
        mounted is None
        or mounted["fstype"] != "fuse.mergerfs"
        or mounted["source"] != _source(root, index)
    ):
        raise RuntimeError(f"Workspace view is absent or not owned by this helper: {view}")
    runtime = view / ".mergerfs"
    actual = {key: os.getxattr(runtime, f"user.mergerfs.{key}").decode() for key in OPTIONS}
    for key, expected in OPTIONS.items():
        value = actual[key]
        if expected == "0" and float(value) == 0:
            continue
        if value != expected:
            raise RuntimeError(f"{view}: {key}={value!r}, expected {expected!r}")
    branches = os.getxattr(runtime, "user.mergerfs.branches").decode()
    if branches != _branch(data, item[1]):
        raise RuntimeError(f"Workspace mapping mismatch at {view}")
    leaf = PurePosixPath(item[1]).name
    _directory(view / leaf)
    return {
        "box_path": item[0],
        "storage_subpath": item[1],
        "view": str(view),
        "consumer_subpath": f"{index}/{leaf}",
        "branches": branches,
        "passthrough_io": actual["passthrough.io"],
        "cache_files": actual["cache.files"],
    }


def _status(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    if state["phase"] != "READY":
        raise RuntimeError(f"Workspace mapping is not ready: {state['phase']}")
    data = _absolute(state["data"])
    return {
        "state": "READY",
        "binding": state["binding"],
        "version": VERSION,
        "mounts": [
            _read_view(root, data, index, item) for index, item in enumerate(state["mounts"])
        ],
    }


def _preflight(data: Path, shared: bool) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("mergerfs I/O passthrough requires root")
    match = re.match(r"(\d+)\.(\d+)", platform.release())
    if match is None or tuple(map(int, match.groups())) < (6, 9):
        raise RuntimeError("mergerfs I/O passthrough requires Linux >= 6.9")
    if not stat.S_ISCHR(Path("/dev/fuse").stat().st_mode):
        raise RuntimeError("/dev/fuse must be a character device")
    version = _run("mergerfs", "--version")
    if re.search(rf"(?<![\d.]){re.escape(VERSION)}(?![\d.])", version) is None:
        raise RuntimeError(f"Expected mergerfs {VERSION}, got {version!r}")
    _directory(data)
    if shared:
        fstype = _run("findmnt", "--noheadings", "--target", str(data), "--output", "FSTYPE")
        if fstype not in SHARED_FILESYSTEMS:
            raise RuntimeError(f"Multi-host workspaces require a shared filesystem; got {fstype!r}")


def _bind(root: Path, data: Path, mounts: list[list[str]], binding: str) -> dict[str, Any]:
    if not binding or len(binding) > 256 or any(ord(char) < 32 for char in binding):
        raise ValueError("binding must be a non-empty session identity")
    control = _control(root)
    with _lock(control, "state.lock"):
        state = _load(control)
        if state["data"] != str(data) or [m[0] for m in state["mounts"]] != [m[0] for m in mounts]:
            raise RuntimeError(
                "Binding cannot change data root, mount order, or sandbox mount paths"
            )
        if [PurePosixPath(item[1]).name for item in state["mounts"]] != [
            PurePosixPath(item[1]).name for item in mounts
        ]:
            raise RuntimeError("Binding cannot change the mounted workspace leaf name")
        if state["binding"] is not None:
            if state["binding"] != binding or state["mounts"] != mounts:
                raise RuntimeError("Workspace entry is already assigned; rebinding is forbidden")
            if state["phase"] == "READY":
                return _status(root, state)
            if state["phase"] != "BINDING":
                raise RuntimeError(f"Cannot bind a stopped helper: {state['phase']}")
        else:
            _status(root, state)
            for _, subpath in mounts:
                _branch(data, subpath, create=True)
            state.update(
                binding=binding, previous_mounts=state["mounts"], mounts=mounts, phase="BINDING"
            )
            _save(control, state)
        for index, item in enumerate(mounts):
            previous = state["previous_mounts"][index]
            view = root / str(index)
            actual = os.getxattr(view / ".mergerfs", "user.mergerfs.branches").decode()
            if actual not in {_branch(data, previous[1]), _branch(data, item[1])}:
                raise RuntimeError(f"Unrecognized mapping while completing binding: {view}")
            _read_view(root, data, index, item if actual == _branch(data, item[1]) else previous)
            os.setxattr(
                view / ".mergerfs", "user.mergerfs.branches", _branch(data, item[1]).encode()
            )
            _read_view(root, data, index, item)
        state.update(phase="READY")
        state.pop("previous_mounts", None)
        _save(control, state)
        return _status(root, state)


def _serve(root: Path, data: Path, mounts: list[list[str]], shared: bool) -> None:
    if root.is_relative_to(data) or data.is_relative_to(root):
        raise ValueError("Workspace data and control views must be separate directory trees")
    _preflight(data, shared)
    _directory(root, create=True)
    control = _control(root)
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _signal, _frame: stopped.set())
    children: list[tuple[Path, subprocess.Popen[bytes]]] = []
    with _lock(control, "serve.lock", nonblocking=True):
        with _lock(control, "state.lock"):
            if (control / "state.json").exists():
                state = _load(control)
                if (
                    state["data"] != str(data)
                    or state["initial_mounts"] != mounts
                    or state["shared"] != shared
                ):
                    raise RuntimeError(
                        "Existing entry cannot be reused with different creation parameters"
                    )
            else:
                state = {
                    "data": str(data),
                    "initial_mounts": mounts,
                    "mounts": mounts,
                    "binding": None,
                    "shared": shared,
                }
            for index in range(len(mounts)):
                view = root / str(index)
                if _mounted(view) is not None:
                    raise RuntimeError(
                        f"Refusing to cover an existing or disconnected mount: {view}"
                    )
                _directory(view, create=True)
                if any(view.iterdir()):
                    raise RuntimeError(f"Refusing to cover files in workspace mountpoint: {view}")
            state.update(phase="STARTING")
            _save(control, state)
        try:
            for index, item in enumerate(state["mounts"]):
                view = root / str(index)
                branch = _branch(data, item[1], create=True)
                options = ",".join(f"{key}={value}" for key, value in OPTIONS.items())
                child = subprocess.Popen(
                    [
                        "mergerfs",
                        "-f",
                        "-o",
                        f"{options},fsname={_source(root, index)}",
                        branch,
                        str(view),
                    ]
                )
                children.append((view, child))
                deadline = time.monotonic() + 20
                while _mounted(view) is None:
                    if child.poll() is not None or stopped.is_set() or time.monotonic() >= deadline:
                        raise RuntimeError(f"mergerfs did not mount {view}; exit={child.poll()}")
                    stopped.wait(0.1)
                _read_view(root, data, index, item)
            with _lock(control, "state.lock"):
                pending = "previous_mounts" in state
                state.update(phase="BINDING" if pending else "READY")
                _save(control, state)
                result = (
                    {"state": "BINDING", "binding": state["binding"]}
                    if pending
                    else _status(root, state)
                )
                print(json.dumps(result), flush=True)
            while not stopped.wait(1):
                for view, child in children:
                    if child.poll() is not None:
                        raise RuntimeError(f"mergerfs exited for {view}: {child.returncode}")
        finally:
            with _lock(control, "state.lock"):
                state = _load(control)
                state.update(phase="STOPPED")
                _save(control, state)
            failures: list[str] = []
            for view, child in reversed(children):
                try:
                    mounted = _mounted(view)
                    if mounted is not None:
                        if mounted["source"] != _source(root, int(view.name)):
                            raise RuntimeError(f"Refusing to unmount foreign view: {view}")
                        _run("umount", str(view))
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    failures.append(str(exc))
                finally:
                    if child.poll() is None:
                        child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
            if failures:
                raise RuntimeError("Workspace mount cleanup failed: " + "; ".join(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "bind", "status"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True)
        if name != "status":
            command.add_argument("--data", required=True)
            command.add_argument("--mounts", required=True)
        if name == "serve":
            command.add_argument("--shared", action="store_true")
        if name == "bind":
            command.add_argument("--binding", required=True)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise RuntimeError("Workspace helper commands require root")
        root = _absolute(args.root)
        if args.command == "serve":
            _serve(root, _absolute(args.data), _mounts(args.mounts), args.shared)
        elif args.command == "bind":
            print(
                json.dumps(_bind(root, _absolute(args.data), _mounts(args.mounts), args.binding)),
                flush=True,
            )
        else:
            control = _control(root)
            with _lock(control, "state.lock"):
                print(json.dumps(_status(root, _load(control))), flush=True)
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "ERROR", "error": str(exc)}), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
