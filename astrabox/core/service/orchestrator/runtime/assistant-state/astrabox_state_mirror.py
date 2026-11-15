"""Publish an Assistant runtime's native store and profile archive as they change.

The mirror runs under the image's transcript-mirror driver once the box's
restore receipt exists (``astrabox_state_gate``). Each pass commits any bytes a
previous pass left pending, then snapshots the native store when its files
changed and the profile archive when its fingerprint changed, each against
the predecessor the receipt records. ``save`` forces both, which is the
release barrier an adapter runs before the platform gives the box up.
"""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import runpy
import sys
import tempfile
import time
from typing import Any

from astrabox_state_gate import (
    PENDING_NAME,
    PROFILE_PENDING_NAME,
    RECEIPT_NAME,
    StateLayout,
    atomic_json,
    marker,
    read_json,
    target_for,
)
from astrabox_state_transport import PROFILE, save_snapshot

LOCK_NAME = "astrabox-runtime-state.lock"
#: How often the profile archive is checked for changes, in seconds.
PROFILE_CHECK_SECONDS = 5.0


def receipt_ready(env_path: Path, layout: StateLayout) -> bool:
    """Accept only a restore receipt for this box's initialized profile."""
    home, target = target_for(env_path, layout)
    initialized = marker(layout)
    receipt_path = home / RECEIPT_NAME
    if initialized is None or not receipt_path.exists():
        return False
    receipt = read_json(receipt_path, layout)
    return (
        initialized.get(layout.home_key) == str(home)
        and initialized.get("owner") == target["owner"]
        and receipt.get(layout.home_key) == str(home)
        and receipt.get("owner") == target["owner"]
        and receipt.get("sandbox_id") == initialized["sandbox_id"]
    )


