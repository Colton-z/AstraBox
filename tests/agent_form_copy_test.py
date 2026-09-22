"""Every editable agent field has copy, in both locales.

The agent form is schema-driven: ``agent_schema.py`` owns a field's SHAPE and
says so in its own docstring — "the schema carries shape, never copy" — leaving
the label and help to the frontend i18n catalogue, keyed by the field's ``key``.
That split is what lets a field reach the console without a frontend change, and
it is exactly why a new field arrives with no copy at all: the form renders it
regardless, and a missing key degrades to the key itself rather than to an error.
So an operator sees ``agent_form.fields.model.label`` where "Model" belongs.

This is the agent twin of ``tests/environment_form_copy_test.py``; it lives in a
separate file because the two catalogues are separate (``misc:agent_form`` for
the agent, ``manage:env_form`` for the environment).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from astrabox.core.service.orchestrator.agent_schema import (
    AGENT_FIELD_SCHEMA,
    AGENT_GROUPS,
)

_LOCALES = ("en", "zh")
_REPO_ROOT = Path(__file__).resolve().parents[1]


_AGENT_EDIT_CONFIG = (
    _REPO_ROOT / "frontend" / "src" / "manage" / "agentEditConfig.ts"
)
_INLINE_SET = re.compile(
    r"INLINE_ITEM_SCHEMA_KEYS\s*=\s*new Set(?:<[^>]*>)?\(\s*\[(?P<body>[^\]]*)\]",
)


def _inline_item_schema_keys() -> set[str]:
    """Which object blocks the agent form renders as individual controls.

    Parsed from the renderer instead of mirrored, so this cannot quietly
    disagree with it. A rename raises rather than returning an empty set: an
    empty set is also a legitimate answer, so a parser that failed softly would
    leave the assertion passing for a reason nobody could see.
    """
    source = _AGENT_EDIT_CONFIG.read_text(encoding="utf-8")
    match = _INLINE_SET.search(source)
    if match is None:
        raise AssertionError(
            f"INLINE_ITEM_SCHEMA_KEYS not found in {_AGENT_EDIT_CONFIG}; this "
            "test can no longer tell which sub-fields are rendered"
        )
    return set(re.findall(r"['\"]([^'\"]+)['\"]", match.group("body")))


def _agent_form_for(locale: str) -> dict[str, dict]:
    path = _REPO_ROOT / "frontend" / "src" / "i18n" / "locales" / locale / "misc.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    return document.get("agent_form") or {}


@pytest.mark.parametrize("locale", _LOCALES)
def test_every_schema_field_has_a_label(locale: str) -> None:
    """A label is what stands between a field and its raw key on screen.

    Only the label is required. ``help`` is genuinely optional — a field whose
    label already says the whole thing does not need a paragraph, and demanding
    one would push someone to write filler.
    """
    copy = _agent_form_for(locale).get("fields") or {}
    missing = [
        str(field["key"])
        for field in AGENT_FIELD_SCHEMA
        if not (copy.get(str(field["key"])) or {}).get("label")
    ]
    assert not missing, (
        f"{locale}: these agent fields would render their raw i18n key to an "
        f"operator: {missing}. Add misc:agent_form.fields.<key>.label."
    )


@pytest.mark.parametrize("locale", _LOCALES)
def test_every_group_has_a_label(locale: str) -> None:
    """Groups are the form's section headings — a missing one is a raw key too.

    The environment form has no groups, so this half has no twin to inherit
    from; the agent form renders one heading per group id and needs each named.
    """
    copy = _agent_form_for(locale).get("groups") or {}
    missing = [
        str(group["id"])
        for group in AGENT_GROUPS
        if not (copy.get(str(group["id"])) or {}).get("label")
    ]
    assert not missing, (
        f"{locale}: these agent form sections would render their raw i18n key: "
        f"{missing}. Add misc:agent_form.groups.<id>.label."
    )


@pytest.mark.parametrize("locale", _LOCALES)
def test_every_inlined_sub_field_has_a_label(locale: str) -> None:
    """A sub-field rendered as its own control needs a label of its own.

    Carrying an ``item_schema`` is not what puts a sub-field on screen — the
    form decides that. Every object block here is edited through the JSON
    escape hatch unless its key is in the renderer's inline set, and a sub-field
    nobody renders has nothing to label. So the requirement follows the set:
    while it is empty this asks for nothing, and a block added to it must bring
    its copy — which is why inlining needs no separate reminder elsewhere.

    Read from the renderer rather than restated here. A second copy of the set
    would agree on the day it was written and drift afterwards, and the drift
    would be silent in the direction that matters: demanding copy for controls
    that do not exist, or excusing controls that do.
    """
    inlined = _inline_item_schema_keys()
    copy = _agent_form_for(locale).get("fields") or {}
    missing: list[str] = []
    for field in AGENT_FIELD_SCHEMA:
        if str(field["key"]) not in inlined:
            continue
        parent = copy.get(str(field["key"])) or {}
        for item in field.get("item_schema") or []:
            entry = parent.get(str(item["key"]))
            if not isinstance(entry, dict) or not entry.get("label"):
                missing.append(f"{field['key']}.{item['key']}")
    assert not missing, (
        f"{locale}: these sub-fields are rendered as their own controls and "
        f"would show their raw i18n key: {missing}. Add "
        f"misc:agent_form.fields.<parent>.<sub>.label."
    )


def test_the_two_locales_describe_the_same_fields() -> None:
    """A field translated in one locale only is half-shipped.

    Asymmetry is how a locale silently falls behind: the default locale looks
    finished, and the other one shows keys to whoever reads it.
    """
    # Inlined sub-field copy nests under its parent inside `fields`, so the two
    # blocks below cover it too.
    for block in ("fields", "groups"):
        english, chinese = (
            set(_agent_form_for(locale).get(block) or {}) for locale in _LOCALES
        )
        assert english == chinese, (
            f"agent form {block} copy is asymmetric between locales: "
            f"only in en={sorted(english - chinese)}, only in zh={sorted(chinese - english)}"
        )


def test_the_catalogue_carries_no_copy_for_a_field_that_is_gone() -> None:
    """Translation catalogues only describe fields declared by the Agent schema."""
    declared = {str(f["key"]) for f in AGENT_FIELD_SCHEMA}
    for locale in _LOCALES:
        stale = sorted(set(_agent_form_for(locale).get("fields") or {}) - declared)
        assert not stale, (
            f"{locale}: misc:agent_form.fields describes fields the agent schema "
            f"does not declare: {stale}"
        )
