"""Gate Hermes startup on this box's platform initialization and native restore."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any


PROFILE_MARKER = Path("/run/astrabox-hermes/profile-ready.json")
TARGET_NAME = "astrabox-runtime-state.json"
RECEIPT_NAME = "astrabox-runtime-state-restored.json"


def read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Hermes bootstrap input is not a regular private file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Hermes bootstrap input must be an object")
    return value


def atomic_json(path: Path, value: dict[str, Any], *, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def target_for(env_path: Path) -> tuple[Path, dict[str, Any]]:
    home = env_path.parent
    if (
        not env_path.is_absolute() or env_path.resolve() != env_path
        or env_path.name != "astrabox-hermes.env"
        or home.name != ".hermes" or home.parent.parent != Path("/home/conversations")
    ):
        raise RuntimeError("Hermes profile is outside the provisioned profile root")
    target = read_json(home / TARGET_NAME)
    if not isinstance(target.get("owner"), dict) or not isinstance(target.get("base_url"), str):
        raise RuntimeError("Hermes profile has no platform runtime-state target")
    return home, target


def marker() -> dict[str, Any] | None:
    if not PROFILE_MARKER.exists():
        return None
    metadata = PROFILE_MARKER.stat()
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise RuntimeError("Hermes initialization evidence is not root-owned and read-only")
    return read_json(PROFILE_MARKER)


def ready(env_path: Path) -> bool:
    initialized = marker()
    if initialized is None:
        return False
    home, target = target_for(env_path)
    return (
        initialized.get("hermes_home") == str(home)
        and initialized.get("owner") == target["owner"]
        and initialized.get("profile_env_sha256") == hashlib.sha256(env_path.read_bytes()).hexdigest()
    )


def publish(env_path: Path, sandbox_id: str) -> None:
    if os.geteuid() != 0 or not sandbox_id:
        raise RuntimeError("Only platform provisioning can publish the box initialization")
    home, target = target_for(env_path)
    previous = marker()
    identity = {"sandbox_id": sandbox_id, "hermes_home": str(home), "owner": target["owner"]}
    if previous is not None and any(previous.get(key) != value for key, value in identity.items()):
        raise RuntimeError("This box already belongs to another Hermes profile")
    PROFILE_MARKER.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if PROFILE_MARKER.parent.is_symlink() or PROFILE_MARKER.parent.stat().st_uid != 0:
        raise RuntimeError("Hermes initialization directory is not platform-owned")
    PROFILE_MARKER.parent.chmod(0o755)
    atomic_json(PROFILE_MARKER, {
        **identity, "profile_env_sha256": hashlib.sha256(env_path.read_bytes()).hexdigest(),
    }, mode=0o644)
    print(f"HERMES_PROFILE_INITIALIZED fresh={int(previous is None)}", flush=True)


def restore_once(env_path: Path) -> None:
    if not ready(env_path):
        raise RuntimeError("This box's Hermes profile has not been initialized")
    home, target = target_for(env_path)
    if os.geteuid() == 0 or home.stat().st_uid != os.geteuid():
        raise RuntimeError("Hermes restore must run as the profile owner")
    initialized = marker()
    if initialized is None:
        raise RuntimeError("Hermes initialization disappeared before restore")
    identity = {key: initialized[key] for key in ("sandbox_id", "hermes_home", "owner")}
    receipt_path = home / RECEIPT_NAME
    if receipt_path.exists():
        receipt = read_json(receipt_path)
        if all(receipt.get(key) == value for key, value in identity.items()):
            print("HERMES_STATE_RESTORE_ALREADY_INITIALIZED", flush=True)
            return

    from hermes_native_store import restore
    from hermes_state_transport import load_snapshot

    loaded = load_snapshot(target)
    if loaded["snapshot_id"] is None:
        if (home / "state.db").exists():
            raise RuntimeError("Platform has no snapshot but the cold profile contains state.db")
    else:
        with tempfile.TemporaryDirectory(prefix=".astrabox-restore-", dir=home) as temporary:
            source = Path(temporary) / "state.db"
            descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(loaded["payload"])
            restore(source, home)
    # This cold restore supersedes the previous box's unacknowledged local copy.
    # The mirror cannot adopt this profile until the new receipt is published.
    (home / "astrabox-runtime-state-pending.db").unlink(missing_ok=True)
    atomic_json(receipt_path, {**identity, "snapshot_id": loaded["snapshot_id"]}, mode=0o600)
    print("HERMES_STATE_RESTORE_COMPLETE", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("publish", "ready", "restore"))
    parser.add_argument("--profile-env", type=Path, required=True)
    parser.add_argument("--sandbox-id")
    args = parser.parse_args()
    try:
        if args.action == "publish":
            publish(args.profile_env, args.sandbox_id)
        elif args.action == "ready":
            return 0 if ready(args.profile_env) else 1
        else:
            restore_once(args.profile_env)
    except Exception as exc:
        print(f"HERMES_STATE_BOOTSTRAP_FAILED: {exc}", file=sys.stderr, flush=True)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
