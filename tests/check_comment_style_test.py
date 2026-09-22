from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import check_comment_style as checker


def _rule(key: str) -> checker.Rule:
    return next(rule for rule in checker.RULES if rule.key == key)


def _write_baseline(path: Path, *, total: int, files: dict[str, dict[str, int]]) -> None:
    path.write_text(json.dumps({"total": total, "files": files}), encoding="utf-8")


def test_javascript_lexer_finds_every_comment_form_as_a_complete_unit() -> None:
    source = """\
const endpoint = "https://example.test/used-to//WE";
const shade = "#fff";
const pattern = /[/*] OUR old implementation/;
const template = `// WE used to own this`;
const node = <p>it's // OUR old implementation</p>;
const value = 1; // WE used to own this
/*
 * The previous
 * implementation owned this.
 */
const view = <section>{/* OUR component was
 * moved here. */}</section>;
const interpolated = `${value /* WE no
 * longer own this */}`;
"""

    units = checker.javascript_comment_units(source, jsx=True)

    assert [line for line, _text in units] == [6, 7, 11, 13]
    assert len(units) == 4
    assert units[0][1] == "WE used to own this"
    assert "previous\nimplementation" in units[1][1]
    assert "OUR component was\nmoved here." in units[2][1]
    assert "WE no\nlonger own this" in units[3][1]


def test_javascript_lexer_ignores_comment_shapes_in_non_comment_tokens() -> None:
    source = """\
const endpoint = "https://example.test//WE-used-to";
const block = '/* OUR previous implementation */';
const template = `// WE used to own this`;
const regex = /[/*] OUR old implementation/;
const view = <p>it's // OUR old implementation</p>;
"""

    assert checker.javascript_comment_units(source, jsx=True) == []


@pytest.mark.parametrize(
    "parameters",
    [
        "T,",
        "T extends Entity",
        "T extends Record<string, unknown>",
        "T = string",
        "const T",
    ],
)
def test_tsx_generic_arrow_does_not_swallow_its_trailing_comment(parameters: str) -> None:
    source = f"const identity = <{parameters}>(value: T) => value; // OUR old path\n"

    assert checker.javascript_comment_units(source, jsx=True, typescript=True) == [
        (1, "OUR old path")
    ]


def test_multiline_tsx_generic_arrow_does_not_swallow_its_trailing_comment() -> None:
    source = """\
const identity = <T extends {
  value: string;
}>(value: T) => value; // TODO
"""

    assert checker.javascript_comment_units(source, jsx=True, typescript=True) == [(3, "TODO")]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "const f = <T,> /* TODO between */ (x: T) => x; // TODO trailing\n",
            [(1, " TODO between "), (1, "TODO trailing")],
        ),
        (
            "const f = <T /* > */ ,>(x: T) => x; // TODO trailing\n",
            [(1, " > "), (1, "TODO trailing")],
        ),
        (
            "const f = <T,>(x: T /* ) */) => x; // TODO trailing\n",
            [(1, " ) "), (1, "TODO trailing")],
        ),
        (
            "const f = <T,>(x: T) /* TODO between */ => x; // TODO trailing\n",
            [(1, " TODO between "), (1, "TODO trailing")],
        ),
        (
            "const f = <T,>(x = /\\)/) => x; // TODO trailing\n",
            [(1, "TODO trailing")],
        ),
        (
            "const f = <T,>(x = function () { return /\\)/; }) => x; // TODO trailing\n",
            [(1, "TODO trailing")],
        ),
        (
            "const f = <T,>(x = value! / total) => x; // TODO trailing\n",
            [(1, "TODO trailing")],
        ),
    ],
)
def test_tsx_generic_lookahead_ignores_comments_and_regex_tokens(
    source: str, expected: list[tuple[int, str]]
) -> None:
    assert checker.javascript_comment_units(source, jsx=True, typescript=True) == expected


def test_plain_tsx_type_tag_is_not_reclassified_as_a_generic_arrow() -> None:
    source = "const view = <T>(value) => // TODO</T>;\n"

    assert checker.javascript_comment_units(source, jsx=True, typescript=True) == []


def test_tsx_generic_guard_does_not_reclassify_an_actual_jsx_element() -> None:
    source = "const view = <T>{/* OUR old path */}</T>;\n"

    assert checker.javascript_comment_units(source, jsx=True, typescript=True) == [
        (1, " OUR old path ")
    ]