class StateMirror:
    """One owner's acknowledged head and one immutable, possibly in-flight file."""

    def __init__(self, env_path: Path, layout: StateLayout) -> None:
        self.env_path = env_path
        self.layout = layout
        self.home, _ = target_for(env_path, layout)
        if os.geteuid() == 0 or self.home.stat().st_uid != os.geteuid():
            raise RuntimeError(f"{layout.label} state mirror must run as the profile owner")
        self._last_stamp: tuple[Any, ...] | None = None
        self._last_profile_stamp: Any = None
        self._next_profile_check = 0.0

    def _sync_home(self) -> None:
        descriptor = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _stamp(self) -> tuple[Any, ...]:
        values = []
        for name in self.layout.native_stamp_names:
            try:
                metadata = (self.home / name).stat()
            except FileNotFoundError:
                values.append(None)
            else:
                values.append(
                    (metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
                )
        return tuple(values)

    def _commit(self, target: dict[str, Any], receipt: dict[str, Any]) -> str:
        pending = self.home / PENDING_NAME
        if pending.is_symlink() or not pending.is_file():
            raise RuntimeError(f"{self.layout.label} pending snapshot is not a regular file")
        snapshot_id = save_snapshot(target, pending.read_bytes(), receipt["snapshot_id"])
        receipt["snapshot_id"] = snapshot_id
        atomic_json(self.home / RECEIPT_NAME, receipt, mode=0o600)
        self._sync_home()
        pending.unlink()
        self._sync_home()
        return snapshot_id

    def _save(self, *, force: bool) -> tuple[int, str | None]:
        label = self.layout.label
        descriptor = os.open(self.home / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not receipt_ready(self.env_path, self.layout):
                raise RuntimeError(f"{label} state mirror has no current-box restore receipt")
            _, target = target_for(self.env_path, self.layout)
            receipt = read_json(self.home / RECEIPT_NAME, self.layout)
            if "snapshot_id" not in receipt or (
                receipt["snapshot_id"] is not None
                and (not isinstance(receipt["snapshot_id"], str) or not receipt["snapshot_id"])
            ):
                raise RuntimeError(f"{label} restore receipt has no valid snapshot predecessor")
            sent = 0
            pending = self.home / PENDING_NAME
            if pending.exists() or pending.is_symlink():
                self._commit(target, receipt)
                self._last_stamp = None
                sent += 1
            if not (self.home / self.layout.native_name).is_file():
                if force or receipt["snapshot_id"] is not None:
                    raise RuntimeError(f"{label} native store is missing during snapshot save")
                return sent, None
            stamp = self._stamp()
            if not force and stamp == self._last_stamp:
                return sent, receipt["snapshot_id"]
            with tempfile.TemporaryDirectory(prefix=".astrabox-state-", dir=self.home) as directory:
                temporary = Path(directory) / self.layout.native_name
                temporary.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self.layout.snapshot_native(self.home, temporary)
                except RuntimeError:
                    raise RuntimeError(f"{label} native store snapshot failed") from None
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, pending)
                self._sync_home()
            snapshot_id = self._commit(target, receipt)
            # A write during backup must remain visible to the next poll.
            self._last_stamp = stamp
            return sent + 1, snapshot_id

    def _commit_profile(self, target: dict[str, Any], receipt: dict[str, Any]) -> None:
        pending = self.home / PROFILE_PENDING_NAME
        if pending.is_symlink() or not pending.is_file():
            raise RuntimeError(f"{self.layout.label} pending profile archive is not a regular file")
        receipt["profile_snapshot_id"] = save_snapshot(
            target, pending.read_bytes(), receipt["profile_snapshot_id"], PROFILE
        )
        atomic_json(self.home / RECEIPT_NAME, receipt, mode=0o600)
        self._sync_home()
        pending.unlink()
        self._sync_home()

    def _save_profile(self, *, force: bool) -> int:
        label = self.layout.label
        descriptor = os.open(self.home / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not receipt_ready(self.env_path, self.layout):
                raise RuntimeError(f"{label} state mirror has no current-box restore receipt")
            _, target = target_for(self.env_path, self.layout)
            receipt = read_json(self.home / RECEIPT_NAME, self.layout)
            if "profile_snapshot_id" not in receipt:
                raise RuntimeError(f"{label} restore receipt has no profile predecessor")
            sent = 0
            pending = self.home / PROFILE_PENDING_NAME
            if pending.exists() or pending.is_symlink():
                self._commit_profile(target, receipt)
                self._last_profile_stamp = None
                sent += 1
            stamp = self.layout.stamp_profile(self.home)
            if not force and stamp == self._last_profile_stamp:
                return sent
            with tempfile.NamedTemporaryFile(
                prefix=".astrabox-profile-", dir=self.home, delete=False
            ) as stream:
                stream.write(self.layout.pack_profile(self.home))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(stream.name, pending)
            self._sync_home()
            self._commit_profile(target, receipt)
            # A write during packing must remain visible to the next check.
            self._last_profile_stamp = stamp
            return sent + 1

    def _unchanged(self) -> bool:
        """Nothing to send: no bytes left pending, and the native files as last committed.

        Two ``stat`` calls, checked before the lock and the receipt reads a
        save takes. The mirror polls twice a second for the life of the box,
        so the no-op pass is what it costs almost all the time.
        """
        if self._last_stamp is None:
            return False
        if any(os.path.lexists(self.home / name) for name in (PENDING_NAME, PROFILE_PENDING_NAME)):
            return False
        return self._stamp() == self._last_stamp

    def pump(self) -> int:
        sent = 0 if self._unchanged() else self._save(force=False)[0]
        if time.monotonic() >= self._next_profile_check:
            self._next_profile_check = time.monotonic() + PROFILE_CHECK_SECONDS
            if (
                self._last_profile_stamp is None
                or os.path.lexists(self.home / PROFILE_PENDING_NAME)
                or self.layout.stamp_profile(self.home) != self._last_profile_stamp
            ):
                sent += self._save_profile(force=False)
        return sent

    def save(self) -> str:
        """Acknowledge pending bytes, then force fresh native and profile snapshots."""
        _, snapshot_id = self._save(force=True)
        if snapshot_id is None:
            raise RuntimeError(f"{self.layout.label} state save returned no snapshot identity")
        self._save_profile(force=True)
        return snapshot_id


def main(layout: StateLayout, description: str | None = None) -> int:
    parser = argparse.ArgumentParser(description=description or __doc__)
    parser.add_argument("action", choices=("ready", "mirror", "save"))
    parser.add_argument("--profile-env", type=Path, required=True)
    args = parser.parse_args()
    tag, label = layout.tag, layout.label
    try:
        if args.action == "ready":
            return 0 if receipt_ready(args.profile_env, layout) else 1
        producer = StateMirror(args.profile_env, layout)
        if args.action == "save":
            snapshot_id = producer.save()
            print(f"{tag}_STATE_SAVE_COMPLETE snapshot_id={snapshot_id}", flush=True)
            return 0
        driver = runpy.run_path("/usr/local/bin/astrabox-transcript-mirror")

        def pump() -> int:
            try:
                return producer.pump()
            except OSError as exc:
                raise OSError(f"{label} state mirror I/O failed: {exc}") from None
            except RuntimeError as exc:
                raise driver["PermanentRejection"](str(exc)) from None
            except ValueError:
                raise driver["PermanentRejection"](
                    f"{label} state mirror metadata is invalid"
                ) from None

        return int(driver["run_mirror"](pump))
    except OSError as exc:
        print(f"{tag}_STATE_MIRROR_FAILED: I/O request failed: {exc}", file=sys.stderr, flush=True)
        return 75
    except RuntimeError as exc:
        print(f"{tag}_STATE_MIRROR_FAILED: {exc}", file=sys.stderr, flush=True)
        return 78
    except Exception:
        print(
            f"{tag}_STATE_MIRROR_FAILED: invalid snapshot or profile", file=sys.stderr, flush=True
        )
        return 78
