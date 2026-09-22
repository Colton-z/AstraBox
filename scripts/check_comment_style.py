#!/usr/bin/env python3
"""Enforce the mechanical half of the comment rules in ``CONTRIBUTING.md`` (Comments).

The rules are checked over comments and docstrings in source and configuration
files, including untracked working-tree files. Each pattern identifies wording
that an outside reader cannot resolve, such as a private tracker reference or
an account of repository history.

The default check compares findings with the reviewed zero-violation baseline
in ``comment_style_baseline.json``. ``--report`` prints the full work list, and
``--update-baseline`` rewrites the strict-zero file after a clean scan.

Each class is intentionally narrow so that every reported line is actionable.
Rule 8 remains review-only: deciding whether a section banner is a noun phrase
has no stable lexical signal, so a mechanical approximation would add noise.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tokenize
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).resolve().parent / "comment_style_baseline.json"

#: Generated or vendored source and build output are not hand-maintained here.
EXCLUDED_PREFIXES = (
    "channel-gateway/vendor/",
    "frontend/src/components/ai-elements/",
    "frontend/src/components/ui/",
    "dist/",
)
EXCLUDED_PATHS = {
    "frontend/src/api/schema.d.ts",
}

SOURCE_SUFFIXES = {
    ".cjs",
    ".css",
    ".html",
    ".js",
    ".jsx",
    ".mjs",
    ".py",
    ".sh",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}

# Maintained files whose language cannot be inferred from their final suffix.
# Keeping the scanner beside each path prevents a Python entrypoint or a
# hash-commented configuration file from falling through to the HTML scanner.
SPECIAL_SOURCE_KINDS = {
    ".dockerignore": "hash",
    ".env.example": "env",
    ".gitignore": "hash",
    "astrabox/core/service/orchestrator/runtime/astrabox-assistant-workspace-storage": "shell",
    "astrabox/core/service/orchestrator/runtime/hermes-profile-setup": "shell",
    "astrabox/core/service/orchestrator/runtime/hermes-skill-repo-cache": "python",
    "astrabox/core/service/orchestrator/runtime/provision-conversation": "shell",
    "astrabox/common/utils/error_registry_baseline.txt": "hash",
    "Makefile": "hash",
    "containers/casdoor/app.conf": "hash",
    "containers/coredns/Corefile": "hash",
    "containers/coredns/sandbox-edge.Corefile": "hash",
    "containers/sandbox-edge/default.conf.template": "hash",
    "tests/e2e-ui/fixtures/secure-team-gateway.Caddyfile": "hash",
    "website/.gitignore": "hash",
}

#: Names retired from contributor-facing vocabulary. Compatibility sentinels
#: may remain in code, but comments should name the current component and refer
#: to the sentinel's symbol when its behavior must be documented.
RETIRED_NAMES = ("DirectDocker", "direct_docker", "session_kernel_v2")

_RETIRED = "|".join(re.escape(n) for n in RETIRED_NAMES)

#: Proper nouns that name organisation-only code, infrastructure, or process.
#: The explicit list covers names that structural patterns cannot infer.
INTERNAL_NAMES = (
    "enterprise fork",
    "cross-tree",
    "persistence agent",
    "Mongo proxy",
    "turn-recovery displacement",
)

_INTERNAL = "|".join(re.escape(n) for n in INTERNAL_NAMES)


#: Spans the first-person rule does not look inside. A quoted phrase is a value
#: the code produces or a message it emits — ``"I could not tell"`` names a
#: verdict, not the comment author's voice. Rule 5 exempts these spans so that
#: literal messages can be documented without triggering first-person findings.
_QUOTED = re.compile(r'"(?:\\.|[^"\\])*"|``(?:\\.|[^`\\])*``|`(?:\\.|[^`\\])*`')
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Rule:
    """One checked class: which guideline rule it serves, and what it matches."""

    key: str
    rule: str
    summary: str
    pattern: re.Pattern[str]
    #: Blank quoted spans before matching. Set for rules whose vocabulary is
    #: legitimate when quoted.
    outside_quotes: bool = False

    def hits(self, text: str) -> bool:
        text = _WHITESPACE.sub(" ", text)
        if self.outside_quotes:
            text = _QUOTED.sub("", text)
            # An unmatched opening quote makes the remaining prose a quoted
            # value even when the closing quote is in another comment unit.
            head, sep, tail = text.partition('"')
            if sep and '"' not in tail:
                text = head
        return bool(self.pattern.search(text))


RULES: tuple[Rule, ...] = (
    Rule(
        "internal-code",
        "3",
        "internal work-plan or phase code the reader cannot resolve",
        re.compile(
            r"\b(F\d-\d|M\d(?:\s+fix)?|H\d|Stage T\d|Hoist T\d|DB-\d[a-z]?"
            r"|ARCH-\d+|INV-\d+|Phase [A-Z](?:-\d+(?:\.\d+)*)?)\b|\(G\d\)"
        ),
    ),
    Rule(
        "private-issue-ref",
        "3",
        "issue number in a tracker the reader cannot open",
        re.compile(r"\(#\d{1,4}\)|(?<![\w#])#\d{2,4}\b|\bsee #\d+"),
    ),
    Rule(
        "commit-history-ref",
        "3",
        "raw commit id used as repository-history context",
        re.compile(r"\([0-9a-f]{7,12}\)", re.IGNORECASE),
    ),
    Rule(
        "internal-tree",
        "3",
        "reference to the internal tree, fork, or suite",
        re.compile(
            r"\b(?:the\s+)?internal (tree|fork|suite|version|repo|one|spec|test|oracle"
            r"|template|fixture|reader|helper|page)\b"
            r"|\binternal (prod|production) session\b|\binternal suite\b"
            r"|\b(?:not\s+)?ported\b|\bporting notes?\b"
            r"|IN-COMMUNITY|\bthe community (in-box|regression|edition)\b"
            r"|\bcommunity (concept mapping|adaptation|analog|equivalent|expression|stand-in)\b",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "retired-name",
        "3",
        "uses a retired product name instead of the current component",
        re.compile(rf"\b({_RETIRED})\b"),
    ),
    Rule(
        "internal-name",
        "3",
        "names something only reachable from inside the organisation",
        re.compile(rf"\b({_INTERNAL})\b", re.IGNORECASE),
    ),
    Rule(
        "unowned-marker",
        "9",
        "a marker whose meaning is defined nowhere in the repository",
        re.compile(
            r"\b(?:(?:FIXTURE|TESTID|KNOWN)(?:/CONSOLE)?|CONSOLE) GAP\b"
            r"|\bMISSING TESTID\b"
        ),
    ),
    Rule(
        "timeline",
        "2",
        "describes how the code got this way rather than what it is",
        re.compile(
            r"\b(used to|no longer|previously|originally|anymore|historically"
            r"|nowadays|these days|back when|the regression|moved verbatim"
            r"|pinned there now|was moved|were moved|renamed from"
            r"|today(?:'s)?|at the time of writing|launch-day|tree-wide"
            r"|stage report|what this changes|first version)\b"
            r"|BUG \(fixed\)"
            r"|\b(previous|earlier|old|former)\s+(version|implementation|shape|design|approach)\b"
            r"|\b(before (the|this) fix|pre-fix|what this replaces|bug this replaces"
            r"|the fix this pins|the actual regression|old (failure|guard|nail|loop|source"
            r"|detail page|location)|the incident this comes from)\b"
            r"|\b(before this (file|module|branch|existed|parameter existed)"
            r"|previous (behavior|behaviour|implementation|guard)"
            r"|measured before this|had already drifted|old hand-copied"
            r"|exactly as it did before|same as before)\b"
            r"|\bthe first attempt (put|failed)\b|\b(?:already )?shipped once\b"
            r"|\bthe (case|call|failure) that actually broke\b"
            r"|\bthe (API|fetch-loop) port\s+(spent|made|called|used|interrupted|could)\b"
            r"|\bthe fetch loop it replaces\b"
            r"|\bonce carried\b|\bwas until now\b|\bHEAD~\d+\b"
            r"|\bextracted verbatim from SessionPage(?:Ready)?\b"
            r"|\bthe original (?:implementation|version|design|approach)\b"
            r"|\bbefore (?:the )?(?:rails? (?:were|was) unified|(?:kit (?:was )?)?re-vendor(?:ed|ing)?)\b"
            r"|\b(?:recovered|restored) from (?:a |the )?deleted file\b"
            r"|\bgit show HEAD:"
            r"|\b(?:the|this) fix\b|\bpins? the fix\b|\breverting the fix\b"
            r"|\bis what made\b"
            r"|\bthe old (?:branch|fallback|judge|read path|dispatch|implementation"
            r"|scan|listing|gateway|sample|give-back|mismatch branch|single sample"
            r"|HTTP gateway|Python scan)\b",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "incident-story",
        "2 / 4",
        "repository or deployment incident used to explain current code",
        re.compile(
            r"\b(?:the )?(?:defect|failure|regression|incident|crash) (?:this|that) "
            r"(?:covers|pins|proof exists for|test exists for)\b"
            r"|\b(?:cost|costs|costing) (?:a|one|the) (?:full )?"
            r"(?:deploy(?:ment)? )?round\b"
            r"|\b(?:every|all) (?:other )?(?:gate|test|check)s? passed\b"
            r"|\b(?:nothing was watching|no spec had ever checked)\b"
            r"|\b(?:(?:was|were) )?(?:caught|found|surfaced) only (?:by|when|after)\b"
            r"|\b(?:was never measured|shipped a crash|survived only as a leftover)\b"
            r"|\b(?:first iteration on a fresh worker dies|a fresh host fails)\b"
            r"|\b(?:a )?deletion by line range\b|\bthis was a literal\b"
            r"|\bthis asserted the opposite\b|\bhad no (?:coverage|test|spec|guard)s?\b"
            r"|\bsurvived (?:a|the) green (?:test|suite|gate)\b"
            r"|\bthe first deploy that\b|\bstayed at whatever the first deploy wrote\b"
            r"|\bfailed on a real testbed before it was a test\b"
            r"|\b(?:measured on (?:a |the )?fresh worker|measured against the live server)\b"
            r"|\bverbatim from a real restart\b",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "private-infra",
        "4",
        "cites our infrastructure instead of the failure mechanism",
        re.compile(
            r"\b(measured on the testbed|on the testbed|the testbed|crash-matrix"
            r"|testbed-verified|AWS (?:box|dev box)"
            r"|verified against the live store|internal prod session)\b"
            r"|\bround \d+,\s*20\d\d-\d\d-\d\d\b",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "first-person",
        "5",
        "first person: the subject should be something in the code",
        re.compile(
            r"(?<![\w'-])(?:(?i:we|our|my|ours)|us|Us|(?i:let's(?!\s+encrypt\b)))"
            r"(?![\w'])|(?<![\w'-])I(?![\w'/])"
        ),
        outside_quotes=True,
    ),
    Rule(
        "helper-heading",
        "6",
        "comment restates the following helper signature",
        re.compile(r"\bINLINE HELPER\b", re.IGNORECASE),
    ),
    Rule(
        "tone",
        "7",
        "editorial or emotive register",
        re.compile(
            # "simply" is deliberately absent: in this tree it almost always
            # carries the neutral "merely" sense ("the classifier simply returns
            # False"), not the dismissive one. Flagging it would make the gate
            # argue with the reader on nearly every hit.
            r"\b(unfortunately|sadly|annoyingly|obviously|of course|nasty|ugly|stupid|horrible|awful|bricked)\b|!!",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "todo-marker",
        "9",
        "unowned work marker in a public repository",
        re.compile(r"\b(TODO|FIXME|XXX|HACK|WIP)\b"),
    ),
)


def source_paths() -> list[str]:
    """Versioned and untracked source/config files, relative to the repository."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [
        p
        for p in out
        if p
        and (REPO_ROOT / p).is_file()
        and not p.startswith(EXCLUDED_PREFIXES)
        and p not in EXCLUDED_PATHS
        and (
            Path(p).suffix.lower() in SOURCE_SUFFIXES
            or Path(p).name.startswith("Dockerfile")
            or Path(p).name == "Caddyfile"
            or p in SPECIAL_SOURCE_KINDS
        )
    ]