def test_javascript_groups_only_consecutive_pure_line_comments() -> None:
    source = """\
// context
// component used
// to own this state
first(); // component used
second(); // to own this state
"""

    units = checker.javascript_comment_units(source)

    assert units == [
        (1, "context\ncomponent used\nto own this state"),
        (4, "component used"),
        (5, "to own this state"),
    ]
    assert checker._violation_location(1, units[0][1], _rule("timeline"))[0] == 2
    assert not _rule("timeline").hits(units[1][1])
    assert not _rule("timeline").hits(units[2][1])


@pytest.mark.parametrize(
    "source",
    [
        "const ratio = value! / total; // TODO\n",
        "counter++ / total; // TODO\n",
        "counter-- / total; // TODO\n",
    ],
)
def test_javascript_postfix_operators_keep_following_slash_as_division(source: str) -> None:
    assert checker.javascript_comment_units(source, typescript=True) == [(1, "TODO")]


def test_css_lexer_finds_blocks_but_not_urls_colours_or_strings() -> None:
    source = """\
:root { --shade: #fff; }
.hero { background: url(https://example.test/image.svg); }
.hero::after { content: "/* OUR old implementation */"; }
.hero { display: block; } /* WE used
 * to own this. */
// OUR old implementation
"""

    assert checker.css_comment_units(source) == [(4, " WE used\nto own this. ")]


def test_python_uses_tokens_and_keeps_each_docstring_whole() -> None:
    source = '''"""The previous
implementation owned this."""

value = "# WE used to own this"
active = True  # OUR code owns this
'''

    units = checker.python_units(source)

    assert units == [
        (5, "OUR code owns this"),
        (1, "The previous\nimplementation owned this."),
    ]
    assert _rule("timeline").hits(units[1][1])


def test_hash_scanner_keeps_full_line_only_behavior_for_configuration() -> None:
    source = 'url = "# WE used to own this"\nvalue = 1 # OUR old value\n  # TODO owner\n'

    assert checker.hash_comment_units(source) == [(3, "TODO owner")]


def test_env_scanner_skips_commented_assignments_but_reads_prose() -> None:
    source = "# Optional credential.\n#ASTRABOX_REGION=us-west-2\n"

    assert checker.hash_comment_units(source, ignore_commented_env_assignments=True) == [
        (1, "Optional credential.")
    ]


def test_shell_scanner_finds_trailing_comments_without_reading_hash_values() -> None:
    source = """\
url="https://example.test/#fragment"
trimmed=${name#prefix}
escaped=literal\\#hash
command || true  # WE judge the result here
# component used
# to own this state
"""

    assert checker.shell_comment_units(source) == [
        (4, "WE judge the result here"),
        (5, "component used\nto own this state"),
    ]


def test_shell_scanner_ignores_heredoc_payload_comments() -> None:
    source = "cat <<'EOF'\n# TODO payload\nEOF\n"

    assert checker.shell_comment_units(source) == []


def test_shell_scanner_enters_command_substitution_inside_double_quotes() -> None:
    source = 'value="$(echo ok # TODO\n)"\n'

    assert checker.shell_comment_units(source) == [(1, "TODO")]


def test_shell_scanner_preserves_word_boundary_across_line_continuation() -> None:
    source = "printf '%s\\n' value\\\n# TODO\n"

    assert checker.shell_comment_units(source) == []


def test_shell_scanner_does_not_treat_here_string_tail_as_a_heredoc() -> None:
    source = 'cat <<< "# TODO payload"\n# TODO real\n'

    assert checker.shell_comment_units(source) == [(2, "TODO real")]


def test_shell_scanner_does_not_treat_arithmetic_shift_as_a_heredoc() -> None:
    source = "(( value = 1 << 2 ))\n# TODO real\n"

    assert checker.shell_comment_units(source) == [(2, "TODO real")]


def test_shell_scanner_reads_comments_inside_multiline_arithmetic() -> None:
    source = "(( value = 1\n# TODO arithmetic\n+ 2 ))\n"

    assert checker.shell_comment_units(source) == [(2, "TODO arithmetic")]


def test_shell_scanner_understands_ansi_quoted_heredoc_delimiter() -> None:
    source = "cat <<$'EOF'\n# TODO payload\nEOF\n# TODO real\n"

    assert checker.shell_comment_units(source) == [(4, "TODO real")]


