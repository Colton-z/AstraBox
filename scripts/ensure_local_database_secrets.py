#!/usr/bin/env python3
"""Create the local Compose secret files without printing them.

The files are deployment state, not sample configuration. Existing valid
values are kept so ``compose down`` / ``up`` and host restarts do not rotate the
database accounts behind PostgreSQL's back.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import secrets
import stat
import sys
from pathlib import Path


DATABASE_SECRET_FILE_NAMES = (
    "postgres_admin_password",
    "astrabox_password",
    "litellm_password",
    "casdoor_password",
)
AUTH_SECRET_FILE_NAMES = (
    "oidc_client_secret",
    "oidc_api_client_secret",
    "casdoor_admin_password",
)
SECRET_FILE_NAMES = (*DATABASE_SECRET_FILE_NAMES, *AUTH_SECRET_FILE_NAMES)
_SECRET_PATTERN = re.compile(r"[0-9a-f]{64}")


def _existing_secret(path: Path) -> str | None:
    if not os.path.lexists(path):
        return None
    if path.is_symlink():
        raise RuntimeError(f"refusing local secret symlink: {path}")
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        raise RuntimeError(f"local secret is not a regular file: {path}")
    value = path.read_text(encoding="ascii").strip()
    if not _SECRET_PATTERN.fullmatch(value):
        raise RuntimeError(
            f"local secret has an invalid format: {path}; restore the original "
            "64-character hexadecimal value or follow the documented recovery procedure"
        )
    # Compose implements file-backed secrets as bind mounts and cannot remap
    # uid/gid for them. The parent directory is 0700 on the host; 0604 lets a
    # non-root container uid read only a file Compose explicitly grants to that
    # service, without making the host directory traversable to other users.
    path.chmod(0o604)
    return value


def _write_new_secret(path: Path) -> None:
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
        path.chmod(0o604)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def ensure_local_database_secrets(directory: Path) -> None:
    if os.path.lexists(directory) and directory.is_symlink():
        raise RuntimeError(f"refusing local secret directory symlink: {directory}")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)

    lock_path = directory / ".generation.lock"
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_descriptor, "w", encoding="ascii") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        for name in SECRET_FILE_NAMES:
            path = directory / name
            if _existing_secret(path) is None:
                _write_new_secret(path)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create persistent random secrets for the local Compose stack."
    )
    parser.add_argument(
        "--directory",
        type=Path,
        required=True,
        help="gitignored deployment-state directory that will hold the secret files",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    directory = args.directory.expanduser().absolute()
    try:
        # ``resolve()`` follows the final path component and would make the
        # symlink refusal in ``ensure_local_database_secrets`` ineffective.
        ensure_local_database_secrets(directory)
    except Exception as exc:
        print(f"local secret setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"local deployment secrets ready in {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