def python_units(source: str) -> list[tuple[int, str]]:
    """Comment tokens and docstrings — never text that merely looks like either.

    Tokenizing rather than matching ``^\\s*#`` is what keeps a ``#`` inside a
    string literal (a URL fragment, a colour, a shell command in a test fixture)
    from being read as a comment.
    """
    comments: list[tuple[int, str, bool]] = []
    source_lines = source.splitlines()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                prefix = source_lines[tok.start[0] - 1][: tok.start[1]]
                comments.append((tok.start[0], tok.string[1:].lstrip(), not prefix.strip()))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return _group_comment_lines(comments)

    units = _group_comment_lines(comments)

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return units
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc is None:
            continue
        start = getattr(node.body[0], "lineno", 1)
        units.append((start, doc))
    return units


_HASH_COMMENT = re.compile(r"^\s*#")
_COMMENTED_ENV_ASSIGNMENT = re.compile(r"^\s*#[A-Z_][A-Z0-9_]*=")


def _group_comment_lines(
    comments: list[tuple[int, str, bool]],
) -> list[tuple[int, str]]:
    """Join adjacent pure comment lines without joining trailing comments."""
    units: list[tuple[int, str]] = []
    previous_full_line: int | None = None
    previous_group: int | None = None
    for lineno, text, full_line in comments:
        if full_line and text and previous_full_line == lineno - 1:
            assert previous_group is not None
            start, previous = units[previous_group]
            units[previous_group] = (start, f"{previous}\n{text}")
        elif text:
            units.append((lineno, text))
            previous_group = len(units) - 1
        if full_line and text:
            previous_full_line = lineno
        else:
            previous_full_line = None
            previous_group = None
    return units


