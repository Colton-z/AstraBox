#!/usr/bin/env python3
"""Configure the snapshot differ on a systemd containerd node."""

from __future__ import annotations

import argparse
import copy
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import tomllib
import uuid


NAME = "astrabox-overlay"
SOCKET = "/run/astrabox-snapshot-differ/diff.sock"
SERVICE = "astrabox-snapshot-differ.service"
BINARY = Path("/usr/local/libexec/astrabox-snapshot-differ")
UNIT = Path("/etc/systemd/system") / SERVICE
CONFIG = Path("/etc/containerd/config.toml")
STATE = Path("/var/lib/astrabox-snapshot-differ")
DIFF_SERVICE = "io.containerd.service.v1.diff-service"
PROXY = {"type": "diff", "address": SOCKET}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def render_config(source: str) -> str:
    """Change only the preferred differ and its owned proxy registration."""
    current = tomllib.loads(source)
    if current.get("version") not in (2, 3, 4):
        raise ValueError("requires containerd configuration version 2–4")
    imports = current.get("imports", [])
    if not isinstance(imports, list) or any(
        not isinstance(pattern, str) or not Path(pattern).is_absolute()
        or "*" not in pattern or glob.glob(pattern)
        for pattern in imports
    ):
        raise ValueError("imports must be empty absolute globs; configure active imported files explicitly")
    expected = copy.deepcopy(current)
    proxies = expected.setdefault("proxy_plugins", {})
    if NAME in proxies and proxies[NAME] != PROXY:
        raise ValueError("the snapshot differ name already refers to a different proxy")
    proxies[NAME] = dict(PROXY)
    service = expected.setdefault("plugins", {}).setdefault(DIFF_SERVICE, {})
    order = service.get("default", ["walking"])
    if not isinstance(order, list) or not order or not all(isinstance(x, str) for x in order):
        raise ValueError("invalid native differ order")
    if "walking" not in order:
        raise ValueError("native walking differ must remain available")
    service["default"] = [NAME, *[name for name in order if name != NAME]]
    if expected == current:
        return source
    rendered = source.rstrip() + "\n"
    table = re.compile(r'''(?m)^[ \t]*\[plugins\.(["'])io\.containerd\.service\.v1\.diff-service\1\][ \t]*(?:\#.*)?$''')
    match = table.search(rendered)
    assignment = "default = " + json.dumps(service["default"]) + "\n"
    if DIFF_SERVICE not in current.get("plugins", {}):
        rendered += f'\n[plugins."{DIFF_SERVICE}"]\n{assignment}'
    elif match is None:
        raise ValueError("unsupported diff service table spelling; edit the configuration explicitly")
    else:
        start = match.end() + 1
        next_table = re.search(r"(?m)^[ \t]*\[", rendered[start:])
        end = start + next_table.start() if next_table else len(rendered)
        body = rendered[start:end]
        field = re.search(r"(?m)^[ \t]*default[ \t]*=", body)
        if field is None:
            body += assignment
        else:
            field_end = None
            lines = body[field.start():].splitlines(keepends=True)
            candidate = ""
            for line in lines:
                candidate += line
                try:
                    parsed = tomllib.loads(candidate)
                except tomllib.TOMLDecodeError:
                    continue
                if parsed == {"default": order}:
                    field_end = field.start() + len(candidate)
                break
            if field_end is None:
                raise ValueError("cannot isolate the native differ order")
            body = body[:field.start()] + assignment + body[field_end:]
        rendered = rendered[:start] + body + rendered[end:]
    if NAME not in current.get("proxy_plugins", {}):
        rendered += f'\n[proxy_plugins.{NAME}]\n  type = "diff"\n  address = "{SOCKET}"\n'
    if tomllib.loads(rendered) != expected:
        raise ValueError("configuration rewrite would change an unrelated setting")
    return rendered


def command(args: list[str], *, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr[-1500:]}")
    return result


def replace(path: Path, data: bytes, mode: int) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def active_binary(binary_sha256: str) -> bool:
    state = command(["systemctl", "show", SERVICE, "--property=MainPID", "--value"])
    pid = state.stdout.strip()
    if not pid.isdecimal() or int(pid) <= 0:
        return False
    try:
        return digest(Path(f"/proc/{pid}/exe").read_bytes()) == binary_sha256
    except FileNotFoundError:
        return False


