"""Every default image is one the release publishes, at this release's version.

A deployment that configures no image runs
``<ASTRABOX_IMAGE_PREFIX><component>:<ASTRABOX_IMAGE_TAG>``, whose defaults are
the published prefix and this package's version. Two failure modes stay
invisible until a user installs a release: a default naming a component the
release workflow does not publish, and a Compose file whose own default prefix
drifts from the one the server uses for the sandbox images beside it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

import astrabox
from astrabox.config.release_images import (
    IMAGE_PREFIX_ENV,
    IMAGE_TAG_ENV,
    PUBLISHED_IMAGE_PREFIX,
    release_image,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _shipped_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host that overrides the images must not change what this file reads."""
    for name in (IMAGE_PREFIX_ENV, IMAGE_TAG_ENV, "ASTRABOX_AGENT_IMAGE"):
        monkeypatch.delenv(name, raising=False)


def test_the_package_version_is_pyprojects() -> None:
    project = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert astrabox.__version__ == project["project"]["version"], (
        "the release workflow tags the images with pyproject's version and the "
        "server names them with this one; they are the same number"
    )


def test_an_unconfigured_deployment_runs_its_own_releases_images() -> None:
    assert release_image("sandbox-codex") == (
        f"{PUBLISHED_IMAGE_PREFIX}sandbox-codex:{astrabox.__version__}"
    )


def test_a_mirror_and_a_checkout_move_every_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(IMAGE_PREFIX_ENV, "registry.example.com/team/astrabox-")
    assert release_image("server") == (
        f"registry.example.com/team/astrabox-server:{astrabox.__version__}"
    )

    monkeypatch.setenv(IMAGE_PREFIX_ENV, "astrabox/")
    monkeypatch.setenv(IMAGE_TAG_ENV, "latest")
    assert release_image("sandbox-hermes") == "astrabox/sandbox-hermes:latest"


def _published_components() -> set[str]:
    workflow = yaml.safe_load(
        (_REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]
    built = set(jobs["build"]["strategy"]["matrix"]["component"])
    # The all-in-one image has its own job: it is built FROM the server image
    # each architecture just pushed, so it cannot share the build matrix.
    all_in_one = jobs["build-all-in-one"]
    assert "build" in _needs(all_in_one), "the all-in-one is built from the server digest"
    assert any(
        (step.get("with") or {}).get("file") == "containers/all-in-one/Dockerfile"
        for step in all_in_one["steps"]
    )
    built.add("all-in-one")
    merged = set(jobs["merge"]["strategy"]["matrix"]["component"])
    assert built == merged, "release.yml builds and tags different images"
    # A failed build of any image, the all-in-one included, must skip every
    # merge, so a release never tags half its images.
    assert {"build", "build-all-in-one"} <= _needs(jobs["merge"])
    return built


def test_nothing_is_published_before_the_servers_gateway_answers() -> None:
    """Every engine reaches its model through the gateway in the server image,
    so a server whose gateway fails its first request is a release no
    installation can use. The release checks the image it just built, by
    digest, in the job that every tagging or release-publishing job needs."""
    workflow = yaml.safe_load(
        (_REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]

    def runs(job: dict, text: str) -> bool:
        return any(text in str(step.get("run") or "") for step in job.get("steps", []))

    checking = [
        name for name, job in jobs.items() if runs(job, "scripts/embedded-gateway-acceptance.py")
    ]
    assert len(checking) == 1, f"jobs running the gateway acceptance: {checking}"
    check_job = jobs[checking[0]]
    step = next(
        step
        for step in check_job["steps"]
        if "scripts/embedded-gateway-acceptance.py" in str(step.get("run") or "")
    )
    assert "server" in check_job["strategy"]["matrix"]["component"]
    assert "matrix.component == 'server'" in str(step.get("if") or "")
    assert "steps.build.outputs.digest" in str(step.get("env") or {}) + str(step["run"])
    assert any(built.get("id") == "build" for built in check_job["steps"])

    def needs(name: str) -> set[str]:
        direct = jobs[name].get("needs") or []
        direct = [direct] if isinstance(direct, str) else list(direct)
        return set(direct).union(*(needs(parent) for parent in direct))

    publishing = [
        name
        for name, job in jobs.items()
        if runs(job, "imagetools create") or runs(job, "gh release")
    ]
    assert publishing, "release.yml publishes nothing this test recognises"
    for name in publishing:
        assert checking[0] in needs(name), f"{name} can publish without the gateway check"


def _needs(job: dict) -> set[str]:
    needs = job.get("needs") or []
    return {needs} if isinstance(needs, str) else set(needs)


def test_the_release_publishes_every_image_in_containers() -> None:
    components = {
        path.parent.name for path in (_REPO_ROOT / "containers").glob("*/Dockerfile")
    }

    assert _published_components() == components


def test_every_engines_default_image_is_published() -> None:
    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.core.service.orchestrator.engine.registry import (
        registered_engine_adapters,
    )
    from astrabox.providers import register_builtin_providers

    register_builtin_providers()
    published = _published_components()
    images = [
        adapter.capabilities.default_runtime_image
        for adapter in registered_engine_adapters().values()
    ]
    images.append(load_astrabox_settings().workspace_mounter_image)

    for image in images:
        assert image is not None
        assert image.startswith(PUBLISHED_IMAGE_PREFIX), image
        component, _, tag = image[len(PUBLISHED_IMAGE_PREFIX) :].partition(":")
        assert component in published, f"{component} has no release.yml row"
        assert tag == astrabox.__version__, image


def test_compose_names_the_server_image_the_same_way() -> None:
    """An installation sets only the tag, so both prefixes must default alike."""
    compose = yaml.safe_load(
        (_REPO_ROOT / "containers/compose.yaml").read_text(encoding="utf-8")
    )

    assert compose["services"]["server"]["image"] == (
        "${ASTRABOX_SERVER_IMAGE:-"
        f"${{ASTRABOX_IMAGE_PREFIX:-{PUBLISHED_IMAGE_PREFIX}}}server:"
        "${ASTRABOX_IMAGE_TAG:-}}"
    )
