#!/usr/bin/env python3
"""Create the local Compose secret files without printing them.

The files are deployment state, not sample configuration. Existing valid
values are kept so ``compose down`` / ``up`` and host restarts do not rotate the
database accounts behind PostgreSQL's back. The generation rules are shared
with the all-in-one image in ``astrabox/deploy/secret_files.py``, loaded here
by path because a host running this script has no AstraBox installation.
"""

from __future__ import annotations

import argparse
import importlib.util
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
    "casdoor_builtin_admin_password",
)
SECRET_FILE_NAMES = (*DATABASE_SECRET_FILE_NAMES, *AUTH_SECRET_FILE_NAMES)
# Compose implements file-backed secrets as bind mounts and cannot remap
# uid/gid for them. The parent directory is 0700 on the host; 0604 lets a
# non-root container uid read only a file Compose explicitly grants to that
# service, without making the host directory traversable to other users.
COMPOSE_SECRET_MODE = 0o604


def _load_secret_files():
    path = Path(__file__).resolve().parent.parent / "astrabox" / "deploy" / "secret_files.py"
    specification = importlib.util.spec_from_file_location("astrabox_secret_files", path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load the secret generation rules from {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


_SECRET_FILES = _load_secret_files()


def _existing_secret(path: Path) -> str | None:
    return _SECRET_FILES.read_existing_secret(path, mode=COMPOSE_SECRET_MODE)


def ensure_local_database_secrets(directory: Path) -> None:
    _SECRET_FILES.ensure_secret_files(directory, SECRET_FILE_NAMES, mode=COMPOSE_SECRET_MODE)


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
