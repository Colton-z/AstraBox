"""Gate an Assistant runtime's start on this box's initialization and native restore.

One protocol for every Assistant runtime image. The platform, as root, writes
the runtime-state target into the profile's home and publishes a root-owned
marker naming this box, the home and the owner (``publish``). The runtime's
service waits for that marker (``ready``), then restores the native store and
the profile archive once per box as the profile owner (``restore_once``) and
records a receipt the mirror commits against. A box that already holds native
state the platform has never seen publishes it first (initial recovery), so a
cold profile never silently replaces conversations.

An engine supplies a :class:`StateLayout`: where its home and environment file
are, what its native store is called, and how its native store and profile
archive are copied, restored and checked. Everything else is shared.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any, Callable

from astrabox_state_transport import PROFILE, load_snapshot, save_snapshot

TARGET_NAME = "astrabox-runtime-state.json"
RECEIPT_NAME = "astrabox-runtime-state-restored.json"
INITIAL_RECOVERY_NAME = "astrabox-initial-state-recovery.json"
PENDING_NAME = "astrabox-runtime-state-pending.db"
PROFILE_PENDING_NAME = "astrabox-profile-state-pending.tar.gz"
#: Where every Assistant profile home is provisioned (``ASSISTANT_HOME_ROOT``).
HOME_ROOT = Path("/home/conversations")


@dataclass(frozen=True)
class StateLayout:
    """One engine's place in the shared protocol."""

    #: Prefix of the markers the adapter reads from this protocol's output.
    tag: str
    #: The engine's name in messages.
    label: str
    #: Root-owned evidence that platform provisioning initialized this box.
    marker: Path
    #: The profile's engine directory under its home, e.g. ``.hermes``.
    home_name: str
    #: The environment file the platform writes into that directory.
    env_name: str
    #: The key naming that directory in the marker and the receipt.
    home_key: str
    #: The native store's path relative to that directory.
    native_name: str
    #: Files whose metadata changing means the native store may have changed.
    native_stamp_names: tuple[str, ...]
    #: Copy the live native store under ``home`` to a consistent file.
    snapshot_native: Callable[[Path, Path], Any]
    #: Install a native store file into ``home``.
    restore_native: Callable[[Path, Path], Any]
    #: Refuse a restored native store this profile cannot resume.
    verify_native: Callable[[Path], None]
    #: The profile archive: pack, restore and fingerprint.
    pack_profile: Callable[[Path], bytes]
    restore_profile: Callable[[bytes, Path], None]
    stamp_profile: Callable[[Path], Any]


def read_json(path: Path, layout: StateLayout) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{layout.label} bootstrap input is not a regular private file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{layout.label} bootstrap input must be an object")
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


def target_for(env_path: Path, layout: StateLayout) -> tuple[Path, dict[str, Any]]:
    home = env_path.parent
    if (
        not env_path.is_absolute() or env_path.resolve() != env_path
        or env_path.name != layout.env_name
        or home.name != layout.home_name or home.parent.parent != HOME_ROOT
    ):
        raise RuntimeError(f"{layout.label} profile is outside the provisioned profile root")
    target = read_json(home / TARGET_NAME, layout)
    if not isinstance(target.get("owner"), dict) or not isinstance(target.get("base_url"), str):
        raise RuntimeError(f"{layout.label} profile has no platform runtime-state target")
    return home, target


def marker(layout: StateLayout) -> dict[str, Any] | None:
    if not layout.marker.exists():
        return None
    metadata = layout.marker.stat()
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise RuntimeError(f"{layout.label} initialization evidence is not root-owned and read-only")
    return read_json(layout.marker, layout)


def ready(env_path: Path, layout: StateLayout) -> bool:
    initialized = marker(layout)
    if initialized is None:
        return False
    home, target = target_for(env_path, layout)
    return (
        initialized.get(layout.home_key) == str(home)
        and initialized.get("owner") == target["owner"]
        and initialized.get("profile_env_sha256") == hashlib.sha256(env_path.read_bytes()).hexdigest()
    )


def publish(env_path: Path, sandbox_id: str, layout: StateLayout) -> None:
    if os.geteuid() != 0 or not sandbox_id:
        raise RuntimeError("Only platform provisioning can publish the box initialization")
    home, target = target_for(env_path, layout)
    previous = marker(layout)
    identity = {"sandbox_id": sandbox_id, layout.home_key: str(home), "owner": target["owner"]}
    if previous is not None and any(previous.get(key) != value for key, value in identity.items()):
        raise RuntimeError(f"This box already belongs to another {layout.label} profile")
    layout.marker.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if layout.marker.parent.is_symlink() or layout.marker.parent.stat().st_uid != 0:
        raise RuntimeError(f"{layout.label} initialization directory is not platform-owned")
    layout.marker.parent.chmod(0o755)
    atomic_json(layout.marker, {
        **identity, "profile_env_sha256": hashlib.sha256(env_path.read_bytes()).hexdigest(),
    }, mode=0o644)
    print(f"{layout.tag}_PROFILE_INITIALIZED fresh={int(previous is None)}", flush=True)