def hash_comment_units(
    source: str, *, ignore_commented_env_assignments: bool = False
) -> list[tuple[int, str]]:
    """Full-line comments for hash-commented configuration files.

    Requiring the marker at the start avoids guessing whether a trailing hash
    begins a comment inside TOML, YAML, or Dockerfile syntax.
    """
    return _group_comment_lines(
        [
            (n, line.lstrip()[1:].lstrip(), True)
            for n, line in enumerate(source.splitlines(), 1)
            if _HASH_COMMENT.match(line)
            and not (ignore_commented_env_assignments and _COMMENTED_ENV_ASSIGNMENT.match(line))
        ]
    )


_SHELL_COMMENT_BOUNDARIES = frozenset(" \t\r\n;|&()<>")
_SHELL_COMMAND_PREFIX_WORDS = frozenset({"do", "elif", "else", "if", "then", "until", "while"})
_ANSI_C_SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
}


def _decode_ansi_c_word(raw: str) -> str:
    """Decode common Bash ANSI-C escapes without altering literal Unicode."""
    decoded: list[str] = []
    cursor = 0
    while cursor < len(raw):
        if raw[cursor] != "\\" or cursor + 1 >= len(raw):
            decoded.append(raw[cursor])
            cursor += 1
            continue

        escape_start = cursor
        cursor += 1
        kind = raw[cursor]
        if kind in _ANSI_C_SIMPLE_ESCAPES:
            decoded.append(_ANSI_C_SIMPLE_ESCAPES[kind])
            cursor += 1
            continue
        if kind in {"x", "u", "U"}:
            limit = {"x": 2, "u": 4, "U": 8}[kind]
            cursor += 1
            digits_start = cursor
            while (
                cursor < len(raw)
                and cursor - digits_start < limit
                and raw[cursor] in "0123456789abcdefABCDEF"
            ):
                cursor += 1
            digits = raw[digits_start:cursor]
            if digits:
                try:
                    decoded.append(chr(int(digits, 16)))
                    continue
                except ValueError:
                    pass
            decoded.append(raw[escape_start:cursor])
            continue
        if kind in "01234567":
            digits_start = cursor
            while cursor < len(raw) and cursor - digits_start < 3 and raw[cursor] in "01234567":
                cursor += 1
            decoded.append(chr(int(raw[digits_start:cursor], 8)))
            continue
        if kind == "c" and cursor + 1 < len(raw):
            decoded.append(chr(ord(raw[cursor + 1]) & 0x1F))
            cursor += 2
            continue
        decoded.extend(("\\", kind))
        cursor += 1
    return "".join(decoded)


@dataclass
class _ShellFrame:
    kind: str
    previous: str | None = None
    depth: int = 0
    case_states: list[str] = field(default_factory=list)
    command_position: bool = True


