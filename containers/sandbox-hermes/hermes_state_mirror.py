"""Publish complete Hermes SessionDB snapshots with the image's mirror driver."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import runpy
import sys
import tempfile
from typing import Any

from hermes_native_store import snapshot
from hermes_state_bootstrap import RECEIPT_NAME, atomic_json, marker, read_json, target_for
from hermes_state_transport import save_snapshot

PENDING_NAME = "astrabox-runtime-state-pending.db"
LOCK_NAME = "astrabox-runtime-state.lock"


def receipt_ready(env_path: Path) -> bool:
    """Accept only a restore receipt for this box's initialized profile."""
    home, target = target_for(env_path)
    initialized = marker()
    receipt_path = home / RECEIPT_NAME
    if initialized is None or not receipt_path.exists():
        return False
    receipt = read_json(receipt_path)
    return (
        initialized.get("hermes_home") == str(home)
        and initialized.get("owner") == target["owner"]
        and receipt.get("hermes_home") == str(home)
        and receipt.get("owner") == target["owner"]
        and receipt.get("sandbox_id") == initialized["sandbox_id"]
    )


class StateMirror:
    """One owner's acknowledged head and one immutable, possibly in-flight file."""

    def __init__(self, env_path: Path) -> None:
        self.env_path = env_path
        self.home, _ = target_for(env_path)
        if os.geteuid() == 0 or self.home.stat().st_uid != os.geteuid():
            raise RuntimeError("Hermes state mirror must run as the profile owner")
        self._last_stamp: tuple[Any, ...] | None = None

    def _sync_home(self) -> None:
        descriptor = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _stamp(self) -> tuple[Any, ...]:
        values = []
        for name in ("state.db", "state.db-wal"):
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
            raise RuntimeError("Hermes pending snapshot is not a regular file")
        snapshot_id = save_snapshot(target, pending.read_bytes(), receipt["snapshot_id"])
        receipt["snapshot_id"] = snapshot_id
        atomic_json(self.home / RECEIPT_NAME, receipt, mode=0o600)
        self._sync_home()
        pending.unlink()
        self._sync_home()
        return snapshot_id

    def _save(self, *, force: bool) -> tuple[int, str | None]:
        descriptor = os.open(self.home / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not receipt_ready(self.env_path):
                raise RuntimeError("Hermes state mirror has no current-box restore receipt")
            _, target = target_for(self.env_path)
            receipt = read_json(self.home / RECEIPT_NAME)
            if "snapshot_id" not in receipt or (
                receipt["snapshot_id"] is not None
                and (not isinstance(receipt["snapshot_id"], str) or not receipt["snapshot_id"])
            ):
                raise RuntimeError("Hermes restore receipt has no valid snapshot predecessor")
            sent = 0
            pending = self.home / PENDING_NAME
            if pending.exists() or pending.is_symlink():
                self._commit(target, receipt)
                self._last_stamp = None
                sent += 1
            if not (self.home / "state.db").is_file():
                if force or receipt["snapshot_id"] is not None:
                    raise RuntimeError("Hermes SessionDB is missing during snapshot save")
                return sent, None
            stamp = self._stamp()
            if not force and stamp == self._last_stamp:
                return sent, receipt["snapshot_id"]
            with tempfile.TemporaryDirectory(prefix=".astrabox-state-", dir=self.home) as directory:
                temporary = Path(directory) / "state.db"
                try:
                    snapshot(self.home, temporary)
                except RuntimeError:
                    raise RuntimeError("Hermes native SessionDB snapshot failed") from None
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, pending)
                self._sync_home()
            snapshot_id = self._commit(target, receipt)
            # A write during backup must remain visible to the next poll.
            self._last_stamp = stamp
            return sent + 1, snapshot_id

    def pump(self) -> int:
        return self._save(force=False)[0]

    def save(self) -> str:
        """Acknowledge pending bytes, then force a fresh native snapshot."""
        _, snapshot_id = self._save(force=True)
        if snapshot_id is None:
            raise RuntimeError("Hermes state save returned no snapshot identity")
        return snapshot_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("ready", "mirror", "save"))
    parser.add_argument("--profile-env", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "ready":
            return 0 if receipt_ready(args.profile_env) else 1
        producer = StateMirror(args.profile_env)
        if args.action == "save":
            snapshot_id = producer.save()
            print(f"HERMES_STATE_SAVE_COMPLETE snapshot_id={snapshot_id}", flush=True)
            return 0
        driver = runpy.run_path("/usr/local/bin/astrabox-transcript-mirror")

        def pump() -> int:
            try:
                return producer.pump()
            except OSError as exc:
                raise OSError(f"Hermes state mirror I/O failed: {exc}") from None
            except RuntimeError as exc:
                raise driver["PermanentRejection"](str(exc)) from None
            except ValueError:
                raise driver["PermanentRejection"](
                    "Hermes state mirror metadata is invalid"
                ) from None

        return int(driver["run_mirror"](pump))
    except OSError as exc:
        print(f"HERMES_STATE_MIRROR_FAILED: I/O request failed: {exc}", file=sys.stderr, flush=True)
        return 75
    except RuntimeError as exc:
        print(f"HERMES_STATE_MIRROR_FAILED: {exc}", file=sys.stderr, flush=True)
        return 78
    except Exception:
        print(
            "HERMES_STATE_MIRROR_FAILED: invalid snapshot or profile", file=sys.stderr, flush=True
        )
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