def recover_initial_state(
    home: Path, target: dict[str, Any], initialized: dict[str, Any],
    receipt: dict[str, Any], profile: dict[str, Any], layout: StateLayout,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Publish an immutable native backup under the current allocation's fence."""
    proof = target.get("initialization")
    if not isinstance(proof, dict) or proof.get("sandbox_id") != initialized["sandbox_id"]:
        raise RuntimeError("Initial native state recovery needs the current allocation")
    pending = home / INITIAL_RECOVERY_NAME
    if pending.exists():
        recovery = read_json(pending, layout)
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
            or receipt.get(layout.home_key) != str(home)
            or not receipt.get("sandbox_id")
            or receipt.get("snapshot_id") is not None
            or receipt.get("profile_snapshot_id") != profile["snapshot_id"]
        ):
            raise RuntimeError("Unpublished native state has no matching owner receipt")
        backup = Path(tempfile.mkdtemp(prefix=".astrabox-initial-state-", dir=home))
        (backup / layout.native_name).parent.mkdir(parents=True, exist_ok=True)
        layout.snapshot_native(home, backup / layout.native_name)
        atomic_json(backup / "previous-receipt.json", receipt, mode=0o600)
        profile_bytes = layout.pack_profile(home)
        descriptor = os.open(backup / "profile.tar.gz", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(profile_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        recovery = {
            "owner": target["owner"], "backup": backup.name,
            "profile_snapshot_id": profile["snapshot_id"],
            "native_sha256": hashlib.sha256((backup / layout.native_name).read_bytes()).hexdigest(),
            "profile_sha256": hashlib.sha256(profile_bytes).hexdigest(),
        }
        atomic_json(pending, recovery, mode=0o600)
        for path in (backup / layout.native_name, backup, home):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    native_path, profile_path = backup / layout.native_name, backup / "profile.tar.gz"
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
    print(f"{layout.tag}_STATE_INITIAL_RECOVERED", flush=True)
    return (
        {"snapshot_id": native_id, "payload": native_bytes},
        {"snapshot_id": profile_id, "payload": profile_bytes},
        backup.name,
    )


def restore_once(env_path: Path, layout: StateLayout) -> None:
    if not ready(env_path, layout):
        raise RuntimeError(f"This box's {layout.label} profile has not been initialized")
    home, target = target_for(env_path, layout)
    if os.geteuid() == 0 or home.stat().st_uid != os.geteuid():
        raise RuntimeError(f"{layout.label} restore must run as the profile owner")
    initialized = marker(layout)
    if initialized is None:
        raise RuntimeError(f"{layout.label} initialization disappeared before restore")
    identity = {key: initialized[key] for key in ("sandbox_id", layout.home_key, "owner")}
    receipt_path = home / RECEIPT_NAME
    receipt: dict[str, Any] = {}
    pending = home / INITIAL_RECOVERY_NAME
    if receipt_path.exists():
        receipt = read_json(receipt_path, layout)
        if pending.exists() and receipt.get("initial_recovery_backup") == read_json(pending, layout).get("backup"):
            pending.unlink()
        if all(receipt.get(key) == value for key, value in identity.items()):
            print(f"{layout.tag}_STATE_RESTORE_ALREADY_INITIALIZED", flush=True)
            return

    native = home / layout.native_name
    loaded = load_snapshot(target)
    profile = load_snapshot(target, PROFILE)
    recovered_backup = None
    if pending.exists() or (loaded["snapshot_id"] is None and native.exists()):
        loaded, profile, recovered_backup = recover_initial_state(
            home, target, initialized, receipt, profile, layout
        )
    if loaded["snapshot_id"] is None:
        if native.exists():
            raise RuntimeError(
                f"Platform has no snapshot but the cold profile contains {layout.native_name}"
            )
    else:
        with tempfile.TemporaryDirectory(prefix=".astrabox-restore-", dir=home) as temporary:
            source = Path(temporary) / layout.native_name
            source.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(loaded["payload"])
            layout.restore_native(source, home)
    layout.verify_native(home)
    # No saved profile means none was published yet; whatever the home holds
    # (a persistent workspace's files, or nothing) stays and is published next.
    if profile["snapshot_id"] is not None:
        layout.restore_profile(profile["payload"], home)
    # This cold restore supersedes the previous box's unacknowledged local copy.
    # The mirror cannot adopt this profile until the new receipt is published.
    (home / PENDING_NAME).unlink(missing_ok=True)
    (home / PROFILE_PENDING_NAME).unlink(missing_ok=True)
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
    print(f"{layout.tag}_STATE_RESTORE_COMPLETE", flush=True)


def main(layout: StateLayout, description: str | None = None) -> int:
    parser = argparse.ArgumentParser(description=description or __doc__)
    parser.add_argument("action", choices=("publish", "ready", "restore"))
    parser.add_argument("--profile-env", type=Path, required=True)
    parser.add_argument("--sandbox-id")
    args = parser.parse_args()
    try:
        if args.action == "publish":
            publish(args.profile_env, args.sandbox_id, layout)
        elif args.action == "ready":
            return 0 if ready(args.profile_env, layout) else 1
        else:
            restore_once(args.profile_env, layout)
    except Exception as exc:
        print(f"{layout.tag}_STATE_BOOTSTRAP_FAILED: {exc}", file=sys.stderr, flush=True)
        return 78
    return 0
