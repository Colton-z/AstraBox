"""GitHub-hosted image inputs are checksum-pinned stages the build host can serve.

A GitHub download inside a build step is fetched again by every build, and a
rate-limited or changed response fails the build or changes the image. Each one
is instead a ``FROM scratch AS <name>`` stage holding one
``ADD --checksum=sha256:...``: a standalone build downloads and verifies it
there, and a build host with a verified cache serves the same bytes through
``--build-context`` (pinned-build-sources.py).
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/pinned-build-sources.py"


def _load() -> ModuleType:
    name = "pinned_build_sources"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pinned = _load()


def _dockerfiles() -> list[Path]:
    found = sorted(REPO.glob("containers/*/Dockerfile"))
    assert found, "the repository has container Dockerfiles"
    return found


def _make_recipe_for(dockerfile: str) -> str:
    """Return the Make recipe that builds *dockerfile*."""

    lines = (REPO / "Makefile").read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not re.match(r"^[A-Za-z0-9_-]+:", line):
            continue
        recipe = []
        for body in lines[index + 1 :]:
            if not body.startswith("\t") and not (recipe and recipe[-1].rstrip().endswith("\\")):
                break
            recipe.append(body)
        text = "\n".join(recipe)
        if f"-f {dockerfile}" in text:
            return text
    raise AssertionError(f"no Make target builds {dockerfile}")


def test_every_github_download_in_an_image_is_a_checksum_pinned_stage() -> None:
    served: dict[str, list[str]] = {}
    for dockerfile in _dockerfiles():
        sources, violations = pinned.inspect_dockerfile(dockerfile.read_text(encoding="utf-8"))
        assert violations == [], f"{dockerfile.relative_to(REPO)}: {violations}"
        if sources:
            relative = dockerfile.relative_to(REPO).as_posix()
            served[relative] = [source.stage for source in sources]
            # A build host can only serve a pinned stage through the Make
            # target that builds the image.
            assert "$(DOCKER_BUILD_CONTEXTS)" in _make_recipe_for(relative), relative
    # The images' GitHub downloads, each served by its own stage.
    assert served == {
        "containers/sandbox-hermes/Dockerfile": ["hermes-source"],
        "containers/workspace-mounter/Dockerfile": ["mergerfs-release", "mergerfs-license"],
    }


@pytest.mark.parametrize(
    ("dockerfile", "reason"),
    [
        (
            "FROM python:3.12-slim\nRUN curl -fsSL https://github.com/o/r/archive/abc.tar.gz -o /x\n",
            "RUN downloads",
        ),
        (
            "FROM python:3.12-slim\nADD https://raw.githubusercontent.com/o/r/abc/LICENSE /LICENSE\n",
            "ADD downloads",
        ),
        (
            "FROM scratch AS src\nADD https://codeload.github.com/o/r/tar.gz/abc /src.tar.gz\n",
            "exactly one --checksum",
        ),
        (
            "FROM scratch AS src\nADD --checksum=sha256:abc https://github.com/o/r/x.tgz /x.tgz\n",
            "exactly one --checksum",
        ),
        (
            "FROM scratch AS src\n"
            f"ADD --checksum=sha256:{'0' * 64} https://github.com/o/r/x.tgz /dir/x.tgz\n",
            "one file at the stage root",
        ),
    ],
)
def test_the_rule_rejects_a_github_download_that_is_not_a_pinned_stage(
    dockerfile: str, reason: str
) -> None:
    _, violations = pinned.inspect_dockerfile(dockerfile)
    assert any(reason in violation for violation in violations), violations


_PAYLOAD = b"pinned source bytes\n"
_DIGEST = hashlib.sha256(_PAYLOAD).hexdigest()


def _source_tree(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "containers/example").mkdir(parents=True)
    (root / "containers/example/Dockerfile").write_text(
        "FROM scratch AS example-source\n"
        f"ADD --checksum=sha256:{_DIGEST} https://github.com/o/r/archive/abc.tar.gz /example.tar.gz\n"
        "FROM python:3.12-slim\n"
        "RUN --mount=type=bind,from=example-source,source=/example.tar.gz,target=/x true\n",
        encoding="utf-8",
    )
    return root


def _count_downloads(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> list[str]:
    calls: list[str] = []

    def download(url: str, target: Path) -> None:
        calls.append(url)
        target.write_bytes(payload)

    monkeypatch.setattr(pinned, "_download", download)
    return calls


def test_a_pinned_source_is_fetched_once_and_served_as_a_build_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _source_tree(tmp_path)
    cache = tmp_path / "cache"
    dockerfile = root / "containers/example/Dockerfile"
    calls = _count_downloads(monkeypatch, _PAYLOAD)

    with pytest.raises(pinned.PinnedSourceError, match="no cache entry"):
        pinned.preflight(dockerfile, cache)

    assert [row["action"] for row in pinned.fetch(dockerfile, cache)] == ["fetched"]
    assert [row["action"] for row in pinned.fetch(dockerfile, cache)] == ["cached"]
    assert calls == ["https://github.com/o/r/archive/abc.tar.gz"], "the second fetch is a cache hit"

    _, flags = pinned.preflight(dockerfile, cache)
    assert flags == [f"--build-context example-source={cache / _DIGEST}"]
    assert (cache / _DIGEST / "example.tar.gz").read_bytes() == _PAYLOAD


def test_a_cache_entry_with_the_wrong_bytes_is_an_error_not_a_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _source_tree(tmp_path)
    cache = tmp_path / "cache"
    dockerfile = root / "containers/example/Dockerfile"
    entry = cache / _DIGEST / "example.tar.gz"
    entry.parent.mkdir(parents=True)
    entry.write_bytes(b"tampered\n")
    calls = _count_downloads(monkeypatch, _PAYLOAD)

    with pytest.raises(pinned.PinnedSourceError, match="not refetched"):
        pinned.fetch(dockerfile, cache)
    with pytest.raises(pinned.PinnedSourceError, match="not the pinned"):
        pinned.preflight(dockerfile, cache)
    assert calls == []
    assert entry.read_bytes() == b"tampered\n"


def test_a_download_with_the_wrong_bytes_leaves_no_cache_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _source_tree(tmp_path)
    cache = tmp_path / "cache"
    dockerfile = root / "containers/example/Dockerfile"
    _count_downloads(monkeypatch, b"something else\n")

    with pytest.raises(pinned.PinnedSourceError, match="delivered sha256"):
        pinned.fetch(dockerfile, cache)
    assert list((cache / _DIGEST).iterdir()) == []
