"""The build entry filename and resident-console release guard agree.

``frontend/src/frontendRelease.ts`` identifies the running release from the
document's entry script. This module reads ``entryFileNames`` from the Vite
config, constructs the emitted path, and applies the guard's own regex and
selector matchers to it. Either side can therefore change independently only if
the contract check fails, without requiring a browser or production build.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
_VITE_CONFIG = _FRONTEND / "vite.config.ts"
_RELEASE_GUARD = _FRONTEND / "src" / "frontendRelease.ts"

# A hash of the shape Vite emits. Substituted into the configured pattern, it
# gives the concrete name the guard is handed.
_SAMPLE_HASH = "K2sG-Evf"

_ENTRY_FILE_NAMES = re.compile(r"""entryFileNames\s*:\s*(['"])(?P<pattern>[^'"]+)\1""")
# Nothing is required after the flags. One of the guard's regexes is called
# inline as `/…/.test(value)`, so a terminator class of `;` or `)` leaves that
# matcher uncollected — and an uncollected matcher is free to move on its own
# while everything here stays green.
_JS_REGEX_LITERAL = re.compile(r"/(?P<body>(?:[^/\\\n]|\\.)+)/(?P<flags>[a-z]*)")
_ASSET_STRING_LITERAL = re.compile(r"""(['"])(?P<value>[^'"]*/assets/[^'"]*)\1""")


def _entry_pattern(vite_config: str) -> str:
    """The filename pattern the build is pinned to.

    The explicit pin is required because Rollup's ``[name]`` default depends on
    the HTML entry name while the release guard requires a stable prefix.
    """
    match = _ENTRY_FILE_NAMES.search(vite_config)
    if match is None:
        raise AssertionError(
            f"no entryFileNames pin in {_VITE_CONFIG.name}. The release guard "
            "identifies a release by this filename, so it cannot be left to the "
            "bundler's default"
        )
    return match.group("pattern")


def _built_entry_path(pattern: str) -> str:
    """The path the pattern produces, as an absolute URL path."""
    if "[name]" in pattern:
        raise AssertionError(
            f"entryFileNames={pattern!r} leaves the entry's name to the bundler. "
            "[name] resolves to the HTML file rather than the module it loads, "
            "which is how the guard came to look for a file nobody emitted"
        )
    path = pattern.replace("[hash]", _SAMPLE_HASH)
    return path if path.startswith("/") else f"/{path}"


def _guard_matchers(guard_source: str) -> tuple[list[re.Pattern[str]], list[str]]:
    """Every regex and every `/assets/` literal the guard tests an entry with."""
    patterns: list[re.Pattern[str]] = []
    for match in _JS_REGEX_LITERAL.finditer(guard_source):
        body = match.group("body")
        if "/assets/" not in body.replace("\\", ""):
            continue
        try:
            patterns.append(
                re.compile(body, re.I if "i" in match.group("flags") else 0)
            )
        except re.error as exc:  # pragma: no cover - a JS-only construct
            raise AssertionError(
                f"the guard's regex {body!r} cannot be read here ({exc}); the "
                "contract still needs checking, so translate it rather than "
                "dropping this assertion"
            ) from exc
    literals = [
        match.group("value")
        for match in _ASSET_STRING_LITERAL.finditer(guard_source)
    ]
    return patterns, literals


def _mismatches(vite_config: str, guard_source: str) -> list[str]:
    """Which of the guard's matchers reject the entry this build emits."""
    entry_path = _built_entry_path(_entry_pattern(vite_config))
    script_tag = f'<script type="module" crossorigin src="{entry_path}"></script>'
    patterns, literals = _guard_matchers(guard_source)

    assert patterns, "found no entry matcher in the guard — this test reads nothing"
    assert literals, "found no /assets/ selector in the guard — this test reads nothing"

    failures = [
        f"regex {pattern.pattern!r} matches neither {entry_path!r} nor the script tag"
        for pattern in patterns
        if not (pattern.search(entry_path) or pattern.search(script_tag))
    ]
    failures += [
        f"selector fragment {literal!r} is not part of {entry_path!r}"
        for literal in literals
        if _asset_fragment(literal) not in entry_path
    ]
    return failures


def _asset_fragment(literal: str) -> str:
    """The `/assets/...` part of a literal, which may be wrapped in a selector."""
    start = literal.index("/assets/")
    end = len(literal)
    for terminator in ('"', "'", "]", ")"):
        found = literal.find(terminator, start)
        if found != -1:
            end = min(end, found)
    return literal[start:end]


def test_the_build_emits_the_entry_the_release_guard_looks_for() -> None:
    failures = _mismatches(
        _VITE_CONFIG.read_text(encoding="utf-8"),
        _RELEASE_GUARD.read_text(encoding="utf-8"),
    )

    assert failures == [], "\n".join(failures)


def test_the_bundler_default_that_broke_this_once_is_refused() -> None:
    """The bundler's name-dependent default is not a valid release contract."""
    with pytest.raises(AssertionError, match=r"\[name\]"):
        _mismatches(
            "entryFileNames: 'assets/[name]-[hash].js'",
            _RELEASE_GUARD.read_text(encoding="utf-8"),
        )


def test_renaming_the_built_entry_alone_fails() -> None:
    failures = _mismatches(
        "entryFileNames: 'assets/console-[hash].js'",
        _RELEASE_GUARD.read_text(encoding="utf-8"),
    )

    assert failures, "the guard accepted an entry name it does not look for"


def test_repointing_the_guard_alone_fails() -> None:
    """The other direction, which a check anchored on the config would miss."""
    repointed = _RELEASE_GUARD.read_text(encoding="utf-8").replace(
        "/assets/main-", "/assets/index-"
    )

    failures = _mismatches(_VITE_CONFIG.read_text(encoding="utf-8"), repointed)

    assert failures, "the built entry satisfied a guard looking for another name"


def test_removing_the_pin_fails_rather_than_passing_vacuously() -> None:
    with pytest.raises(AssertionError, match="no entryFileNames pin"):
        _mismatches("build: { outDir: 'dist' }", _RELEASE_GUARD.read_text(encoding="utf-8"))