def test_shell_scanner_ignores_hash_inside_ansi_quoted_word() -> None:
    source = "value=$'can\\'t # TODO data'\n# TODO real\n"

    assert checker.shell_comment_units(source) == [(2, "TODO real")]


def test_shell_scanner_keeps_case_pattern_parens_inside_command_substitution() -> None:
    source = """\
value="$(case x in
  x) echo ok # TODO inner
  ;;
esac)"
# TODO outer
"""

    assert checker.shell_comment_units(source) == [
        (2, "TODO inner"),
        (5, "TODO outer"),
    ]


def test_shell_scanner_does_not_treat_case_arguments_as_case_syntax() -> None:
    source = 'value="$(echo case in)"\n# TODO real\n'

    assert checker.shell_comment_units(source) == [(2, "TODO real")]


@pytest.mark.parametrize("arguments", ["if case in", "x then case in"])
def test_shell_scanner_does_not_treat_command_prefix_arguments_as_syntax(arguments: str) -> None:
    source = f'value="$(printf {arguments})"\n# TODO outer\n# TODO tail\n'

    assert checker.shell_comment_units(source) == [(2, "TODO outer\nTODO tail")]


def test_shell_scanner_accepts_optional_case_pattern_parenthesis() -> None:
    source = """\
value="$(case x in
  (x) echo ok # TODO inner
  ;;
esac)"
# TODO outer
"""

    assert checker.shell_comment_units(source) == [
        (2, "TODO inner"),
        (5, "TODO outer"),
    ]


@pytest.mark.parametrize("pattern", ["*", "?", "1", "'quoted'"])
def test_shell_scanner_closes_nonword_case_patterns(pattern: str) -> None:
    source = f"""value="$(case x in
  {pattern}) echo ok # TODO inner
  ;;
esac)"
# TODO outer
# TODO tail
"""

    assert checker.shell_comment_units(source) == [
        (2, "TODO inner"),
        (5, "TODO outer\nTODO tail"),
    ]


@pytest.mark.parametrize(
    "prefix",
    [
        "if ",
        "then ",
        "! ",
        "{ ",
    ],
)
def test_shell_scanner_recognizes_case_after_command_prefix(prefix: str) -> None:
    if prefix == "if ":
        body = "if case x in x) echo ok # TODO inner\n;; esac\nthen echo yes; fi"
    elif prefix == "then ":
        body = "if true; then case x in x) echo ok # TODO inner\n;; esac; fi"
    elif prefix == "! ":
        body = "! case x in x) echo ok # TODO inner\n;; esac"
    else:
        body = "{ case x in x) echo ok # TODO inner\n;; esac; }"
    source = f'value="$( {body} )"\n# TODO outer\n'
    outer_line = source[: source.index("# TODO outer")].count("\n") + 1

    assert checker.shell_comment_units(source) == [
        (1, "TODO inner"),
        (outer_line, "TODO outer"),
    ]


def test_shell_scanner_understands_ansi_quotes_inside_parameter_expansion() -> None:
    source = "value=\"${x:-$'can\\'t'}\"\n# TODO real\n"

    assert checker.shell_comment_units(source) == [(2, "TODO real")]


def test_shell_scanner_decodes_ansi_quoted_heredoc_delimiter() -> None:
    source = "cat <<$'E\\x4fF'\n# TODO payload\nEOF\n# TODO real\n"

    assert checker.shell_comment_units(source) == [(4, "TODO real")]


def test_shell_scanner_preserves_unicode_ansi_heredoc_delimiter() -> None:
    source = "cat <<$'结束'\n# TODO payload\n结束\n# TODO real\n"

    assert checker.shell_comment_units(source) == [(4, "TODO real")]


def test_python_groups_consecutive_pure_comments_but_not_trailing_comments() -> None:
    source = """\
# context
# component used
# to own this state
first = 1  # component used
second = 2  # to own this state
"""

    units = checker.python_units(source)

    assert units == [
        (1, "context\ncomponent used\nto own this state"),
        (4, "component used"),
        (5, "to own this state"),
    ]
    assert checker._violation_location(1, units[0][1], _rule("timeline"))[0] == 2
    assert not any(_rule("timeline").hits(text) for _line, text in units[1:])


def test_hash_scanner_groups_consecutive_comment_paragraphs() -> None:
    source = """\
# context
# component used
# to own this state
#
# another paragraph
value = 1 # component used
# to own this state
"""

    units = checker.hash_comment_units(source)

    assert units == [
        (1, "context\ncomponent used\nto own this state"),
        (5, "another paragraph"),
        (7, "to own this state"),
    ]
    assert _rule("timeline").hits(units[0][1])
    assert not any(_rule("timeline").hits(text) for _line, text in units[1:])


