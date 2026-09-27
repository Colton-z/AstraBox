"""The Dockerfile mode check reads Dockerfiles the way the builder does.

The check is only as good as its parser: a copy it misses stays at the
builder's umask, and a copy it invents fails a clean tree. These cases are
the parts of the Dockerfile syntax the check depends on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_dockerfile_modes as check

_HEAD = "# syntax=docker/dockerfile:1\nFROM debian AS base\n"


def _lines(text: str) -> list[int]:
    return [int(problem.split(":")[1]) for problem in check.violations(Path("D"), text)
            if problem.count(":") >= 2 and problem.split(":")[1].isdigit()]


def test_a_context_copy_without_a_mode_is_reported_at_its_first_line() -> None:
    text = _HEAD + "COPY a.py \\\n  b.py /opt/\nADD c.tgz /opt/\n"
    assert _lines(text) == [3, 5]


def test_copies_that_do_not_read_the_build_context_are_exempt() -> None:
    text = _HEAD + (
        "COPY --from=base /x /x\n"
        "COPY --link --from=golang:1.22 /usr/local/go /go\n"
        "COPY <<EOF /etc/x.conf\n"
        "COPY a.py /never-read-as-an-instruction\n"
        "EOF\n"
    )
    assert check.violations(Path("D"), text) == []


def test_a_remote_add_does_not_read_the_build_context_but_a_local_one_does() -> None:
    text = _HEAD + (
        "ADD --checksum=sha256:00 https://example.com/a.tar.gz /a.tar.gz\n"
        "ADD https://github.com/moby/buildkit.git#v0.10.1 /buildkit\n"
        "ADD https://example.com/a.tar.gz local.tar.gz /both/\n"
    )
    assert _lines(text) == [5]


def test_the_exec_form_is_read_as_json() -> None:
    text = _HEAD + 'COPY ["a b.py", "/opt/"]\nCOPY --chmod=0644 ["c.py", "/opt/"]\n'
    assert _lines(text) == [3]


def test_a_pinned_mode_passes_in_either_notation() -> None:
    text = _HEAD + "COPY --chmod=0644 a /a\nCOPY --chown=1000 --chmod=u=rwX,go=rX b/ /b/\n"
    assert check.violations(Path("D"), text) == []


def test_comment_lines_inside_a_continuation_do_not_end_the_instruction() -> None:
    text = _HEAD + "RUN true \\\n# COPY x /x\n    && true\nCOPY --chmod=0644 a /a\n"
    assert check.violations(Path("D"), text) == []


def test_a_heredoc_body_is_not_read_as_instructions() -> None:
    text = _HEAD + "RUN <<-SCRIPT\n\tCOPY a /a\n\tSCRIPT\nCOPY b /b\n"
    assert _lines(text) == [6]


def test_a_symbolic_mode_needs_a_syntax_that_supports_it() -> None:
    body = "FROM debian\nCOPY --chmod=u=rwX,go=rX a /a\n"
    assert check.violations(Path("D"), "# syntax=docker/dockerfile:1\n" + body) == []
    assert check.violations(Path("D"), "# syntax=docker/dockerfile:1.14\n" + body) == []
    for syntax in ("# syntax=docker/dockerfile:1.10\n", ""):
        assert any("symbolic --chmod" in p for p in check.violations(Path("D"), syntax + body))


@pytest.mark.parametrize(
    "text",
    ["# escape=`\nFROM debian\n", _HEAD + "RUN <<EOF\necho never closed\n"],
    ids=["escape-directive", "unclosed-heredoc"],
)
def test_a_dockerfile_the_check_cannot_read_is_an_error_not_a_pass(text: str) -> None:
    with pytest.raises(check.DockerfileError):
        check.violations(Path("D"), text)
