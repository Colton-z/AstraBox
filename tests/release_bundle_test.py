"""The release bundle carries everything the installed Compose stack reads.

The bundle is the whole deployment a user installs: no checkout, no build
context. A file `containers/compose.yaml` mounts but the bundle omits is missing
only on an installed host, where Compose refuses to start the service that
mounts it.
"""

from __future__ import annotations

import os
import re
import subprocess
import tarfile
import tomllib
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _compose_relative_inputs() -> set[str]:
    compose = yaml.safe_load(
        (_REPO_ROOT / "containers/compose.yaml").read_text(encoding="utf-8")
    )
    inputs: set[str] = set()
    for service in compose["services"].values():
        for mount in service.get("volumes") or []:
            source = mount.split(":", 1)[0]
            if source.startswith("./"):
                inputs.add(f"containers/{source[2:]}")
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

    assert "VERSION" in members
    assert "containers/compose.yaml" in members
    missing = sorted(_compose_relative_inputs() - members)
    assert not missing, f"the installed stack would mount files the bundle lacks: {missing}"
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
                    if match := re.match(r"\s*(scripts/\S+\.sh)\b", line):
                        invoked.add(match.group(1))

    assert invoked, "the release workflows run no repository script by path"
    for script in sorted(invoked):
        assert os.access(_REPO_ROOT / script, os.X_OK), f"{script} is not executable"