def test_html_scanner_does_not_treat_embedded_code_as_a_comment() -> None:
    source = """<script>const sample = "<!-- TODO -->";</script>
  <!-- The previous
  implementation owned this. -->
"""

    assert checker.html_comment_units(source) == [
        (2, " The previous\n  implementation owned this. ")
    ]


@pytest.mark.parametrize("word", ["we", "WE", "We", "our", "OUR", "My", "MY"])
def test_first_person_rule_covers_capitalization(word: str) -> None:
    assert _rule("first-person").hits(f"{word} own this branch")


@pytest.mark.parametrize("name", ["Let's Encrypt", "LET'S ENCRYPT"])
def test_first_person_rule_preserves_lets_encrypt(name: str) -> None:
    assert not _rule("first-person").hits(name)


def test_rule_matching_crosses_decorated_block_lines() -> None:
    [(line, text)] = checker.javascript_comment_units(
        "/* The component used\n * to own this state. */"
    )

    assert line == 1
    assert _rule("timeline").hits(text)
    assert checker._violation_location(line, text, _rule("timeline")) == (
        1,
        "The component used to own this state.",
    )


def test_private_infra_location_crosses_five_decorated_lines() -> None:
    [(line, text)] = checker.javascript_comment_units(
        "/* safe\n * verified\n * against\n * the\n * live\n * store */"
    )

    assert checker._violation_location(line, text, _rule("private-infra"))[0] == 2


@pytest.mark.parametrize(
    "text",
    [
        "This footer once carried peer navigation.",
        "The state was extracted verbatim from SessionPageReady.",
        "It stays the same as before.",
        "This state was until now invisible.",
        "The original version linked these values.",
        "See HEAD~1 for the deleted block.",
        "The console before the rails were unified used this colour.",
        "The mapping was chosen before the re-vendor.",
        "Recovered from the deleted file with git show HEAD:path.",
    ],
)
def test_timeline_rule_catches_narrow_repository_history_phrases(text: str) -> None:
    assert _rule("timeline").hits(text)


@pytest.mark.parametrize(
    "text",
    [
        "Return the original value when validation succeeds.",
        "The bytes are extracted verbatim from the HTTP response.",
    ],
)
def test_timeline_rule_preserves_current_data_flow_phrases(text: str) -> None:
    assert not _rule("timeline").hits(text)


@pytest.mark.parametrize(
    "text",
    [
        "The defect this covers cost a full deploy round.",
        "Every other gate passed it and nothing was watching.",
        "The error surfaced only when somebody opened a conversation.",
        "The branch was caught only after a live restart.",
        "This asserted the opposite because the behavior was never measured.",
        "A deletion by line range removed the decorator.",
        "This was a literal five in two places.",
        "Measured on a fresh worker, the next probe failed.",
        "The failure this pins is verbatim from a real restart.",
        "The value stayed at whatever the first deploy wrote.",
        "The component survived a green test.",
    ],
)
def test_incident_story_rule_catches_repository_and_deployment_history(
    text: str,
) -> None:
    assert _rule("incident-story").hits(text)


@pytest.mark.parametrize(
    "text",
    [
        "The second worker dies before committing its lease.",
        "The request timeout is the ceiling for a lending create.",
        "The test starts with an empty Pool and expects a new sandbox.",
    ],
)
def test_incident_story_rule_preserves_current_scenarios(text: str) -> None:
    assert not _rule("incident-story").hits(text)


def test_scan_reports_trailing_jsx_comment_at_its_source_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "const view = <Panel>{/* safe */}</Panel>; // OUR old implementation\n"
    (tmp_path / "view.tsx").write_text(source, encoding="utf-8")
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)

    violations = checker.scan(["view.tsx"])

    assert {(item.line, item.key) for item in violations} == {
        (1, "first-person"),
        (1, "timeline"),
    }


def test_scan_uses_the_typescript_guard_for_tsx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "const identity = <T,>(value: T) => value; // OUR old implementation\n"
    (tmp_path / "generic.tsx").write_text(source, encoding="utf-8")
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)

    violations = checker.scan(["generic.tsx"])

    assert {(item.line, item.key) for item in violations} == {
        (1, "first-person"),
        (1, "timeline"),
    }


