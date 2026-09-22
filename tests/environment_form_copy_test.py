"""Every editable environment field has copy, in both locales.

The schema owns a field's SHAPE and the frontend i18n owns its LABEL AND HELP —
``environment_schema`` says so itself. Nothing enforced the second half, so a
field could be added to the schema, render on the admin form, and show the
operator its raw translation key. That is exactly what ``endpoint_provider`` and
``provider_access`` did until someone opened the page and looked.

A unit test cannot notice this: the form renders whatever the schema hands it,
and a missing key degrades to the key itself rather than to an error. So the
check has to be this one — the two halves compared directly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from astrabox.core.service.orchestrator.environment_schema import ENV_FIELD_SCHEMA

_LOCALES = ("en", "zh")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENVIRONMENT_EDIT_CONFIG = (
    _REPO_ROOT / "frontend" / "src" / "manage" / "environmentEditConfig.ts"
)
_INLINE_SET = re.compile(
    r"INLINE_ITEM_SCHEMA_KEYS\s*=\s*new Set(?:<[^>]*>)?\(\s*\[(?P<body>[^\]]*)\]",
)


def _inline_item_schema_keys() -> set[str]:
    """Which object blocks the environment form renders as individual controls.

    Parsed from the renderer instead of mirrored, so this cannot quietly
    disagree with it. A rename raises rather than returning an empty set: an
    empty set is also a legitimate answer, so a parser that failed softly would
    leave the assertion passing for a reason nobody could see.
    """
    source = _ENVIRONMENT_EDIT_CONFIG.read_text(encoding="utf-8")
    match = _INLINE_SET.search(source)
    if match is None:
        raise AssertionError(
            f"INLINE_ITEM_SCHEMA_KEYS not found in {_ENVIRONMENT_EDIT_CONFIG}; "
            "this test can no longer tell which sub-fields are rendered"
        )
    return {
        token.strip().strip("'\"")
        for token in match.group("body").split(",")
        if token.strip().strip("'\"")
    }


def _fields_for(locale: str) -> dict[str, dict[str, str]]:
    path = _REPO_ROOT / "frontend" / "src" / "i18n" / "locales" / locale / "manage.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    return (document.get("env_form") or {}).get("fields") or {}


@pytest.mark.parametrize("locale", _LOCALES)
def test_every_schema_field_has_a_label(locale: str) -> None:
    """A label is what stands between a field and its raw key on screen.

    Only the label is required. ``help`` is genuinely optional — a boolean whose
    label already says the whole thing does not need a paragraph, and demanding
    one would push someone to write filler.
    """
    copy = _fields_for(locale)
    missing = [
        str(field["key"])
        for field in ENV_FIELD_SCHEMA
        if not (copy.get(str(field["key"])) or {}).get("label")
    ]
    assert not missing, (
        f"{locale}: these environment fields would render their raw i18n key to "
        f"an operator: {missing}. Add manage:env_form.fields.<key>.label."
    )


@pytest.mark.parametrize("locale", _LOCALES)
def test_every_inlined_sub_field_has_a_label(locale: str) -> None:
    """A sub-field rendered as its own control needs a label of its own.

    Carrying an ``item_schema`` is not what puts a sub-field on screen — the
    form decides that. An object block is edited through the JSON escape hatch
    unless its key is in the renderer's inline set, and a sub-field nobody
    renders has nothing to label. ``provider_access`` is the standing example:
    it stays behind the JSON field because it carries a masked secret, so
    demanding copy for its sub-fields would demand copy for controls that do
    not exist.

    Read from the renderer rather than restated here. A second copy of the set
    would agree on the day it was written and drift afterwards, and the drift
    would be silent in the direction that matters: demanding copy for controls
    that do not exist, or excusing controls that do.
    """
    inlined = _inline_item_schema_keys()
    copy = _fields_for(locale)
    missing: list[str] = []
    for field in ENV_FIELD_SCHEMA:
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
        f"manage:env_form.fields.<parent>.<sub>.label."
    )


def test_the_two_locales_describe_the_same_fields() -> None:
    """A field translated in one locale only is half-shipped.

    Asymmetry is how a locale silently falls behind: the default locale looks
    finished, and the other one shows keys to whoever reads it.
    """
    english, chinese = (set(_fields_for(locale)) for locale in _LOCALES)
    assert english == chinese, (
        "environment form copy is asymmetric between locales: "
        f"only in en={sorted(english - chinese)}, only in zh={sorted(chinese - english)}"
    )
