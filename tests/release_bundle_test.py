"""The release bundle carries everything the installed Compose stack reads.

The bundle is the whole deployment a user installs: no checkout, no build
context. A file a Compose file the installer runs mounts, but the bundle omits,
is missing only on an installed host, where Compose refuses to start the
service that mounts it. That covers the team-login overlay as much as the base
stack: an installation that turns team login on runs it from the bundle.
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
import tarfile
import tomllib
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_INSTALLER = _REPO_ROOT / "scripts/install.sh"


def _installer_compose_files() -> list[str]:
    """Every Compose file the installer can run, as repository paths.

    Read from the installer itself: the team-login setting it writes to
    COMPOSE_FILE names the base file and the overlay, relative to containers/.
    """
    result = subprocess.run(
        ["bash", "-c", f'source "{_INSTALLER}"; printf %s "$TEAM_LOGIN_COMPOSE_FILE"'],
        check=True,
        capture_output=True,
        text=True,
    )
    return [f"containers/{name}" for name in result.stdout.split(":")]


def _compose_relative_inputs(compose_file: str) -> set[str]:
    compose = yaml.safe_load((_REPO_ROOT / compose_file).read_text(encoding="utf-8"))
    directory = posixpath.dirname(compose_file)
    inputs: set[str] = set()
    for service in compose["services"].values():
        for mount in service.get("volumes") or []:
            source = mount.split(":", 1)[0]
            if source.startswith(("./", "../")):
                inputs.add(posixpath.normpath(posixpath.join(directory, source)))
    for secret in (compose.get("secrets") or {}).values():
        # The secret files are generated per installation, not shipped; their
        # directory is what the installer creates.
        assert "${ASTRABOX_LOCAL_DATABASE_SECRET_DIR" in secret["file"], secret
    return inputs


def test_the_bundle_holds_every_file_compose_mounts(tmp_path: Path) -> None:
    subprocess.run(
        [str(_REPO_ROOT / "scripts/build-release-bundle.sh"), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    version = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    archive = tmp_path / f"astrabox-deploy-{version}.tar.gz"

    with tarfile.open(archive) as bundle:
        members = {
            name.split("/", 1)[1]
            for name in bundle.getnames()
            if "/" in name and not name.endswith("/")
        }
        modes = {
            member.name.split("/", 1)[1]: member.mode
            for member in bundle.getmembers()
            if member.isfile()
        }

    compose_files = _installer_compose_files()
    assert compose_files == ["containers/compose.yaml", "containers/compose.sso.yaml"]
    assert "VERSION" in members
    for compose_file in compose_files:
        assert compose_file in members, f"the bundle lacks {compose_file}"
        missing = sorted(_compose_relative_inputs(compose_file) - members)
        assert not missing, f"{compose_file} would mount files the bundle lacks: {missing}"
    # The containers that read them run as other users than the installing one.
    assert all(mode == 0o644 for mode in modes.values()), modes

    checksum = (tmp_path / f"{archive.name}.sha256").read_text(encoding="utf-8")
    assert archive.name in checksum


def test_the_scripts_the_release_workflows_run_directly_are_executable() -> None:
    """A checkout gives a script the mode git recorded for it.

    A file committed without its executable bit passes every check that runs
    it through ``bash`` and fails only in the job that calls it by path, which
    for the release is the job that would publish the installer's bundle.
    """
    invoked: set[str] = set()
    for name in ("release.yml", "installer.yml"):
        workflow = yaml.safe_load(
            (_REPO_ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        )
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                for line in str(step.get("run") or "").splitlines():
                    if match := re.match(r"\s*(scripts/\S+\.(?:sh|py))\b", line):
                        invoked.add(match.group(1))

    assert invoked, "the release workflows run no repository script by path"
    for script in sorted(invoked):
        assert os.access(_REPO_ROOT / script, os.X_OK), f"{script} is not executable"