class _ShellCommentLexer:
    """Extract comments while preserving shell word and expansion boundaries."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.index = 0
        self.line = 1
        self.frames = [_ShellFrame("code")]
        self.pending_heredocs: list[tuple[str, bool]] = []
        self.comments: list[tuple[int, str, bool]] = []

    def _move(self, end: int) -> None:
        self.line += self.source.count("\n", self.index, end)
        self.index = end

    def _push_expansion(self) -> bool:
        if self.source.startswith("$((", self.index):
            self.frames.append(_ShellFrame("arithmetic", depth=1))
            self._move(self.index + 3)
            return True
        if self.source.startswith("$(", self.index):
            self.frames.append(_ShellFrame("command", depth=1))
            self._move(self.index + 2)
            return True
        if self.source.startswith("${", self.index):
            self.frames.append(_ShellFrame("parameter", depth=1))
            self._move(self.index + 2)
            return True
        return False

    def _close_frame(self, replacement: str) -> None:
        self.frames.pop()
        self.frames[-1].previous = replacement

    def _parse_heredoc(self) -> bool:
        if (
            not self.source.startswith("<<", self.index)
            or self.source.startswith("<<<", self.index)
            or (self.index > 0 and self.source[self.index - 1] == "<")
        ):
            return False
        cursor = self.index + 2
        strip_tabs = cursor < len(self.source) and self.source[cursor] == "-"
        if strip_tabs:
            cursor += 1
        while cursor < len(self.source) and self.source[cursor] in " \t":
            cursor += 1
        if cursor >= len(self.source) or self.source[cursor] in "\r\n":
            return False

        delimiter: list[str] = []
        quote: str | None = None
        ansi_start: int | None = None
        while cursor < len(self.source):
            char = self.source[cursor]
            if quote is not None:
                if char == quote:
                    if ansi_start is not None:
                        raw = "".join(delimiter[ansi_start:])
                        del delimiter[ansi_start:]
                        delimiter.extend(_decode_ansi_c_word(raw))
                        ansi_start = None
                    quote = None
                elif char == "\\" and quote == '"' and cursor + 1 < len(self.source):
                    cursor += 1
                    delimiter.append(self.source[cursor])
                else:
                    delimiter.append(char)
                cursor += 1
                continue
            if char in {"'", '"'}:
                quote = char
                cursor += 1
                continue
            if (
                char == "$"
                and cursor + 1 < len(self.source)
                and self.source[cursor + 1] in {"'", '"'}
            ):
                quote = self.source[cursor + 1]
                if quote == "'":
                    ansi_start = len(delimiter)
                cursor += 2
                continue
            if char == "\\" and cursor + 1 < len(self.source):
                cursor += 1
                delimiter.append(self.source[cursor])
                cursor += 1
                continue
            if char.isspace() or char in ";|&()<>":
                break
            delimiter.append(char)
            cursor += 1

        if quote is not None or not delimiter:
            return False
        self.pending_heredocs.append(("".join(delimiter), strip_tabs))
        self.frames[-1].previous = "w"
        self._move(cursor)
        return True

    def _skip_heredocs(self) -> None:
        while self.pending_heredocs and self.index < len(self.source):
            delimiter, strip_tabs = self.pending_heredocs.pop(0)
            while self.index < len(self.source):
                end = self.source.find("\n", self.index)
                if end < 0:
                    end = len(self.source)
                candidate = self.source[self.index : end]
                if candidate.endswith("\r"):
                    candidate = candidate[:-1]
                if strip_tabs:
                    candidate = candidate.lstrip("\t")
                self._move(end + 1 if end < len(self.source) else end)
                if candidate == delimiter:
                    break

    def _quoted(self, frame: _ShellFrame) -> None:
        char = self.source[self.index]
        if frame.kind == "single":
            self._move(self.index + 1)
            if char == "'":
                self._close_frame("'")
            return
        if frame.kind == "ansi-single":
            if char == "\\":
                self._move(min(self.index + 2, len(self.source)))
            else:
                self._move(self.index + 1)
                if char == "'":
                    self._close_frame("'")
            return
        if char == "\\":
            self._move(min(self.index + 2, len(self.source)))
        elif char == '"':
            self._move(self.index + 1)
            self._close_frame('"')
        elif char == "`":
            self.frames.append(_ShellFrame("backtick"))
            self._move(self.index + 1)
        elif not self._push_expansion():
            self._move(self.index + 1)

    def _inactive_expansion(self, frame: _ShellFrame) -> None:
        char = self.source[self.index]
        if char == "\\":
            self._move(min(self.index + 2, len(self.source)))
            return
        if self.source.startswith("$'", self.index):
            self.frames.append(_ShellFrame("ansi-single"))
            self._move(self.index + 2)
            return
        if char in {"'", '"'}:
            self.frames.append(_ShellFrame("single" if char == "'" else "double"))
            self._move(self.index + 1)
            return
        if self._push_expansion():
            return
        if frame.kind == "parameter":
            if self.source.startswith("${", self.index):
                frame.depth += 1
                self._move(self.index + 2)
            elif char == "}":
                frame.depth -= 1
                self._move(self.index + 1)
                if frame.depth == 0:
                    self._close_frame("}")
            else:
                self._move(self.index + 1)
            return
        if char == "(":
            frame.depth += 1
            self._move(self.index + 1)
        elif char == ")":
            if frame.depth == 1 and self.source.startswith("))", self.index):
                self._move(self.index + 2)
                self._close_frame(")")
            else:
                frame.depth = max(1, frame.depth - 1)
                self._move(self.index + 1)
        else:
            self._move(self.index + 1)

    def _active_shell(self, frame: _ShellFrame) -> None:
        char = self.source[self.index]
        if frame.kind == "backtick" and char == "`":
            self._move(self.index + 1)
            self._close_frame("`")
            return
        if char == "\\":
            end = min(self.index + 2, len(self.source))
            escaped = self.source[self.index + 1 : end]
            self._move(end)
            if escaped != "\n":
                frame.previous = "w"
            return
        if char == "\n":
            frame.previous = "\n"
            frame.command_position = True
            self._move(self.index + 1)
            self._skip_heredocs()
            return
        if char in {"'", '"'}:
            if frame.case_states and frame.case_states[-1] == "pattern-start":
                frame.case_states[-1] = "pattern"
            frame.previous = char
            frame.command_position = False
            self.frames.append(_ShellFrame("single" if char == "'" else "double"))
            self._move(self.index + 1)
            return
        if char == "`":
            frame.command_position = False
            self.frames.append(_ShellFrame("backtick"))
            self._move(self.index + 1)
            return
        if self.source.startswith("$'", self.index):
            if frame.case_states and frame.case_states[-1] == "pattern-start":
                frame.case_states[-1] = "pattern"
            frame.previous = "w"
            frame.command_position = False
            self.frames.append(_ShellFrame("ansi-single"))
            self._move(self.index + 2)
            return
        if self.source.startswith(("$(", "${"), self.index):
            if frame.case_states and frame.case_states[-1] == "pattern-start":
                frame.case_states[-1] = "pattern"
            frame.command_position = False
        if self._push_expansion():
            return
        if frame.case_states and frame.case_states[-1] == "pattern-start" and char == "(":
            frame.case_states[-1] = "pattern"
            frame.previous = char
            self._move(self.index + 1)
            return
        if self.source.startswith("((", self.index):
            frame.previous = ")"
            frame.command_position = False
            self.frames.append(_ShellFrame("arithmetic-command", depth=1))
            self._move(self.index + 2)
            return
        if frame.kind != "arithmetic-command" and self._parse_heredoc():
            return
        if char == "#" and (frame.previous is None or frame.previous in _SHELL_COMMENT_BOUNDARIES):
            end = self.source.find("\n", self.index + 1)
            if end < 0:
                end = len(self.source)
            line_start = self.source.rfind("\n", 0, self.index) + 1
            prefix = self.source[line_start : self.index]
            text = self.source[self.index + 1 : end].lstrip()
            self.comments.append((self.line, text, not prefix.strip()))
            self._move(end)
            return
        if char.isalpha() or char == "_":
            end = self.index + 1
            while end < len(self.source) and (
                self.source[end].isalnum() or self.source[end] in {"_", "-"}
            ):
                end += 1
            word = self.source[self.index : end]
            was_command_position = frame.command_position
            state = frame.case_states[-1] if frame.case_states else None
            if word == "case" and frame.command_position and state in {None, "body"}:
                frame.case_states.append("subject")
            elif word == "in" and state == "subject":
                frame.case_states[-1] = "pattern-start"
            elif word == "esac" and state in {"body", "pattern-start"} and frame.command_position:
                frame.case_states.pop()
            elif state == "pattern-start":
                frame.case_states[-1] = "pattern"
            frame.command_position = was_command_position and word in _SHELL_COMMAND_PREFIX_WORDS
            frame.previous = word[-1]
            self._move(end)
            return
        if frame.case_states and frame.case_states[-1] == "body":
            terminator = next(
                (
                    value
                    for value in (";;&", ";;", ";&")
                    if self.source.startswith(value, self.index)
                ),
                None,
            )
            if terminator is not None:
                frame.case_states[-1] = "pattern-start"
                frame.command_position = True
                frame.previous = ";"
                self._move(self.index + len(terminator))
                return
        if frame.kind == "arithmetic-command":
            if char == "(":
                frame.depth += 1
                self._move(self.index + 1)
            elif char == ")":
                if frame.depth == 1 and self.source.startswith("))", self.index):
                    self._move(self.index + 2)
                    self._close_frame(")")
                else:
                    frame.depth = max(1, frame.depth - 1)
                    self._move(self.index + 1)
            else:
                frame.previous = char
                self._move(self.index + 1)
            return
        if frame.case_states and frame.case_states[-1] == "pattern-start" and not char.isspace():
            frame.case_states[-1] = "pattern"
        if frame.case_states and frame.case_states[-1] == "pattern" and char == ")":
            frame.case_states[-1] = "body"
            frame.command_position = True
            frame.previous = char
            self._move(self.index + 1)
            return
        if frame.kind == "command" and char == "(":
            frame.depth += 1
            frame.command_position = True
        elif frame.kind == "command" and char == ")":
            if frame.case_states and frame.depth == 1:
                frame.previous = char
                self._move(self.index + 1)
                return
            frame.depth -= 1
            self._move(self.index + 1)
            if frame.depth == 0:
                self._close_frame(")")
            return
        frame.previous = char
        if char in ";|&!({":
            frame.command_position = True
        elif not char.isspace():
            frame.command_position = False
        self._move(self.index + 1)

    def scan(self) -> list[tuple[int, str]]:
        while self.index < len(self.source):
            frame = self.frames[-1]
            if frame.kind in {"ansi-single", "single", "double"}:
                self._quoted(frame)
            elif frame.kind in {"parameter", "arithmetic"}:
                self._inactive_expansion(frame)
            else:
                self._active_shell(frame)
        return _group_comment_lines(self.comments)


def shell_comment_units(source: str) -> list[tuple[int, str]]:
    """Return shell comments without reading quoted words or heredoc payloads."""
    return _ShellCommentLexer(source).scan()


def _block_comment_text(body: str) -> str:
    """Remove conventional leading stars while retaining one complete unit."""
    return "\n".join(re.sub(r"^\s*\* ?", "", line) for line in body.splitlines())


class _JavascriptCommentLexer:
    """Extract JavaScript-family comments without parsing program semantics."""

    _REGEX_PREFIX_KEYWORDS = frozenset(
        "await case delete do else in instanceof new of return throw typeof void yield".split()
    )

    def __init__(self, source: str, *, jsx: bool, typescript: bool) -> None:
        self.source = source
        self.jsx = jsx
        self.typescript = typescript
        self.index = 0
        self.line = 1
        self.units: list[tuple[int, str]] = []
        self._pure_line_comment: tuple[int, int] | None = None

    def scan(self) -> list[tuple[int, str]]:
        self._code(stop_at_closing_brace=False)
        return self.units

    def _move(self, end: int) -> None:
        self.line += self.source.count("\n", self.index, end)
        self.index = end

    def _line_comment(self) -> None:
        start_line = self.line
        end = self.source.find("\n", self.index + 2)
        if end < 0:
            end = len(self.source)
        line_start = self.source.rfind("\n", 0, self.index) + 1
        pure_line = not self.source[line_start : self.index].strip()
        text = self.source[self.index + 2 : end].strip()
        if pure_line and text and self._pure_line_comment == (len(self.units) - 1, start_line - 1):
            unit_index, _previous_line = self._pure_line_comment
            first_line, previous = self.units[unit_index]
            self.units[unit_index] = (first_line, f"{previous}\n{text}")
        elif text:
            self.units.append((start_line, text))
        if pure_line and text:
            self._pure_line_comment = (len(self.units) - 1, start_line)
        else:
            self._pure_line_comment = None
        self._move(end)

    def _block_comment(self) -> None:
        self._pure_line_comment = None
        start_line = self.line
        end = self.source.find("*/", self.index + 2)
        body_end = len(self.source) if end < 0 else end
        self.units.append((start_line, _block_comment_text(self.source[self.index + 2 : body_end])))
        self._move(len(self.source) if end < 0 else end + 2)

    def _quoted_string(self, quote: str) -> None:
        self._move(self.index + 1)
        while self.index < len(self.source):
            char = self.source[self.index]
            if char == "\\":
                self._move(min(self.index + 2, len(self.source)))
            elif char == quote:
                self._move(self.index + 1)
                return
            elif char == "\n":
                # A raw newline ends an invalid single- or double-quoted token.
                self._move(self.index + 1)
                return
            else:
                self._move(self.index + 1)

    def _template(self) -> None:
        self._move(self.index + 1)
        while self.index < len(self.source):
            if self.source[self.index] == "\\":
                self._move(min(self.index + 2, len(self.source)))
            elif self.source[self.index] == "`":
                self._move(self.index + 1)
                return
            elif self.source.startswith("${", self.index):
                self._move(self.index + 2)
                self._code(stop_at_closing_brace=True)
            else:
                self._move(self.index + 1)

    def _regex_literal(self) -> None:
        in_character_class = False
        self._move(self.index + 1)
        while self.index < len(self.source):
            char = self.source[self.index]
            if char == "\\":
                self._move(min(self.index + 2, len(self.source)))
            elif char == "\n":
                return
            elif char == "[":
                in_character_class = True
                self._move(self.index + 1)
            elif char == "]":
                in_character_class = False
                self._move(self.index + 1)
            elif char == "/" and not in_character_class:
                self._move(self.index + 1)
                while self.index < len(self.source) and self.source[self.index].isalpha():
                    self._move(self.index + 1)
                return
            else:
                self._move(self.index + 1)

    def _jsx_tag(self) -> bool:
        """Consume an opening tag and return whether it closes itself."""
        self._move(self.index + 1)
        while self.index < len(self.source):
            if self.source.startswith("/>", self.index):
                self._move(self.index + 2)
                return True
            char = self.source[self.index]
            if char in {'"', "'"}:
                self._quoted_string(char)
            elif char == "{":
                self._move(self.index + 1)
                self._code(stop_at_closing_brace=True)
            elif char == ">":
                self._move(self.index + 1)
                return False
            else:
                self._move(self.index + 1)
        return False

    def _jsx_element(self) -> None:
        if self._jsx_tag():
            return
        while self.index < len(self.source):
            if self.source.startswith("</", self.index):
                end = self.source.find(">", self.index + 2)
                self._move(len(self.source) if end < 0 else end + 1)
                return
            if self.source[self.index] == "<" and self._looks_like_jsx():
                self._jsx_element()
            elif self.source[self.index] == "{":
                self._move(self.index + 1)
                self._code(stop_at_closing_brace=True)
            else:
                self._move(self.index + 1)

    def _looks_like_jsx(self) -> bool:
        next_index = self.index + 1
        return next_index < len(self.source) and (
            self.source[next_index] == ">" or self.source[next_index].isalpha()
        )

    def _looks_like_typescript_generic_arrow(self) -> bool:
        """Recognize a TS generic arrow before treating ``<`` as JSX.

        The closing angle may follow nested generic types or a multi-line
        constraint. Requiring a balanced parameter list and ``=>`` keeps the
        lookahead narrow enough that an actual JSX element is unchanged.
        """
        limit = min(len(self.source), self.index + 8192)

        def skip_comment(cursor: int) -> int | None:
            if self.source.startswith("//", cursor):
                end = self.source.find("\n", cursor + 2, limit)
                return limit if end < 0 else end
            if self.source.startswith("/*", cursor):
                end = self.source.find("*/", cursor + 2, limit)
                return limit if end < 0 else end + 2
            return None

        def skip_quoted(cursor: int) -> int:
            quote = self.source[cursor]
            cursor += 1
            while cursor < limit:
                char = self.source[cursor]
                if char == "\\":
                    cursor += 2
                elif char == quote:
                    return cursor + 1
                else:
                    cursor += 1
            return limit

        def skip_regex(cursor: int) -> int:
            cursor += 1
            in_character_class = False
            while cursor < limit:
                char = self.source[cursor]
                if char == "\\":
                    cursor += 2
                elif char == "[":
                    in_character_class = True
                    cursor += 1
                elif char == "]":
                    in_character_class = False
                    cursor += 1
                elif char == "/" and not in_character_class:
                    cursor += 1
                    while cursor < limit and self.source[cursor].isalpha():
                        cursor += 1
                    return cursor
                elif char == "\n":
                    return cursor
                else:
                    cursor += 1
            return limit

        def skip_trivia(cursor: int) -> int:
            while cursor < limit:
                if self.source[cursor].isspace():
                    cursor += 1
                    continue
                comment_end = skip_comment(cursor)
                if comment_end is None:
                    return cursor
                cursor = comment_end
            return cursor

        cursor = self.index + 1
        angle_depth = 1
        generic_end: int | None = None
        while cursor < limit and angle_depth:
            char = self.source[cursor]
            comment_end = skip_comment(cursor)
            if comment_end is not None:
                cursor = comment_end
                continue
            if char in {"'", '"', "`"}:
                cursor = skip_quoted(cursor)
                continue
            elif char == "<":
                angle_depth += 1
            elif char == ">" and not (cursor > self.index and self.source[cursor - 1] == "="):
                angle_depth -= 1
                if angle_depth == 0:
                    generic_end = cursor
            cursor += 1
        if angle_depth or generic_end is None:
            return False

        signature = re.sub(
            r"/\*.*?\*/|//[^\n]*", " ", self.source[self.index + 1 : generic_end], flags=re.DOTALL
        )
        if not (
            "," in signature
            or "=" in signature
            or re.search(r"\bextends\b", signature)
            or re.match(r"\s*const\b", signature)
        ):
            return False

        cursor = skip_trivia(cursor)
        if cursor >= limit or self.source[cursor] != "(":
            return False

        paren_depth = 1
        cursor += 1
        regex_allowed = True
        while cursor < limit and paren_depth:
            char = self.source[cursor]
            if char.isspace():
                cursor += 1
                continue
            comment_end = skip_comment(cursor)
            if comment_end is not None:
                cursor = comment_end
                continue
            if char in {"'", '"', "`"}:
                cursor = skip_quoted(cursor)
                regex_allowed = False
                continue
            if char == "/" and regex_allowed:
                cursor = skip_regex(cursor)
                regex_allowed = False
                continue
            if char.isalpha() or char in {"_", "$"}:
                start = cursor
                cursor += 1
                while cursor < limit and (
                    self.source[cursor].isalnum() or self.source[cursor] in {"_", "$"}
                ):
                    cursor += 1
                regex_allowed = self.source[start:cursor] in self._REGEX_PREFIX_KEYWORDS
                continue
            if char.isdigit():
                cursor += 1
                while cursor < limit and (
                    self.source[cursor].isalnum() or self.source[cursor] in {"_", "."}
                ):
                    cursor += 1
                regex_allowed = False
                continue
            if self.source.startswith(("++", "--"), cursor):
                cursor += 2
                continue
            elif char == "(":
                paren_depth += 1
                regex_allowed = True
            elif char == ")":
                paren_depth -= 1
                regex_allowed = False
            elif char != "!":
                regex_allowed = char not in {"]", "}"}
            cursor += 1
        if paren_depth:
            return False
        cursor = skip_trivia(cursor)
        return self.source.startswith("=>", cursor)

    def _code(self, *, stop_at_closing_brace: bool) -> None:
        brace_depth = 0
        regex_allowed = True
        while self.index < len(self.source):
            char = self.source[self.index]
            if char.isspace():
                self._move(self.index + 1)
                continue
            if self.source.startswith("//", self.index):
                self._line_comment()
                continue
            if self.source.startswith("/*", self.index):
                self._block_comment()
                continue
            if char in {'"', "'"}:
                self._quoted_string(char)
                regex_allowed = False
                continue
            if char == "`":
                self._template()
                regex_allowed = False
                continue
            if char == "}" and stop_at_closing_brace and brace_depth == 0:
                self._move(self.index + 1)
                return
            if char == "{":
                brace_depth += 1
                regex_allowed = True
                self._move(self.index + 1)
                continue
            if char == "}":
                brace_depth = max(0, brace_depth - 1)
                regex_allowed = False
                self._move(self.index + 1)
                continue
            if char.isalpha() or char in {"_", "$"}:
                start = self.index
                self._move(self.index + 1)
                while self.index < len(self.source) and (
                    self.source[self.index].isalnum() or self.source[self.index] in {"_", "$"}
                ):
                    self._move(self.index + 1)
                regex_allowed = self.source[start : self.index] in self._REGEX_PREFIX_KEYWORDS
                continue
            if char.isdigit():
                self._move(self.index + 1)
                while self.index < len(self.source) and (
                    self.source[self.index].isalnum() or self.source[self.index] in {"_", "."}
                ):
                    self._move(self.index + 1)
                regex_allowed = False
                continue
            if self.source.startswith(("++", "--"), self.index):
                self._move(self.index + 2)
                continue
            if (
                self.jsx
                and regex_allowed
                and char == "<"
                and self._looks_like_jsx()
                and not (self.typescript and self._looks_like_typescript_generic_arrow())
            ):
                self._jsx_element()
                regex_allowed = False
                continue
            if char == "/" and regex_allowed:
                self._regex_literal()
                regex_allowed = False
                continue
            if char in {
                ")",
                "]",
            }:
                regex_allowed = False
            elif char == ".":
                regex_allowed = False
            elif char != "!":
                regex_allowed = True
            self._move(self.index + 1)


def javascript_comment_units(
    source: str, *, jsx: bool = False, typescript: bool = False
) -> list[tuple[int, str]]:
    """Return complete line and block comments from JavaScript-family source."""
    return _JavascriptCommentLexer(source, jsx=jsx, typescript=typescript).scan()


_CSS_TOKEN = re.compile(
    r"""(?P<string>"(?:\\.|[^"\\])*(?:"|\Z)|'(?:\\.|[^'\\])*(?:'|\Z))"""
    r"|/\*(?P<comment>.*?)(?:\*/|\Z)",
    re.DOTALL,
)