def healthy(binary_sha256: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if active_binary(binary_sha256) and command([str(BINARY), "--check"], check=False, timeout=5).returncode == 0:
            return
        time.sleep(0.25)
    raise RuntimeError("snapshot differ did not serve from the expected binary")


def install(binary: bytes, unit: bytes, identity: dict[str, str]) -> dict[str, object]:
    if os.geteuid() != 0:
        raise ValueError("node installation requires root")
    if digest(binary) != identity["binary_sha256"] or digest(unit) != identity["unit_sha256"]:
        raise ValueError("deployment input hashes differ")
    if not re.fullmatch(r"[0-9a-f]{64}", identity["source_sha256"]):
        raise ValueError("source identity is missing")
    for path in (BINARY, UNIT, CONFIG):
        if path.is_symlink():
            raise ValueError(f"refusing managed symlink: {path}")
    original = {path: path.read_bytes() if path.exists() else None for path in (BINARY, UNIT, CONFIG)}
    if original[CONFIG] is None:
        raise ValueError("containerd configuration does not exist")
    config_mode = stat.S_IMODE(CONFIG.stat().st_mode)
    desired = render_config(original[CONFIG].decode()).encode()
    if BINARY.parent.is_symlink():
        raise ValueError("refusing a symlink for the system executable directory")
    BINARY.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    # This shared system directory must remain searchable by other tools even
    # when the installer itself uses a private umask.
    BINARY.parent.chmod(0o755)
    before_active = command(["systemctl", "is-active", SERVICE], check=False).returncode == 0
    before_enabled = command(["systemctl", "is-enabled", SERVICE], check=False).returncode == 0
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="validate-", dir=STATE) as temp:
        candidate = Path(temp) / "config.toml"
        candidate.write_bytes(desired)
        command(["containerd", "--config", str(candidate), "config", "dump"])
    changed = original[BINARY] != binary or original[UNIT] != unit or original[CONFIG] != desired
    backup = None
    if changed:
        backup = STATE / ("backup-" + uuid.uuid4().hex)
        backup.mkdir(mode=0o700)
        for index, (path, data) in enumerate(original.items()):
            if data is not None:
                replace(backup / str(index), data, 0o600)
        replace(backup / "paths.json", json.dumps({str(i): str(p) for i, p in enumerate(original)}).encode(), 0o600)
    config_applied = False
    try:
        if original[BINARY] != binary:
            replace(BINARY, binary, 0o755)
        if original[UNIT] != unit:
            replace(UNIT, unit, 0o644)
            command(["systemctl", "daemon-reload"])
        if not before_enabled:
            command(["systemctl", "enable", SERVICE])
        if original[UNIT] != unit or not active_binary(identity["binary_sha256"]):
            command(["systemctl", "restart", SERVICE])
        healthy(identity["binary_sha256"])
        if CONFIG.read_bytes() != original[CONFIG]:
            raise RuntimeError("containerd configuration changed during installation")
        if render_config(original[CONFIG].decode()).encode() != desired:
            raise RuntimeError("containerd configuration inputs changed during installation")
        if desired != original[CONFIG]:
            replace(CONFIG, desired, config_mode)
            config_applied = True
            command(["systemctl", "restart", "containerd"])
        plugins = command(["ctr", "--address", "/run/containerd/containerd.sock", "plugins", "list"]).stdout
        for name in (NAME, "walking"):
            if not any(line.split()[:2] == ["io.containerd.differ.v1", name] and line.split()[-1] == "ok" for line in plugins.splitlines()):
                raise RuntimeError(f"containerd differ {name} is not ready")
        healthy(identity["binary_sha256"])
        receipt = {**identity, "state": "CONFIGURED", "config_sha256": digest(desired), "changed": changed, "backup": str(backup) if backup else None}
        replace(STATE / "receipt.json", (json.dumps(receipt, sort_keys=True) + "\n").encode(), 0o644)
        return receipt
    except BaseException as failure:
        # Restore routing before stopping the new service; an unavailable proxy
        # is not an unsupported request and containerd will not fall back.
        try:
            if not config_applied and CONFIG.read_bytes() != original[CONFIG]:
                raise RuntimeError("another writer changed routing; keeping the new service available")
            if config_applied:
                if CONFIG.read_bytes() != desired:
                    raise RuntimeError("configuration changed after installation; automatic rollback refused")
                replace(CONFIG, original[CONFIG], config_mode)
                command(["systemctl", "restart", "containerd"])
            if not before_enabled:
                command(["systemctl", "disable", SERVICE], check=False)
            for path in (BINARY, UNIT):
                if original[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    replace(path, original[path], 0o755 if path == BINARY else 0o644)
            command(["systemctl", "daemon-reload"])
            command(["systemctl", "restart" if before_active else "stop", SERVICE], check=before_active)
        except BaseException as rollback:
            raise RuntimeError(f"installation failed; rollback needs attention: {rollback}; backup: {backup}") from failure
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", type=Path, help="render a configuration without installing")
    parser.add_argument("--inputs", type=Path, help="directory containing the binary, service, and identity.json")
    args = parser.parse_args()
    if bool(args.render) == bool(args.inputs):
        parser.error("choose exactly one of --render and --inputs")
    if args.render:
        print(render_config(args.render.read_text()), end="")
        return
    with Path("/run/lock/astrabox-snapshot-differ.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        root = args.inputs
        assert root is not None
        identity = json.loads((root / "identity.json").read_text())
        if digest(Path(__file__).read_bytes()) != identity["installer_sha256"]:
            raise ValueError("node installer identity differs")
        print(json.dumps(install((root / "astrabox-snapshot-differ").read_bytes(), (root / SERVICE).read_bytes(), identity), sort_keys=True))


if __name__ == "__main__":
    main()
