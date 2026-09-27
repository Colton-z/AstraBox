#!/usr/bin/env python3
"""Serve checksum-pinned Dockerfile source stages from the build host's cache.

A GitHub-hosted build input is a stage of its own in its Dockerfile::

    FROM scratch AS hermes-source
    ADD --checksum=sha256:<digest> https://github.com/... /hermes-source.tar.gz

A standalone build (release.yml, a user building from source) downloads it
there and BuildKit verifies the digest. A build host running build-release.sh
fetches each pinned URL once into ``--cache``, verifies it against the same digest, and
passes the entry to ``docker build --build-context <stage>=<dir>``. BuildKit
replaces a stage with a named context of the same name, so both paths build
from the same verified bytes and GitHub is asked at most once per digest.

``fetch`` downloads only entries that are absent. An entry whose bytes do not
match its digest is an error to investigate, never refetched over.
``preflight`` downloads nothing: it fails when any pinned stage has no verified
entry, and otherwise prints the ``--build-context`` flags for the build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path


GITHUB_HOSTS = frozenset(
    {
        "github.com",
        "codeload.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
    }
)
_URL_RE = re.compile(r"https?://[^\s\"'\\)]+")
_CHECKSUM_RE = re.compile(r"^--checksum=sha256:([0-9a-f]{64})$")
_STAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_FILE_NAME_RE = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9._-]*)$")
_FETCH_TIMEOUT_SECONDS = 120


class PinnedSourceError(RuntimeError):
    """A Dockerfile, cache entry or download does not satisfy the pinned contract."""


@dataclass(frozen=True)
class PinnedSource:
    stage: str
    url: str
    sha256: str
    name: str


def _logical_instructions(text: str) -> list[str]:
    instructions: list[str] = []
    pending = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        instructions.append(pending)
        pending = ""
    if pending:
        raise PinnedSourceError("Dockerfile ends inside a continued instruction")
    return instructions


def is_github_url(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in GITHUB_HOSTS or host.endswith(".githubusercontent.com")


def inspect_dockerfile(text: str) -> tuple[list[PinnedSource], list[str]]:
    """Return the Dockerfile's pinned source stages and every GitHub download outside one.

    A pinned source stage is ``FROM scratch AS <name>`` holding exactly one
    ``ADD --checksum=sha256:<digest> https://<url> /<file>``. Any RUN or ADD that
    names a GitHub-hosted URL anywhere else is reported as a violation.
    """

    stages: list[tuple[str | None, str, list[list[str]]]] = []
    for instruction in _logical_instructions(text):
        tokens = shlex.split(instruction, posix=True)
        keyword = tokens[0].upper()
        if keyword == "FROM":
            name = tokens[3] if len(tokens) >= 4 and tokens[2].upper() == "AS" else None
            stages.append((name, tokens[1], []))
            continue
        if not stages:
            continue
        stages[-1][2].append(tokens)

    pinned: list[PinnedSource] = []
    violations: list[str] = []
    for name, base, body in stages:
        is_pinned_stage = (
            base == "scratch"
            and name is not None
            and len(body) == 1
            and body[0][0].upper() == "ADD"
        )
        if is_pinned_stage:
            assert name is not None
            source = _pinned_add(name, body[0], violations)
            if source is not None:
                pinned.append(source)
            continue
        for tokens in body:
            if tokens[0].upper() not in {"RUN", "ADD"}:
                continue
            for url in _URL_RE.findall(" ".join(tokens[1:])):
                if is_github_url(url):
                    violations.append(
                        f"stage {name or base!r}: {tokens[0].upper()} downloads {url} outside a "
                        "checksum-pinned `FROM scratch AS <name>` stage"
                    )
    names = [source.stage for source in pinned]
    for duplicate in sorted({stage for stage in names if names.count(stage) > 1}):
        violations.append(f"pinned source stage {duplicate!r} is defined more than once")
    return pinned, violations


def _pinned_add(stage: str, tokens: list[str], violations: list[str]) -> PinnedSource | None:
    if not _STAGE_NAME_RE.match(stage):
        violations.append(f"pinned source stage name {stage!r} is not a valid build context name")
        return None
    flags = [token for token in tokens[1:] if token.startswith("--")]
    values = [token for token in tokens[1:] if not token.startswith("--")]
    checksum = _CHECKSUM_RE.match(flags[0]) if len(flags) == 1 else None
    if checksum is None:
        violations.append(
            f"stage {stage!r}: ADD must carry exactly one --checksum=sha256:<64 hex digits>"
        )
        return None
    if len(values) != 2:
        violations.append(f"stage {stage!r}: ADD must name one URL and one destination file")
        return None
    url, destination = values
    if urllib.parse.urlsplit(url).scheme != "https":
        violations.append(f"stage {stage!r}: pinned source must be an https URL, not {url!r}")
        return None
    file_name = _FILE_NAME_RE.match(destination)
    if file_name is None:
        violations.append(
            f"stage {stage!r}: destination {destination!r} must be one file at the stage root"
        )
        return None
    return PinnedSource(stage=stage, url=url, sha256=checksum.group(1), name=file_name.group(1))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def entry_path(cache: Path, source: PinnedSource) -> Path:
    return cache / source.sha256 / source.name


def _require_clean(dockerfile: Path) -> list[PinnedSource]:
    pinned, violations = inspect_dockerfile(dockerfile.read_text(encoding="utf-8"))
    if violations:
        raise PinnedSourceError(f"{dockerfile}: " + "; ".join(violations))
    return pinned


def _download(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "astrabox-testbed-build"})
    try:
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
            with target.open("wb") as handle:
                for chunk in iter(lambda: response.read(1 << 20), b""):
                    handle.write(chunk)
    except urllib.error.HTTPError as error:
        retry_after = error.headers.get("Retry-After") if error.headers else None
        raise PinnedSourceError(
            f"download refused: {url} answered HTTP {error.code}"
            + (f" (Retry-After: {retry_after})" if retry_after else "")
        ) from error
    except urllib.error.URLError as error:
        raise PinnedSourceError(f"download failed: {url}: {error.reason}") from error


def fetch(dockerfile: Path, cache: Path) -> list[dict[str, str]]:
    """Download each absent pinned source once; refuse an entry with the wrong bytes."""

    records: list[dict[str, str]] = []
    for source in _require_clean(dockerfile):
        target = entry_path(cache, source)
        if target.exists():
            actual = _sha256(target)
            if actual != source.sha256:
                raise PinnedSourceError(
                    f"cache entry {target} for stage {source.stage!r} has sha256 {actual}, "
                    f"not the pinned {source.sha256}; it is not refetched over, investigate "
                    "and remove it by hand"
                )
            records.append({**asdict(source), "path": str(target), "action": "cached"})
            continue
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, partial_name = tempfile.mkstemp(prefix=".partial-", dir=target.parent)
        os.close(descriptor)
        partial = Path(partial_name)
        try:
            _download(source.url, partial)
            actual = _sha256(partial)
            if actual != source.sha256:
                raise PinnedSourceError(
                    f"{source.url} delivered sha256 {actual}, not the pinned {source.sha256}"
                )
            partial.chmod(0o644)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        records.append({**asdict(source), "path": str(target), "action": "fetched"})
    return records


def preflight(dockerfile: Path, cache: Path) -> tuple[list[dict[str, str]], list[str]]:
    """Require a verified cache entry for every pinned stage; return records and build flags."""

    records: list[dict[str, str]] = []
    problems: list[str] = []
    flags: list[str] = []
    for source in _require_clean(dockerfile):
        target = entry_path(cache, source)
        record = {**asdict(source), "path": str(target)}
        if not target.is_file():
            problems.append(f"stage {source.stage!r}: no cache entry at {target} for {source.url}")
            record["state"] = "missing"
        elif (actual := _sha256(target)) != source.sha256:
            problems.append(
                f"stage {source.stage!r}: cache entry {target} has sha256 {actual}, "
                f"not the pinned {source.sha256}"
            )
            record["state"] = "mismatch"
        else:
            record["state"] = "verified"
            directory = str(target.parent)
            if any(character.isspace() for character in directory):
                raise PinnedSourceError(f"cache directory contains whitespace: {directory}")
            flags.append(f"--build-context {source.stage}={directory}")
        records.append(record)
    if problems:
        raise PinnedSourceError("; ".join(problems))
    return records, flags


def _write_evidence(path: Path | None, evidence: dict[str, object]) -> None:
    if path is None:
        return
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("fetch", "preflight"):
        sub = commands.add_parser(command)
        sub.add_argument("--source-root", type=Path, required=True)
        sub.add_argument("--dockerfile", required=True, help="path relative to --source-root")
        sub.add_argument("--cache", type=Path, required=True)
        sub.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    dockerfile = args.source_root / args.dockerfile
    cache = args.cache.expanduser()
    if not cache.is_absolute():
        parser.error("--cache must be an absolute path")
    evidence: dict[str, object] = {"command": args.command, "dockerfile": args.dockerfile, "cache": str(cache)}
    try:
        if args.command == "fetch":
            evidence["sources"] = fetch(dockerfile, cache)
        else:
            records, flags = preflight(dockerfile, cache)
            evidence["sources"] = records
            evidence["build_contexts"] = flags
            print(" ".join(flags))
    except PinnedSourceError as error:
        evidence["error"] = str(error)
        _write_evidence(args.json_out, evidence)
        print(f"pinned build sources: {error}", file=sys.stderr)
        return 1
    _write_evidence(args.json_out, evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