def css_comment_units(source: str) -> list[tuple[int, str]]:
    """Return CSS block comments while ignoring quoted property values."""
    return [
        (
            source.count("\n", 0, match.start()) + 1,
            _block_comment_text(match.group("comment")),
        )
        for match in _CSS_TOKEN.finditer(source)
        if match.group("comment") is not None
    ]


_HTML_COMMENT = re.compile(r"^[ \t]*<!--(.*?)(?:-->|\Z)", re.MULTILINE | re.DOTALL)


def html_comment_units(source: str) -> list[tuple[int, str]]:
    """Return full-line HTML comment blocks without interpreting embedded code."""
    return [
        (source.count("\n", 0, match.start()) + 1, match.group(1))
        for match in _HTML_COMMENT.finditer(source)
    ]


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    key: str
    text: str


def _violation_location(lineno: int, text: str, rule: Rule) -> tuple[int, str]:
    lines = text.splitlines()
    for offset, line in enumerate(lines):
        if rule.hits(line):
            return lineno + offset, line.strip()[:120]
    for width in range(2, len(lines) + 1):
        for offset in range(len(lines) - width + 1):
            excerpt = "\n".join(lines[offset : offset + width])
            if rule.hits(excerpt):
                return lineno + offset, _WHITESPACE.sub(" ", excerpt).strip()[:120]
    return lineno, _WHITESPACE.sub(" ", text).strip()[:120]


