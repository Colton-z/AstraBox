"""Generate-once deployment secret files: 64 hexadecimal characters each.

Two deployments create these files. ``scripts/ensure_local_database_secrets.py``
writes the Compose stack's service secrets on the host, and the all-in-one
image (:mod:`astrabox.deploy.all_in_one`) writes its database passwords into
the data volume. Both follow the same rules, so both use this module:

* an existing valid value is kept, never rewritten: the database roles were
  created with it, and a new value would lock the platform out;
* a symlink, a non-regular file or a malformed value is refused rather than
  replaced, because each is either tampering or a restore gone wrong, and
  overwriting it would destroy the evidence and the credential together;
* a new value is written to a temporary file opened with ``O_EXCL`` and
  renamed into place, so a crash never leaves a truncated secret;
* one ``flock`` on the directory serialises concurrent writers.

The file mode is the caller's. Compose implements file-backed secrets as bind
mounts and cannot remap their owner, so the script grants ``0604`` inside a
``0700`` host directory; the all-in-one owns its volume and uses ``0600``.

Standard library only: the script loads this file by path on a host where
AstraBox is not installed.
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Iterable

SECRET_PATTERN = re.compile(r"[0-9a-f]{64}")
LOCK_FILENAME = ".generation.lock"


def read_existing_secret(path: Path, *, mode: int) -> str | None:
    """Return a valid existing secret with its mode reset, or ``None`` if absent."""

    if not os.path.lexists(path):
        return None
    if path.is_symlink():
        raise RuntimeError(f"refusing secret symlink: {path}")
    if not stat.S_ISREG(path.stat().st_mode):
        raise RuntimeError(f"secret is not a regular file: {path}")
    value = path.read_text(encoding="ascii").strip()
    if not SECRET_PATTERN.fullmatch(value):
        raise RuntimeError(
            f"secret has an invalid format: {path}; restore the original "
            "64-character hexadecimal value or follow the documented recovery procedure"
        )
    path.chmod(mode)
    return value


def write_new_secret(path: Path, *, mode: int) -> None:
    """Create ``path`` with a fresh random value; never replaces a concurrent writer's file."""

    value = secrets.token_hex(32)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(value)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def ensure_secret_files(directory: Path, names: Iterable[str], *, mode: int) -> None:
    """Create every missing secret in ``directory`` and validate the existing ones."""

    if os.path.lexists(directory) and directory.is_symlink():
        raise RuntimeError(f"refusing secret directory symlink: {directory}")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)

    lock_descriptor = os.open(directory / LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_descriptor, "w", encoding="ascii") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        for name in names:
            path = directory / name
            if read_existing_secret(path, mode=mode) is None:
                write_new_secret(path, mode=mode)


__all__ = [
    "LOCK_FILENAME",
    "SECRET_PATTERN",
    "ensure_secret_files",
    "read_existing_secret",
    "write_new_secret",
]
