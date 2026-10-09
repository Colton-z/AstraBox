"""How the Assistant defaults combine with an Assistant's own fields."""
from __future__ import annotations

from typing import Any

import pytest

from astrabox.core.service.orchestrator.assistant.assistant_layers import (
    default_environment_name,
    joined_instructions,
    merged_engine_options,
)


def _env(name: str, engine: str = "assistant", enabled: bool = True) -> dict[str, Any]:
    return {"name": name, "engine_kind": engine, "enabled": enabled}


@pytest.mark.parametrize(
    ("layers", "environments", "expected"),
    [
        ([("defaults", {"environment_name": "chosen"})], [_env("a"), _env("b")], "chosen"),
        ([], [_env("only"), _env("agents", engine="claude_code"), _env("off", enabled=False)], "only"),
        ([], [_env("a"), _env("b")], None),
        ([], [_env("agents", engine="claude_code")], None),
        ([("defaults", {"environment_name": "  "})], [_env("only")], "only"),
    ],
    ids=["named", "single-eligible", "ambiguous", "none-eligible", "blank-is-unnamed"],
)
async def test_the_environment_a_new_assistant_uses(
    layers: list[tuple[str, dict[str, Any]]], environments: list[dict[str, Any]], expected: str | None
) -> None:
    assert await default_environment_name(layers, environments) == expected


def test_instructions_join_broadest_first_and_the_owners_last() -> None:
    layers = [("defaults", {"instructions": "Org rules."})]
    assert joined_instructions(layers, "Mine.") == "Org rules.\n\nMine."
    assert joined_instructions(layers, None) == "Org rules."
    assert joined_instructions([], "  ") is None


def test_an_assistants_option_block_replaces_the_inherited_block_of_its_name() -> None:
    layers = [("defaults", {"engine_options": {"config": {"a": 1}, "env": {"X": "1"}}})]
    assert merged_engine_options(layers, {"config": {"b": 2}}) == {
        "config": {"b": 2},
        "env": {"X": "1"},
    }