class ScanError(RuntimeError):
    """A selected source file could not be checked."""


def scan(paths: list[str]) -> list[Violation]:
    found: list[Violation] = []
    for rel in paths:
        full = REPO_ROOT / rel
        try:
            source = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ScanError(f"cannot read {rel}: {exc}") from exc
        suffix = full.suffix.lower()
        special_kind = SPECIAL_SOURCE_KINDS.get(rel)
        if suffix == ".py" or special_kind == "python":
            units = python_units(source)
        elif suffix == ".sh" or special_kind == "shell":
            units = shell_comment_units(source)
        elif (
            suffix in {".toml", ".yaml", ".yml"}
            or full.name.startswith("Dockerfile")
            or full.name == "Caddyfile"
            or special_kind in {"env", "hash"}
        ):
            units = hash_comment_units(
                source, ignore_commented_env_assignments=special_kind == "env"
            )
        elif suffix in {".cjs", ".js", ".jsx", ".mjs", ".ts", ".tsx"}:
            units = javascript_comment_units(
                source,
                jsx=suffix in {".jsx", ".tsx"},
                typescript=suffix in {".ts", ".tsx"},
            )
        elif suffix == ".css":
            units = css_comment_units(source)
        else:
            units = html_comment_units(source)
        for lineno, text in units:
            for rule in RULES:
                if rule.hits(text):
                    violation_line, excerpt = _violation_location(lineno, text, rule)
                    found.append(Violation(rel, violation_line, rule.key, excerpt))
    return found


