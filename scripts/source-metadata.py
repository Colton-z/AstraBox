#!/usr/bin/env python3
"""Run a command with the checkout's entry points in place of the image's.

The source-development overlay mounts the checkout's ``astrabox`` package over
the one installed in the server image. Providers, engines and console scripts
are found through the installed distribution's ``entry_points.txt``, which the
mount does not replace, so an entry point added in the checkout would not
exist for the running server. This writes a copy of the installed
distribution's metadata with the entry points of the checkout's
``pyproject.toml``, puts it first on ``PYTHONPATH`` (the first distribution of
a name wins), and executes the command.

Usage: source-metadata.py <pyproject.toml> -- <command> [args...]
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import tomllib
from importlib.metadata import distribution
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        sys.exit("usage: source-metadata.py <pyproject.toml> -- <command> [args...]")
    project = tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["project"]
    groups = dict(project.get("entry-points") or {})
    if project.get("scripts"):
        groups["console_scripts"] = project["scripts"]

    installed = distribution(project["name"])
    root = Path(tempfile.mkdtemp(prefix="astrabox-source-metadata-"))
    target = root / f"{project['name']}-{installed.version}.dist-info"
    target.mkdir()
    metadata = installed.read_text("METADATA")
    if metadata is None:
        sys.exit(f"the installed {project['name']} distribution has no METADATA")
    (target / "METADATA").write_text(metadata, encoding="utf-8")
    lines: list[str] = []
    for group in sorted(groups):
        lines.append(f"[{group}]")
        lines.extend(f"{name} = {value}" for name, value in sorted(groups[group].items()))
        lines.append("")
    (target / "entry_points.txt").write_text("\n".join(lines), encoding="utf-8")

    existing = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else str(root)
    command = sys.argv[3:]
    executable = shutil.which(command[0])
    if executable is None:
        sys.exit(f"command not found: {command[0]}")
    os.execv(executable, command)


if __name__ == "__main__":
    main()
