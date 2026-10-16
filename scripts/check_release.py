#!/usr/bin/env python3
"""What a deployment resolves by default is what a release ships.

Each rule is a failure that stays invisible until a user installs a release:

* **Every default image is published.** A deployment that configures no image
  runs ``<prefix><component>:<version>`` (``astrabox.config.release_images``).
  Each engine's default runtime image and the workspace mounter's must be a
  component ``.github/workflows/release.yml`` builds and tags, at this package's
  version, and the workflow's build and merge matrices must name the same
  images, the all-in-one included.
* **Compose names the same registry.** The Compose files repeat the published
  prefix as their default ``ASTRABOX_IMAGE_PREFIX``; one that drifted would run
  a server from one registry beside sandbox images from another.
* **Every built-in storage provider is discoverable.** A provider registered
  in-tree but missing from ``[project.entry-points."astrabox.providers.storage"]``
  resolves in a checkout and not from an installed wheel.

Run it directly:

    .venv/bin/python scripts/check_release.py
"""

from __future__ import annotations

import importlib
import pkgutil
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT))


def _needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs") or []
    return {needs} if isinstance(needs, str) else set(needs)


def published_components() -> tuple[set[str], list[str]]:
    """The images release.yml publishes, and what is wrong with how it does."""
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    problems: list[str] = []
    built = set(jobs["build"]["strategy"]["matrix"]["component"])
    # The all-in-one has its own job: it is built FROM the server image each
    # architecture just pushed, so it cannot share the build matrix.
    all_in_one = jobs["build-all-in-one"]
    if "build" not in _needs(all_in_one):
        problems.append("build-all-in-one does not need build, so it is not built from the server digest")
    if not any(
        (step.get("with") or {}).get("file") == "containers/all-in-one/Dockerfile"
        for step in all_in_one["steps"]
    ):
        problems.append("build-all-in-one does not build containers/all-in-one/Dockerfile")
    built.add("all-in-one")
    execd = jobs["build-execd"]
    built.update(execd["strategy"]["matrix"]["component"])
    if "build-execd" not in _needs(jobs["build"]):
        problems.append("runtime images do not wait for their execd artifact")
    merged = set(jobs["merge"]["strategy"]["matrix"]["component"])
    if built != merged:
        problems.append(f"built {sorted(built)} but tags {sorted(merged)}")
    # A failed build of any image must skip every merge, so a release never
    # tags half its images.
    if not {"build-execd", "build", "build-all-in-one"} <= _needs(jobs["merge"]):
        problems.append("merge does not wait for all image build jobs")
    return built, problems


def unpublished_default_images() -> list[str]:
    import astrabox
    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.config.release_images import PUBLISHED_IMAGE_PREFIX
    from astrabox.deploy.sandbox_server import execd_image
    from astrabox.core.service.orchestrator.engine.registry import registered_engine_adapters
    from astrabox.providers import register_builtin_providers

    register_builtin_providers()
    published, problems = published_components()
    images = [
        adapter.capabilities.default_runtime_image
        for adapter in registered_engine_adapters().values()
    ]
    images.append(load_astrabox_settings().workspace_mounter_image)
    images.append(execd_image())
    for image in images:
        if not image or not image.startswith(PUBLISHED_IMAGE_PREFIX):
            problems.append(f"{image!r} is not under {PUBLISHED_IMAGE_PREFIX}")
            continue
        component, _, tag = image[len(PUBLISHED_IMAGE_PREFIX):].partition(":")
        if component not in published:
            problems.append(f"{image}: {component} has no release.yml row")
        if tag != astrabox.__version__:
            problems.append(f"{image}: tagged {tag!r}, this release is {astrabox.__version__!r}")
    return problems


def drifted_compose_prefixes() -> list[str]:
    from astrabox.config.release_images import PUBLISHED_IMAGE_PREFIX

    problems: list[str] = []
    pattern = re.compile(r"\$\{ASTRABOX_IMAGE_PREFIX:-([^}]+)\}")
    for path in sorted((REPO_ROOT / "containers").glob("compose*.yaml")):
        for prefix in pattern.findall(path.read_text(encoding="utf-8")):
            if prefix != PUBLISHED_IMAGE_PREFIX:
                problems.append(
                    f"containers/{path.name} defaults the prefix to {prefix!r}, "
                    f"the server uses {PUBLISHED_IMAGE_PREFIX!r}"
                )
    return problems


def undiscoverable_storage_providers() -> list[str]:
    import astrabox.providers.storage as storage_package
    from astrabox.seams.storage import ENTRY_POINT_GROUP, StorageProvider

    builtin: set[str] = set()
    for module in pkgutil.iter_modules(storage_package.__path__):
        loaded = importlib.import_module(f"{storage_package.__name__}.{module.name}")
        for attribute in vars(loaded).values():
            if (
                isinstance(attribute, type)
                and issubclass(attribute, StorageProvider)
                and attribute is not StorageProvider
                and attribute.__module__ == loaded.__name__
            ):
                builtin.add(attribute.name)
    if not builtin:
        # A walk that found nothing would pass whatever the table says.
        return ["no built-in storage provider found under astrabox/providers/storage"]
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(manifest["project"]["entry-points"][ENTRY_POINT_GROUP])
    return [f"{name} is not in [project.entry-points.{ENTRY_POINT_GROUP!r}]" for name in sorted(builtin - declared)]


def main() -> int:
    failures = [
        ("default image(s) not published by this release", unpublished_default_images()),
        ("Compose image prefix(es) that differ from the published one", drifted_compose_prefixes()),
        ("built-in storage provider(s) an installed wheel cannot discover", undiscoverable_storage_providers()),
    ]
    failed = False
    for reason, items in failures:
        if not items:
            continue
        failed = True
        print(f"\n{len(items)} {reason}:")
        for item in items:
            print(f"  {item}")
    if failed:
        print("\nrelease contents FAILED")
        return 1
    print("release contents OK — default images published, prefixes agree, providers discoverable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