def tally(violations: list[Violation]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for v in violations:
        counts[v.path][v.key] += 1
    return {p: dict(sorted(c.items())) for p, c in sorted(counts.items())}


@dataclass(frozen=True)
class Baseline:
    """A validated snapshot of the exact reviewed findings."""

    total: int
    files: dict[str, dict[str, int]]


class BaselineError(ValueError):
    """The baseline cannot safely authorize any current finding."""


def _validated_baseline(payload: Any) -> Baseline:
    if not isinstance(payload, dict):
        raise BaselineError("top level must be a JSON object")
    total = payload.get("total")
    files = payload.get("files")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise BaselineError("'total' must be a non-negative integer")
    if not isinstance(files, dict):
        raise BaselineError("'files' must be an object")

    known_keys = {rule.key for rule in RULES}
    validated: dict[str, dict[str, int]] = {}
    for path, classes in files.items():
        if not isinstance(path, str) or not path or not isinstance(classes, dict) or not classes:
            raise BaselineError("each file must have a non-empty object of rule counts")
        validated_classes: dict[str, int] = {}
        for key, count in classes.items():
            if key not in known_keys:
                raise BaselineError(f"{path!r} names unknown rule {key!r}")
            if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                raise BaselineError(f"{path!r}/{key!r} must be a positive integer")
            validated_classes[key] = count
        validated[path] = validated_classes

    computed_total = sum(sum(classes.values()) for classes in validated.values())
    if total != computed_total:
        raise BaselineError(
            f"'total' is {total}, but the per-file rule counts sum to {computed_total}"
        )
    if total != 0 or validated:
        raise BaselineError(
            "the comment-style baseline is strict zero; 'total' and 'files' must be empty"
        )
    return Baseline(total=total, files=validated)


def load_baseline() -> Baseline:
    if not BASELINE_PATH.exists():
        raise BaselineError(f"missing baseline: {BASELINE_PATH}")
    try:
        payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"cannot read {BASELINE_PATH}: {exc}") from exc
    return _validated_baseline(payload)


