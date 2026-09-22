"""Snapshot Hermes SessionDB through the image's installed supplier backup API.

The home argument is HERMES_HOME, not its parent Linux home. This adapter copies
the complete state.db, including inactive messages and every native table. It
does not back up other Hermes HOME databases, configuration, or workspace files.
The platform owns profile identity, transport, persistence, and service startup.
"""

from __future__ import annotations

import os
from pathlib import Path

from hermes_cli.backup import (
    _safe_restore_db,
    copy_db_and_verify,
    verify_sqlite_integrity,
)


def _verify(path: Path) -> None:
    result = verify_sqlite_integrity(path, max_bytes=0)
    if not result.get("valid"):
        raise RuntimeError(f"Hermes SessionDB integrity check failed: {result.get('message')}")


def snapshot(home: str | os.PathLike[str], destination: str | os.PathLike[str]) -> Path:
    """Write a complete, WAL-consistent SessionDB to a new SQLite file.

    Hermes may be running. The caller supplies an existing destination directory;
    the destination file must not exist. Run as the profile's Linux owner. A fresh
    profile without state.db raises FileNotFoundError rather than inventing state.
    Returns the absolute destination after a full SQLite integrity check, or raises
    RuntimeError when the supplier cannot produce a valid snapshot. No WAL or SHM
    file needs transporting alongside the returned database.
    """
    source = Path(home).resolve(strict=True) / "state.db"
    if not source.is_file():
        raise FileNotFoundError(f"Hermes SessionDB does not exist: {source}")
    destination_path = Path(destination).absolute()
    descriptor = os.open(destination_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        if not copy_db_and_verify(source, destination_path):
            raise RuntimeError("Hermes could not snapshot SessionDB")
        _verify(destination_path)
    except Exception:
        destination_path.unlink(missing_ok=True)
        raise
    return destination_path


def restore(snapshot: str | os.PathLike[str], home: str | os.PathLike[str]) -> Path:
    """Restore a complete SQLite snapshot into the same owner's Hermes profile.

    The platform must verify the snapshot's profile ownership and keep every Hermes
    process using that HOME stopped until this function succeeds. Run as the target
    profile's Linux owner, with HERMES_HOME already provisioned. The input is an
    offline SQLite file, not a directory, and must differ from the target state.db.
    An existing target SessionDB is replaced through the supplier's restore API.

    Returns the absolute state.db path only after independent full integrity checks
    of both input and restored database. Failure raises RuntimeError; the platform
    must not start Hermes against a failed restore. This function does not start or
    stop services, authenticate ownership, or reconstruct individual session rows.
    """
    source = Path(snapshot).resolve(strict=True)
    destination = Path(home).resolve(strict=True) / "state.db"
    if not source.is_file():
        raise ValueError("Hermes SessionDB snapshot must be a SQLite file")
    if destination.exists() and source.samefile(destination):
        raise ValueError("Hermes SessionDB snapshot must differ from the restore target")
    _verify(source)
    if not _safe_restore_db(source, destination):
        raise RuntimeError("Hermes could not restore SessionDB")
    _verify(destination)
    return destination
