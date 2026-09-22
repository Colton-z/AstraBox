"""Every launch path is installable by the install path that documents it.

``python -m astrabox.deploy.onebox`` is what ``make dev``, ``make e2e-smoke``,
``scripts/dev.sh``, ``scripts/e2e_smoke.sh`` and the container image all run, and
on the default backend it starts the bundled lifecycle server in-process. That
server lives in an OPTIONAL extra, so ``make install`` must install the extra or
the documented "install, then run" flow does not run at all — a break the unit
lane cannot see, because the unit lane deliberately never launches the stack.

Three assertions, each pinning a different half of that:

1. the extra ``astrabox.deploy.sandbox_server`` names in its missing-install
   error really exists in ``pyproject.toml`` (an unknown name would send the
   operator to a pip failure);
2. ``make install`` installs it;
3. the CI unit lane still does NOT, because that is what keeps ``import
   astrabox`` free of docker/kubernetes/redis.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from astrabox.deploy.sandbox_server import EXTRA_NAME

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_named_extra_exists_in_pyproject() -> None:
    with open(_REPO_ROOT / "pyproject.toml", "rb") as fh:
        pyproject = tomllib.load(fh)
    extras = pyproject["project"]["optional-dependencies"]
    assert EXTRA_NAME in extras, (
        f"astrabox.deploy.sandbox_server tells operators to install "
        f"astrabox[{EXTRA_NAME}], which pyproject.toml does not define"
    )


def test_make_install_installs_the_launch_extra() -> None:
    lines = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("install-py:"))
    recipe = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line)
    install_py = "\n".join(recipe)
    assert f"[dev,{EXTRA_NAME}]" in install_py, (
        "make install-py must install the sandbox-server extra: every launch "
        "target (make dev / make e2e-smoke) runs the one-box orchestrator, "
        "which cannot start the lifecycle server without it"
    )


def test_the_ci_unit_lane_stays_free_of_the_launch_extra() -> None:
    with open(_REPO_ROOT / "pyproject.toml", "rb") as fh:
        extras = tomllib.load(fh)["project"]["optional-dependencies"]
    assert set(extras[EXTRA_NAME]).isdisjoint(extras["dev"]), (
        "the dev extra must stay independent of the optional lifecycle server"
    )

    ci = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pip install -e '.[dev]'" in ci, (
        "the unit lane must install .[dev] alone — it never launches the stack, "
        "and that is what proves `import astrabox` needs no optional extra"
    )
