#!/usr/bin/env python3
"""Rotate the maintained local PostgreSQL roles without exposing passwords.

The target PostgreSQL container must already be running. Operators should stop
the services that consume these files first, leave PostgreSQL running, rotate,
then recreate the consumers. A durable pending directory makes an interrupted
rotation resumable: PostgreSQL receives the same values again on the next run.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path


SECRET_TO_ROLE = {
    "postgres_admin_password": "postgres",
    "astrabox_password": "astrabox",
    "litellm_password": "litellm",
    "casdoor_password": "casdoor",
}
SECRET_FILE_NAMES = tuple(SECRET_TO_ROLE)
_SECRET_PATTERN = re.compile(r"[0-9a-f]{64}")
_PENDING_DIRECTORY_NAME = ".rotation-pending"
_LOCK_FILE_NAME = ".generation.lock"

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class RotationError(RuntimeError):
    """A safe, operator-facing rotation failure."""


def _validate_directory(directory: Path) -> None:
    if os.path.lexists(directory):
        if directory.is_symlink():
            raise RotationError(f"refusing database secret directory symlink: {directory}")
        if not directory.is_dir():
            raise RotationError(f"database secret path is not a directory: {directory}")
    else:
        directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)


def _validate_active_files(directory: Path) -> None:
    """Reject unsafe existing paths while allowing genuinely lost files."""
    for name in SECRET_FILE_NAMES:
        path = directory / name
        if not os.path.lexists(path):
            continue
        if path.is_symlink():
            raise RotationError(f"refusing database secret symlink: {path}")
        if not stat.S_ISREG(path.stat().st_mode):
            raise RotationError(f"database secret is not a regular file: {path}")


def _write_candidate(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(value)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_candidate(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RotationError(f"pending database secret is not a regular file: {path}")
    value = path.read_text(encoding="ascii").strip()
    if not _SECRET_PATTERN.fullmatch(value):
        raise RotationError(
            f"pending database secret has an invalid format: {path}; "
            "restore the secret directory from backup before retrying"
        )
    path.chmod(0o600)
    return value


def _prepare_candidates(directory: Path) -> tuple[Path, dict[str, str]]:
    pending = directory / _PENDING_DIRECTORY_NAME
    if os.path.lexists(pending):
        if pending.is_symlink() or not pending.is_dir():
            raise RotationError(f"pending rotation path is not a safe directory: {pending}")
        pending.chmod(0o700)
        return pending, {
            name: _load_candidate(pending / name) for name in SECRET_FILE_NAMES
        }

    pending.mkdir(mode=0o700)
    values: dict[str, str] = {}
    try:
        for name in SECRET_FILE_NAMES:
            value = secrets.token_hex(32)
            _write_candidate(pending / name, value)
            values[name] = value
        directory_descriptor = os.open(pending, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return pending, values
    except Exception:
        shutil.rmtree(pending, ignore_errors=True)
        raise


def _inspect_postgres_container(container: str, run: RunCommand) -> None:
    if not container or container.startswith("-"):
        raise RotationError("--postgres-container must name one exact container")
    result = run(
        [
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "com.docker.compose.service"}}|{{.State.Running}}',
            container,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RotationError(f"cannot inspect PostgreSQL container {container!r}")
    service, separator, running = result.stdout.strip().partition("|")
    if not separator or service != "postgres":
        raise RotationError(
            f"container {container!r} is not the maintained Compose postgres service"
        )
    if running.lower() != "true":
        raise RotationError(
            f"PostgreSQL container {container!r} is not running; start only that "
            "existing container, then retry"
        )


def _password_sql(values: Mapping[str, str]) -> str:
    # Generated values are deliberately restricted before interpolation. The
    # SQL travels over stdin; no password appears in argv or an environment var.
    if set(values) != set(SECRET_FILE_NAMES):
        raise RotationError("database rotation candidate set is incomplete")
    statements = ["BEGIN;"]
    for secret_name, role in SECRET_TO_ROLE.items():
        value = values[secret_name]
        if not _SECRET_PATTERN.fullmatch(value):
            raise RotationError("database rotation candidate has an invalid format")
        statements.append(f'ALTER ROLE "{role}" PASSWORD \'{value}\';')
    statements.extend(("COMMIT;", ""))
    return "\n".join(statements)


def _apply_to_postgres(
    container: str,
    values: Mapping[str, str],
    run: RunCommand,
) -> None:
    command: Sequence[str] = (
        "docker",
        "exec",
        "-i",
        "--user",
        "postgres",
        container,
        "psql",
        "--username",
        "postgres",
        "--dbname",
        "postgres",
        "--set=ON_ERROR_STOP=1",
    )
    result = run(
        list(command),
        input=_password_sql(values),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Do not echo psql output: a driver or server configured to log failed
        # statements could include the ALTER ROLE text and its new passwords.
        raise RotationError(
            f"PostgreSQL rejected the role rotation (psql exit {result.returncode}); "
            "the active secret files were not changed"
        )


def _publish_candidates(
    directory: Path,
    pending: Path,
    values: Mapping[str, str],
) -> None:
    # Copy rather than move the pending files. If the process stops after
    # replacing only some active files, the complete candidate set remains and
    # the next run can safely reapply that same PostgreSQL transaction.
    for name in SECRET_FILE_NAMES:
        temporary = directory / f".{name}.{os.getpid()}.rotation.tmp"
        try:
            _write_candidate(temporary, values[name])
            temporary.chmod(0o604)
            os.replace(temporary, directory / name)
        finally:
            if os.path.lexists(temporary):
                temporary.unlink()
    directory_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    shutil.rmtree(pending)


def rotate_local_database_secrets(
    directory: Path,
    postgres_container: str,
    *,
    run: RunCommand = subprocess.run,
) -> None:
    _validate_directory(directory)
    lock_path = directory / _LOCK_FILE_NAME
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_descriptor, "w", encoding="ascii") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _validate_active_files(directory)
        _inspect_postgres_container(postgres_container, run)
        pending, values = _prepare_candidates(directory)
        _apply_to_postgres(postgres_container, values, run)
        _publish_candidates(directory, pending, values)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rotate all maintained local PostgreSQL role passwords safely."
    )
    parser.add_argument(
        "--directory",
        type=Path,
        required=True,
        help="deployment-state directory containing the Compose secret files",
    )
    parser.add_argument(
        "--postgres-container",
        required=True,
        help="exact running container name returned by scripts/compose.sh ps postgres",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    directory = args.directory.expanduser().absolute()
    try:
        rotate_local_database_secrets(directory, args.postgres_container)
    except Exception as exc:
        print(f"database credential rotation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "database credentials rotated; recreate AstraBox, LiteLLM, and Casdoor "
        "before accepting traffic"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
