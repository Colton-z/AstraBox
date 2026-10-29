"""Gate Hermes startup on this box's platform initialization and native restore."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from typing import Any


PROFILE_MARKER = Path("/run/astrabox-hermes/profile-ready.json")
TARGET_NAME = "astrabox-runtime-state.json"
RECEIPT_NAME = "astrabox-runtime-state-restored.json"
INITIAL_RECOVERY_NAME = "astrabox-initial-state-recovery.json"
#: The gateway's private creation receipts in ``state_meta``
#: (``POLICY_PREFIX`` in ``hermes_state_local.py`` at the pinned commit).
LOCAL_RECEIPT_KEYS = "gateway.local_policy.v1:*"


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


def verify_native_identity(home: Path) -> None:
    """Refuse a restored SessionDB whose conversations another home or account owns.

    Hermes' gateway derives a conversation's id from the owning profile's
    absolute HERMES_HOME, the principal that created it and the creation
    request, and it keys each admission by that principal; a client of the
    gateway's own account is ``uid:<uid>`` (``local_identity`` in
    ``gateway/session_local_recovery.py``, the control socket's peer subject in
    ``gateway/control_socket.py``). Each creation receipt records both. Under
    another home or uid no recorded conversation would resume, and an input
    sent again would be admitted as a new one, so the restore stops here.
    """
    database = home / "state.db"
    if not database.exists():
        return
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT key, value FROM state_meta WHERE key GLOB ?", (LOCAL_RECEIPT_KEYS,)
        ).fetchall()
    principal = f"uid:{os.geteuid()}"
    for key, value in rows:
        receipt = json.loads(value)
        owner = receipt.get("principal_id")
        if receipt.get("profile_id") != str(home) or (
            isinstance(owner, str) and owner.startswith("uid:") and owner != principal
        ):
            raise RuntimeError(
                f"Restored Hermes conversation {key.removeprefix(LOCAL_RECEIPT_KEYS.rstrip('*'))} belongs to "
                f"HERMES_HOME {receipt.get('profile_id')!r} as {owner!r}; this profile is "
                f"{str(home)!r} as {principal!r}"
            )


def recover_initial_state(
    home: Path, target: dict[str, Any], initialized: dict[str, Any],
    receipt: dict[str, Any], profile: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Publish an immutable native backup under the current allocation's fence."""
    import hermes_profile_state
    from hermes_native_store import snapshot
    from hermes_state_transport import PROFILE, save_snapshot

    proof = target.get("initialization")
    if not isinstance(proof, dict) or proof.get("sandbox_id") != initialized["sandbox_id"]:
        raise RuntimeError("Initial native state recovery needs the current allocation")
    pending = home / INITIAL_RECOVERY_NAME
    if pending.exists():
        recovery = read_json(pending)
        if recovery.get("owner") != target["owner"]:
            raise RuntimeError("Initial native state recovery belongs to another owner")
        name = recovery.get("backup")
        if not isinstance(name, str) or not name.startswith(".astrabox-initial-state-") or Path(name).name != name:
            raise RuntimeError("Initial native state backup path is invalid")
        backup = home / name
        if backup.is_symlink() or not backup.is_dir() or backup.stat().st_uid != os.geteuid():
            raise RuntimeError("Initial native state backup is not owned by this profile")
    else:
        if (
            receipt.get("owner") != target["owner"]
            or receipt.get("hermes_home") != str(home)
            or not receipt.get("sandbox_id")
            or receipt.get("snapshot_id") is not None
            or receipt.get("profile_snapshot_id") != profile["snapshot_id"]
        ):
            raise RuntimeError("Unpublished native state has no matching owner receipt")
        backup = Path(tempfile.mkdtemp(prefix=".astrabox-initial-state-", dir=home))
        snapshot(home, backup / "state.db")
        atomic_json(backup / "previous-receipt.json", receipt, mode=0o600)
        profile_bytes = hermes_profile_state.pack(home)
        descriptor = os.open(backup / "profile.tar.gz", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(profile_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        recovery = {
            "owner": target["owner"], "backup": backup.name,
            "profile_snapshot_id": profile["snapshot_id"],
            "native_sha256": hashlib.sha256((backup / "state.db").read_bytes()).hexdigest(),
            "profile_sha256": hashlib.sha256(profile_bytes).hexdigest(),
        }
        atomic_json(pending, recovery, mode=0o600)
        for path in (backup / "state.db", backup, home):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    native_path, profile_path = backup / "state.db", backup / "profile.tar.gz"
    if any(path.is_symlink() or not path.is_file() for path in (native_path, profile_path)):
        raise RuntimeError("Initial native state backup is not a regular file")
    native_bytes, profile_bytes = native_path.read_bytes(), profile_path.read_bytes()
    if (
        hashlib.sha256(native_bytes).hexdigest() != recovery["native_sha256"]
        or hashlib.sha256(profile_bytes).hexdigest() != recovery["profile_sha256"]
    ):
        raise RuntimeError("Initial native state backup checksum changed")
    # Preserve the complete profile before the native head can become visible.
    # A lost acknowledgement resends these exact bytes through the normal CAS.
    profile_id = save_snapshot(
        target, profile_bytes, recovery["profile_snapshot_id"], PROFILE, initialization=proof,
    )
    native_id = save_snapshot(target, native_bytes, None, initialization=proof)
    print("HERMES_STATE_INITIAL_RECOVERED", flush=True)
    return (
        {"snapshot_id": native_id, "payload": native_bytes},
        {"snapshot_id": profile_id, "payload": profile_bytes},
        backup.name,
    )


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
    receipt: dict[str, Any] = {}
    pending = home / INITIAL_RECOVERY_NAME
    if receipt_path.exists():
        receipt = read_json(receipt_path)
        if pending.exists() and receipt.get("initial_recovery_backup") == read_json(pending).get("backup"):
            pending.unlink()
        if all(receipt.get(key) == value for key, value in identity.items()):
            print("HERMES_STATE_RESTORE_ALREADY_INITIALIZED", flush=True)
            return

    import hermes_profile_state
    from hermes_native_store import restore
    from hermes_state_transport import PROFILE, load_snapshot

    loaded = load_snapshot(target)
    profile = load_snapshot(target, PROFILE)
    recovered_backup = None
    if pending.exists() or (loaded["snapshot_id"] is None and (home / "state.db").exists()):
        loaded, profile, recovered_backup = recover_initial_state(home, target, initialized, receipt, profile)
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
    verify_native_identity(home)
    # No saved profile means none was published yet; whatever the home holds
    # (a persistent workspace's files, or nothing) stays and is published next.
    if profile["snapshot_id"] is not None:
        hermes_profile_state.restore(profile["payload"], home)
    # This cold restore supersedes the previous box's unacknowledged local copy.
    # The mirror cannot adopt this profile until the new receipt is published.
    (home / "astrabox-runtime-state-pending.db").unlink(missing_ok=True)
    (home / "astrabox-profile-state-pending.tar.gz").unlink(missing_ok=True)
    atomic_json(
        receipt_path,
        {
            **identity,
            "snapshot_id": loaded["snapshot_id"],
            "profile_snapshot_id": profile["snapshot_id"],
            **({"initial_recovery_backup": recovered_backup} if recovered_backup is not None else {}),
        },
        mode=0o600,
    )
    if recovered_backup is not None:
        pending.unlink()
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
