"""Prefix rules preserve viewer privacy independently of role privileges."""

import pytest

from astrabox.core.service.artifacts.manifest import validate_rules
from astrabox.core.service.artifacts.rules import can_access


def test_root_defaults_and_deeper_overrides() -> None:
    rules = validate_rules([{"path": "scores", "read": "view", "write": "owner"}])
    assert can_access(rules, "public/one", "view", "alice")
    assert not can_access(rules, "public/one", "view", "alice", write=True)
    assert can_access(rules, "public/one", "interact", "alice", write=True)
    assert not can_access(rules, "scores/one", "interact", "alice", write=True)
    assert can_access(rules, "scores/one", "owner", "alice", write=True)


@pytest.mark.parametrize("level", ["view", "interact", "owner"])
def test_default_self_subtree_is_private_even_for_owner(level: str) -> None:
    assert can_access([], "data/users/alice/settings", level, "alice")
    assert not can_access([], "data/users/bob/settings", level, "alice")
    assert not can_access([], "data/users/alice/settings", level, None, guest=True)


def test_explicit_self_prefix_opens_only_when_both_levels_are_declared() -> None:
    rules = validate_rules([{"path": "profiles/{self}", "read": "view", "write": "interact"}])
    assert can_access(rules, "profiles/alice", "view", "alice")
    assert not can_access(rules, "profiles/bob", "owner", "alice")
    opened = validate_rules([*rules, {"path": "profiles", "read": "view", "write": "interact"}])
    assert can_access(opened, "profiles/bob", "view", "alice")
    assert not can_access(opened, "profiles/bob", "interact", None, guest=True, write=True)
