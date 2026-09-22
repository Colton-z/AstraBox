"""``astrabox.yaml`` structural validation.

Every case here asserts the same intent: a document that is wrong in a way the
CLI can see is refused *before* any request is sent. A document that applies
half of itself and then fails leaves a deployment in a state neither the
document nor the previous configuration describes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from astrabox.cli.document import (
    DOCUMENT_VERSION,
    KIND_AGENT,
    KIND_ENVIRONMENT,
    load_document,
    parse_document,
)
from astrabox.cli.output import EXIT_USAGE, CliError


def _valid() -> dict:
    return {
        "version": DOCUMENT_VERSION,
        "environments": [{"name": "default", "endpoint_provider": "litellm"}],
        "agents": [{"name": "researcher", "model": "claude-opus-5", "environment_name": "default"}],
    }


def test_a_valid_document_parses_into_both_kinds() -> None:
    document = parse_document(_valid())

    assert [spec.name for spec in document.environments] == ["default"]
    assert [spec.name for spec in document.agents] == ["researcher"]
    assert document.environments[0].kind == KIND_ENVIRONMENT
    assert document.agents[0].kind == KIND_AGENT


def test_resources_are_ordered_environments_before_agents() -> None:
    """An Agent's environment_name is validated against an existing environment,
    so a first-run document declaring both must apply the environment first."""
    document = parse_document(_valid())

    assert [spec.kind for spec in document.resources()] == [KIND_ENVIRONMENT, KIND_AGENT]


def test_fields_are_carried_verbatim_with_no_cli_defaults() -> None:
    """A field the document omits is one the deployment's own default decides.
    The CLI adding a default here would silently overwrite a stored value."""
    document = parse_document(_valid())

    assert document.agents[0].fields == {
        "name": "researcher",
        "model": "claude-opus-5",
        "environment_name": "default",
    }


def test_an_unknown_top_level_key_is_refused() -> None:
    """A typo in a resource list name would otherwise parse as a document that
    declares nothing and apply successfully without touching anything."""
    raw = _valid()
    raw["agent"] = raw.pop("agents")

    with pytest.raises(CliError) as caught:
        parse_document(raw)

    assert caught.value.exit_code == EXIT_USAGE
    assert "agent" in caught.value.message


def test_an_unknown_document_version_is_refused() -> None:
    raw = _valid()
    raw["version"] = DOCUMENT_VERSION + 1

    with pytest.raises(CliError) as caught:
        parse_document(raw)

    assert caught.value.exit_code == EXIT_USAGE
    assert str(DOCUMENT_VERSION) in caught.value.message


def test_a_missing_version_is_refused() -> None:
    raw = _valid()
    del raw["version"]

    with pytest.raises(CliError) as caught:
        parse_document(raw)

    assert caught.value.exit_code == EXIT_USAGE


def test_a_resource_without_a_name_is_refused() -> None:
    """Name is the identity apply matches on; a nameless resource could only be
    created, never updated, so every apply would add another copy."""
    raw = _valid()
    raw["agents"] = [{"model": "claude-opus-5"}]

    with pytest.raises(CliError) as caught:
        parse_document(raw)

    assert caught.value.exit_code == EXIT_USAGE
    assert "no name" in caught.value.message


def test_a_blank_name_is_refused() -> None:
    raw = _valid()
    raw["agents"] = [{"name": "   ", "model": "claude-opus-5"}]

    with pytest.raises(CliError) as caught:
        parse_document(raw)

    assert caught.value.exit_code == EXIT_USAGE


def test_a_name_declared_twice_in_one_kind_is_refused() -> None:
    """Two entries with one name make the document's own meaning ambiguous:
    whichever applied last would win, silently discarding the other."""
    raw = _valid()
    raw["agents"] = [
        {"name": "researcher", "model": "claude-opus-5", "environment_name": "default"},
        {"name": "researcher", "model": "claude-sonnet-5", "environment_name": "default"},
    ]

    with pytest.raises(CliError) as caught:
        parse_document(raw)

    assert caught.value.exit_code == EXIT_USAGE
    assert "declared twice" in caught.value.message


def test_the_same_name_across_two_kinds_is_allowed() -> None:
    """Environments and Agents are separate collections on the deployment; one
    name in each is not a collision."""
    raw = _valid()
    raw["environments"] = [{"name": "shared"}]
    raw["agents"] = [{"name": "shared", "model": "claude-opus-5", "environment_name": "shared"}]

    document = parse_document(raw)

    assert document.environments[0].name == document.agents[0].name == "shared"


def test_a_resource_list_that_is_not_a_list_is_refused() -> None:
    raw = _valid()
    raw["agents"] = {"name": "researcher"}

    with pytest.raises(CliError) as caught:
        parse_document(raw)

    assert caught.value.exit_code == EXIT_USAGE


def test_an_omitted_resource_list_is_empty_not_an_error() -> None:
    """A document declaring only environments is valid: apply creates and
    updates what is declared and never prunes what is not."""
    document = parse_document({"version": DOCUMENT_VERSION, "environments": [{"name": "default"}]})

    assert document.agents == ()
    assert len(document.environments) == 1


def test_an_empty_document_is_refused() -> None:
    with pytest.raises(CliError) as caught:
        parse_document(None)

    assert caught.value.exit_code == EXIT_USAGE


def test_invalid_yaml_is_refused_with_the_path(tmp_path: Path) -> None:
    document_path = tmp_path / "astrabox.yaml"
    document_path.write_text("version: 1\nagents: [unclosed\n", encoding="utf-8")

    with pytest.raises(CliError) as caught:
        load_document(document_path)

    assert caught.value.exit_code == EXIT_USAGE
    assert str(document_path) in caught.value.message


def test_a_missing_file_is_refused_with_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "absent.yaml"

    with pytest.raises(CliError) as caught:
        load_document(missing)

    assert caught.value.exit_code == EXIT_USAGE
    assert str(missing) in caught.value.message


def test_load_document_reads_a_real_file(tmp_path: Path) -> None:
    document_path = tmp_path / "astrabox.yaml"
    document_path.write_text(
        "version: 1\n"
        "environments:\n"
        "  - name: default\n"
        "agents:\n"
        "  - name: researcher\n"
        "    model: claude-opus-5\n"
        "    environment_name: default\n",
        encoding="utf-8",
    )

    document = load_document(document_path)

    assert document.path == document_path
    assert document.agents[0].fields["model"] == "claude-opus-5"