def write_baseline(counts: dict[str, dict[str, int]]) -> None:
    if counts:
        raise BaselineError("refusing to write a nonzero comment-style baseline")
    total = sum(sum(c.values()) for c in counts.values())
    payload = {
        "_comment": (
            "Strict zero baseline for scripts/check_comment_style.py. Generated — do not "
            "hand-edit. Nonzero totals and file allowances are rejected; see "
            "CONTRIBUTING.md#comments."
        ),
        "total": total,
        "files": counts,
    }
    BASELINE_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )


def report(violations: list[Violation]) -> None:
    by_key: dict[str, list[Violation]] = defaultdict(list)
    for v in violations:
        by_key[v.key].append(v)
    rules = {r.key: r for r in RULES}
    for key in (r.key for r in RULES):
        items = by_key.get(key, [])
        if not items:
            continue
        rule = rules[key]
        print(f"\n=== {key} — rule {rule.rule}: {rule.summary} ({len(items)}) ===")
        for v in items:
            print(f"  {v.path}:{v.line}: {v.text}")


@dataclass(frozen=True)
class BaselineDifference:
    path: str
    key: str
    current: int
    expected: int


def baseline_differences(
    counts: dict[str, dict[str, int]], baseline: Baseline
) -> list[BaselineDifference]:
    """Compare each file and rule so an allowance cannot move elsewhere."""
    differences: list[BaselineDifference] = []
    for path in sorted(set(counts) | set(baseline.files)):
        current_classes = counts.get(path, {})
        expected_classes = baseline.files.get(path, {})
        for key in sorted(set(current_classes) | set(expected_classes)):
            current = current_classes.get(key, 0)
            expected = expected_classes.get(key, 0)
            if current != expected:
                differences.append(BaselineDifference(path, key, current, expected))
    return differences


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="list every current violation")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the strict-zero baseline after a clean scan",
    )
    args = parser.parse_args(argv)

    try:
        violations = scan(source_paths())
    except (OSError, subprocess.CalledProcessError, ScanError) as exc:
        print(f"Comment-style scan failed: {exc}", file=sys.stderr)
        return 1
    counts = tally(violations)
    total = len(violations)

    if args.report:
        report(violations)
        print(f"\n{total} violations in {len(counts)} files")
        return 0

    try:
        baseline = load_baseline()
    except BaselineError as exc:
        print(f"Invalid comment-style baseline: {exc}", file=sys.stderr)
        return 1

    differences = baseline_differences(counts, baseline)

    if args.update_baseline:
        additions = [item for item in differences if item.current > item.expected]
        if additions:
            print("Refusing to write a nonzero comment-style baseline:\n", file=sys.stderr)
            for item in additions:
                print(
                    f"  {item.path}: {item.key} {item.current} current > {item.expected} reviewed",
                    file=sys.stderr,
                )
            print("\nResolve added findings before refreshing the baseline.", file=sys.stderr)
            return 1
        write_baseline(counts)
        print(
            f"baseline recorded: {total} violations in {len(counts)} files (was {baseline.total})"
        )
        return 0

    if differences:
        print("Comment-style findings differ from the reviewed baseline:\n", file=sys.stderr)
        for item in differences:
            rule = next(rule for rule in RULES if rule.key == item.key)
            print(
                f"  {item.path}: {item.key} {item.current} current != "
                f"{item.expected} reviewed — rule {rule.rule}: {rule.summary}",
                file=sys.stderr,
            )
        print(
            "\nRun `.venv/bin/python scripts/check_comment_style.py --report` to inspect "
            "the current findings, then resolve every finding to restore strict zero.",
            file=sys.stderr,
        )
        return 1

    print(f"comment style OK — {total} reviewed violations, exact baseline match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
