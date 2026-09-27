#!/usr/bin/env python3
"""Every file a Dockerfile takes from the build context gets its mode from the Dockerfile.

Without ``--chmod``, ``COPY`` and ``ADD`` keep the mode each file has in the
build context (Dockerfile reference: "File permissions are preserved"). That
mode is the builder's: a checkout made under umask 077 is all 0600 files and
0700 directories, and the image then carries root-owned files that its
unprivileged runtime user (``astrabox`` in the server image, uid 1000 in the
sandboxes) cannot read. The image works for one builder and fails for
another, at runtime.

So every ``COPY``/``ADD`` that reads the build context must carry
``--chmod``. Exempt are the forms that do not read it: ``--from=`` (a stage
or an image), a heredoc source (``COPY <<EOF``), and an ``ADD`` whose every
source is remote (a URL or a Git address), whose mode Docker sets itself. A
symbolic mode
(``u=rwX,go=rX``) needs Dockerfile syntax 1.14, so a file that uses one must
declare ``# syntax=docker/dockerfile:1`` or a 1.x version from 1.14 on.

The parser follows the Dockerfile reference rather than guessing: parser
directives at the top, ``\\`` line continuations, comment lines inside a
continuation, and heredoc bodies. A Dockerfile it cannot read that way (an
``# escape=`` other than ``\\``, a heredoc that never ends) is an error, not
a pass.

    .venv/bin/python scripts/check_dockerfile_modes.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_DIRECTIVE = re.compile(r"^#\s*([a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*(.*?)\s*$")
_HEREDOC = re.compile(r"<<(-?)([\"']?)([A-Za-z_][A-Za-z0-9_]*)\2")
_SYNTAX_1 = re.compile(r"^(?:docker\.io/)?docker/dockerfile:1(?:\.(\d+)(?:\.\d+)?)?(?:-labs)?(?:@sha256:[0-9a-f]{64})?$")
#: An ADD source Docker fetches rather than reads from the build context.
_REMOTE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://|git@)")
#: The Dockerfile version that added symbolic ``--chmod`` modes.
_SYMBOLIC_SINCE = 14


class DockerfileError(ValueError):
    """The file cannot be read by the Dockerfile reference's rules."""


@dataclass(frozen=True)
class Instruction:
    line: int
    keyword: str
    arguments: str


def parse(text: str) -> tuple[dict[str, str], list[Instruction]]:
    """Parser directives and instructions, one logical instruction per entry."""

    lines = text.splitlines()
    directives: dict[str, str] = {}
    index = 0
    while index < len(lines):
        match = _DIRECTIVE.match(lines[index].strip())
        if not match:
            break
        directives[match.group(1).lower()] = match.group(2)
        index += 1
    if directives.get("escape", "\\") != "\\":
        raise DockerfileError(
            f"escape directive {directives['escape']!r} is not supported by this check"
        )

    instructions: list[Instruction] = []
    while index < len(lines):
        raw = lines[index]
        index += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        start = index
        parts = [raw]
        while parts[-1].rstrip().endswith("\\"):
            parts[-1] = parts[-1].rstrip()[:-1]
            while index < len(lines) and (
                not lines[index].strip() or lines[index].lstrip().startswith("#")
            ):
                index += 1
            if index >= len(lines):
                break
            parts.append(lines[index])
            index += 1
        logical = " ".join(part.strip() for part in parts)
        keyword, _, arguments = logical.partition(" ")
        instructions.append(Instruction(start, keyword.upper(), arguments.strip()))
        for strip_tabs, _, delimiter in _HEREDOC.findall(logical):
            while True:
                if index >= len(lines):
                    raise DockerfileError(
                        f"line {start}: heredoc {delimiter} is never closed"
                    )
                body = lines[index]
                index += 1
                if (body.lstrip("\t") if strip_tabs else body) == delimiter:
                    break
    return directives, instructions


def violations(path: Path, text: str) -> list[str]:
    """Why ``text`` breaks the rule, one message per offending instruction."""

    directives, instructions = parse(text)
    problems: list[str] = []
    symbolic = False
    for instruction in instructions:
        if instruction.keyword not in {"COPY", "ADD"}:
            continue
        flags: dict[str, str] = {}
        tokens = instruction.arguments.split()
        position = 0
        while position < len(tokens) and tokens[position].startswith("--"):
            name, _, value = tokens[position][2:].partition("=")
            flags[name.lower()] = value
            position += 1
        rest = " ".join(tokens[position:])
        if rest.startswith("["):
            try:
                arguments = [str(item) for item in json.loads(rest)]
            except ValueError as exc:
                raise DockerfileError(
                    f"line {instruction.line}: {instruction.keyword} array is not JSON: {exc}"
                ) from exc
        else:
            arguments = rest.split()
        sources = arguments[:-1]
        if "from" in flags or (sources and sources[0].startswith("<<")):
            continue
        if instruction.keyword == "ADD" and sources and all(_REMOTE.match(s) for s in sources):
            continue
        mode = flags.get("chmod")
        if not mode:
            problems.append(
                f"{path}:{instruction.line}: {instruction.keyword} takes files from the "
                "build context without --chmod, so their mode is the builder's umask; "
                "add --chmod=u=rwX,go=rX (or the octal mode the files need)"
            )
        elif not re.fullmatch(r"[0-7]{3,4}", mode):
            symbolic = True
    if symbolic:
        syntax = directives.get("syntax", "")
        match = _SYNTAX_1.match(syntax)
        if not match or (match.group(1) is not None and int(match.group(1)) < _SYMBOLIC_SINCE):
            problems.append(
                f"{path}: uses a symbolic --chmod, which needs Dockerfile syntax 1.14 or "
                f"later; declare `# syntax=docker/dockerfile:1` (found {syntax or 'none'!r})"
            )
    return problems


def dockerfiles() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8").split("\0")
    pattern = re.compile(r"(^|/)(Dockerfile(\.[^/]*)?|[^/]*\.[Dd]ockerfile)$")
    # An unmerged path is listed once per index stage; check each file once.
    names = dict.fromkeys(name for name in listed if name and pattern.search(name))
    return [REPO_ROOT / name for name in names]


def main() -> int:
    files = dockerfiles()
    if not files:
        print("dockerfile mode check found no Dockerfiles; refusing to pass", file=sys.stderr)
        return 1
    problems: list[str] = []
    for path in files:
        relative = path.relative_to(REPO_ROOT)
        try:
            problems.extend(violations(relative, path.read_text(encoding="utf-8")))
        except DockerfileError as exc:
            problems.append(f"{relative}: {exc}")
    if problems:
        print("dockerfile mode check failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"dockerfile mode check passed: {len(files)} Dockerfiles, every build-context copy sets its mode")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