@pytest.mark.parametrize("suffix", [".cjs", ".mjs"])
def test_scan_treats_javascript_module_suffixes_as_javascript(
    suffix: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_path = tmp_path / f"runner{suffix}"
    module_path.write_text(
        'const value = "<!-- OUR old implementation -->"; // OUR old implementation\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)

    violations = checker.scan([module_path.name])

    assert {(item.line, item.key) for item in violations} == {
        (1, "first-person"),
        (1, "timeline"),
    }


def test_source_paths_include_mjs_but_exclude_generated_schema() -> None:
    paths = checker.source_paths()

    assert "tests/e2e-ui/run-round.mjs" in paths
    assert "astrabox/core/service/orchestrator/runtime/provision-conversation" in paths
    assert ".env.example" in paths
    assert "Makefile" in paths
    assert "containers/coredns/sandbox-edge.Corefile" in paths
    assert "website/static/img/astrabox-mark.svg" in paths
    assert "channel-gateway/vendor/file-type-compat/index.cjs" not in paths
    assert not any(path.startswith("frontend/src/components/ai-elements/") for path in paths)
    assert "frontend/src/api/schema.d.ts" not in paths


@pytest.mark.parametrize("path", sorted(checker.SPECIAL_SOURCE_KINDS))
def test_every_special_source_path_is_selected(path: str) -> None:
    assert path in checker.source_paths()


def test_nonzero_baseline_is_rejected_even_when_it_matches_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_path = tmp_path / "baseline.json"
    _write_baseline(
        baseline_path,
        total=1,
        files={"old.ts": {"timeline": 1}},
    )
    monkeypatch.setattr(checker, "BASELINE_PATH", baseline_path)
    monkeypatch.setattr(checker, "source_paths", lambda: ["old.ts"])
    monkeypatch.setattr(
        checker,
        "scan",
        lambda _paths: [checker.Violation("old.ts", 8, "timeline", "used to")],
    )

    assert checker.main([]) == 1
    assert "baseline is strict zero" in capsys.readouterr().err


def test_update_baseline_refuses_a_finding_from_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_path = tmp_path / "baseline.json"
    _write_baseline(baseline_path, total=0, files={})
    monkeypatch.setattr(checker, "BASELINE_PATH", baseline_path)
    monkeypatch.setattr(checker, "source_paths", lambda: ["new.ts"])
    monkeypatch.setattr(
        checker,
        "scan",
        lambda _paths: [checker.Violation("new.ts", 1, "first-person", "OUR state")],
    )

    assert checker.main(["--update-baseline"]) == 1
    assert "Refusing to write a nonzero" in capsys.readouterr().err


@pytest.mark.parametrize("payload", [b"\xff# TODO\n"])
def test_scan_fails_loudly_on_invalid_utf8(
    payload: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bad.py").write_bytes(payload)
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)

    with pytest.raises(checker.ScanError, match=r"cannot read bad\.py"):
        checker.scan(["bad.py"])


def test_scan_fails_loudly_when_a_selected_file_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)

    with pytest.raises(checker.ScanError, match=r"cannot read missing\.py"):
        checker.scan(["missing.py"])


def test_main_reports_a_scan_failure_as_a_failed_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(checker, "source_paths", lambda: ["missing.py"])

    assert checker.main(["--report"]) == 1
    assert "Comment-style scan failed: cannot read missing.py" in capsys.readouterr().err


def test_corrupt_baseline_total_fails_loudly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_path = tmp_path / "baseline.json"
    _write_baseline(
        baseline_path,
        total=2,
        files={"old.ts": {"timeline": 1}},
    )
    monkeypatch.setattr(checker, "BASELINE_PATH", baseline_path)
    monkeypatch.setattr(checker, "source_paths", lambda: [])
    monkeypatch.setattr(checker, "scan", lambda _paths: [])

    assert checker.main([]) == 1
    assert "per-file rule counts sum to 1" in capsys.readouterr().err


def test_zero_baseline_requires_an_exact_zero_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_path = tmp_path / "baseline.json"
    _write_baseline(baseline_path, total=0, files={})
    monkeypatch.setattr(checker, "BASELINE_PATH", baseline_path)
    monkeypatch.setattr(checker, "source_paths", lambda: [])
    monkeypatch.setattr(checker, "scan", lambda _paths: [])

    assert checker.main([]) == 0
    assert "0 reviewed violations, exact baseline match" in capsys.readouterr().out
