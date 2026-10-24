"""Pack and restore the Hermes profile state that lives beside SessionDB.

SessionDB (``state.db``) is the conversation store and is mirrored on its own.
This module covers what Hermes keeps for the profile as a whole and would
otherwise lose with the box: the built-in memory files, skills the user created
or installed, scheduled jobs and their run history, and Hermes' other
per-profile user stores (``hermes_cli/backup.py`` ``_QUICK_STATE_FILES`` at the
pinned commit). Configuration, credentials and messaging-gateway pairing are
written by the platform or unused here and are left out. The complete Skills
tree includes customized defaults and Hermes' bundled-skill origin manifest,
so its native sync can preserve user edits after restoring into a new image.

Run as the profile's Linux owner. The archive is a gzip tar with a manifest
naming what it governs; restore replaces exactly those paths.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
from typing import Any

from hermes_cli.backup import (
    _safe_restore_db,
    _should_exclude,
    copy_db_and_verify,
    verify_sqlite_integrity,
)

MANIFEST_NAME = "astrabox-profile.json"
#: Directories restored as a whole.
DIRECTORIES = ("memories", "kanban/boards", "a2a_conversations", "skills")
#: Plain files.
FILES = ("cron/jobs.json", "a2a_audit.jsonl")
#: SQLite stores, copied and restored through Hermes' own backup API.
DATABASES = (
    "cron/executions.db",
    "memory_store.db",
    "projects.db",
    "kanban.db",
    "verification_evidence.db",
)
SKILLS = "skills"


def _regular_files(directory: Path) -> list[Path]:
    if directory.name == SKILLS:
        # Apply Hermes' full-backup policy relative to HERMES_HOME: archived
        # Skills and origin metadata stay; dependency trees and caches do not.
        files = []
        for parent, directories, names in os.walk(directory, followlinks=False):
            base = Path(parent)
            directories[:] = [
                name for name in directories
                if not _should_exclude((base / name).relative_to(directory.parent))
            ]
            files.extend(
                base / name for name in names
                if (base / name).is_file() and not (base / name).is_symlink()
                and not _should_exclude((base / name).relative_to(directory.parent))
            )
        return sorted(files)
    return sorted(
        path for path in directory.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def stamp(home: Path) -> tuple[Any, ...]:
    """A cheap fingerprint that changes whenever packed content may have changed."""

    values: list[Any] = []
    paths: list[Path] = []
    for name in DIRECTORIES:
        if (home / name).is_dir():
            paths.extend(_regular_files(home / name))
    for name in (*FILES, *DATABASES, *(f"{db}-wal" for db in DATABASES)):
        if (home / name).is_file():
            paths.append(home / name)
    for path in paths:
        metadata = path.stat()
        values.append((str(path.relative_to(home)), metadata.st_size, metadata.st_mtime_ns))
    return tuple(values)


def _add_file(archive: tarfile.TarFile, source: Path, name: str) -> None:
    data = source.read_bytes()
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = source.stat().st_mode & 0o777
    info.mtime = int(source.stat().st_mtime)
    archive.addfile(info, io.BytesIO(data))


def pack(home: str | os.PathLike[str]) -> bytes:
    """Return the profile state of ``home`` (HERMES_HOME) as one archive."""

    root = Path(home).resolve(strict=True)
    manifest: dict[str, list[str]] = {
        "directories": [], "files": [], "databases": [], "skills": [],
    }
    buffer = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix=".astrabox-profile-", dir=root) as scratch, \
            tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in DIRECTORIES:
            if (root / name).is_dir():
                manifest["directories"].append(name)
                for path in _regular_files(root / name):
                    _add_file(archive, path, path.relative_to(root).as_posix())
        for name in FILES:
            if (root / name).is_file():
                manifest["files"].append(name)
                _add_file(archive, root / name, name)
        for name in DATABASES:
            if not (root / name).is_file():
                continue
            copy = Path(scratch) / name.replace("/", "__")
            if not copy_db_and_verify(root / name, copy):
                raise RuntimeError(f"Hermes could not snapshot {name}")
            manifest["databases"].append(name)
            _add_file(archive, copy, name)
        encoded = json.dumps(manifest, sort_keys=True).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(encoded)
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(encoded))
    return buffer.getvalue()


def _governed(name: str, manifest: dict[str, list[str]]) -> bool:
    if name in manifest["files"] or name in manifest["databases"]:
        return True
    prefixes = [*manifest["directories"], *(f"{SKILLS}/{skill}" for skill in manifest["skills"])]
    return any(name.startswith(prefix + "/") for prefix in prefixes)


def restore(payload: bytes, home: str | os.PathLike[str]) -> None:
    """Replace the paths ``payload`` governs in ``home`` (HERMES_HOME).

    Hermes must not be running on this profile. Its next native Skill sync
    updates pristine defaults and retains edited ones using the saved manifest.
    """

    root = Path(home).resolve(strict=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = archive.getmembers()
        manifest_member = next((m for m in members if m.name == MANIFEST_NAME), None)
        if manifest_member is None:
            raise RuntimeError("Hermes profile archive has no manifest")
        manifest = json.loads(archive.extractfile(manifest_member).read())  # type: ignore[union-attr]
        for key in ("directories", "files", "databases", "skills"):
            if not isinstance(manifest.get(key), list):
                raise RuntimeError("Hermes profile archive manifest is invalid")
        with tempfile.TemporaryDirectory(prefix=".astrabox-profile-", dir=root) as scratch:
            staged = Path(scratch)
            for member in members:
                if member.name == MANIFEST_NAME:
                    continue
                path = PurePosixPath(member.name)
                if (
                    not member.isfile()
                    or path.is_absolute()
                    or ".." in path.parts
                    or not _governed(member.name, manifest)
                ):
                    raise RuntimeError(f"Hermes profile archive holds an unexpected entry: {member.name}")
                target = staged / path
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("wb") as sink:  # type: ignore[union-attr]
                    shutil.copyfileobj(source, sink)
                target.chmod(member.mode & 0o700 | 0o600)
            for name in [*manifest["directories"], *(f"{SKILLS}/{s}" for s in manifest["skills"])]:
                destination = root / name
                if destination.exists():
                    shutil.rmtree(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if (staged / name).exists():
                    os.replace(staged / name, destination)
                else:
                    destination.mkdir()
            for name in manifest["files"]:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged / name, root / name)
            for name in manifest["databases"]:
                result = verify_sqlite_integrity(staged / name, max_bytes=0)
                if not result.get("valid"):
                    raise RuntimeError(f"Hermes profile store {name} failed its integrity check")
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                if not _safe_restore_db(staged / name, root / name):
                    raise RuntimeError(f"Hermes could not restore {name}")
