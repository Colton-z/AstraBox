"""Every plugin-repository definition failure names its owner.

`astrabox/common/utils/errors.py` answers `(status, category, retryable,
owner)` for a code it knows, and falls back to `category="unregistered"`,
`owner="platform"` for one it does not. That fallback is wrong for this family
by telling an operator the platform owns a `plugin_repos` field the operator
wrote.

Each case raises through the real code path and reads
`to_error_envelope()` — the mapping that reaches the client. Asserting the
envelope rather than the code constant is what makes a deleted registry row
fail here: the code spells itself correctly either way, and only the envelope
carries the four values the row decides.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any, Awaitable, Callable

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.plugin_repos import normalize_plugin_repos
from astrabox.core.service.orchestrator.runtime.storage import _plugin_cache


async def _plugin_repos_is_not_a_list() -> None:
    normalize_plugin_repos("git@example.invalid:acme/plugins.git")


async def _plugin_repo_names_an_unsupported_protocol() -> None:
    _plugin_cache._validate_plugin_repo_source(
        "ftp://example.invalid/plugins.git", "ftp", "plugin_repos[0]"
    )


# `owner` is the question each row exists to answer, so the table states it per
# case rather than deriving it from the category.
_CASES: tuple[tuple[str, Callable[[], Awaitable[None]], int, str, str], ...] = (
    ("PLUGIN_REPO_INVALID", _plugin_repos_is_not_a_list, 500, "runtime.plugin_repo", "template"),
    (
        "PLUGIN_REPO_UNSUPPORTED_PROTOCOL",
        _plugin_repo_names_an_unsupported_protocol,
        500,
        "runtime.plugin_repo",
        "template",
    ),
)


@pytest.mark.parametrize(
    ("code", "raise_it", "status_code", "category", "owner"),
    _CASES,
    ids=[case[0] for case in _CASES],
)
async def test_a_plugin_failure_reaches_the_client_with_its_owner(
    code: str,
    raise_it: Callable[[], Awaitable[None]],
    status_code: int,
    category: str,
    owner: str,
) -> None:
    with pytest.raises(APIError) as caught:
        await raise_it()

    envelope = caught.value.to_error_envelope()

    assert envelope["code"] == code
    assert envelope["status_code"] == status_code
    assert envelope["category"] == category
    assert envelope["owner"] == owner
    # Re-sending an identical request re-reads the same Agent field.
    assert envelope["retryable"] is False


async def test_no_plugin_failure_is_answered_by_the_unregistered_fallback() -> None:
    """The whole family, not one row at a time.

    A case added above with no registry row would pass its own assertions only
    by being written to match the fallback. `category="unregistered"` is the
    value nothing legitimate produces, so reading the same envelopes for it
    fails the omission here. `owner="platform"` is the fallback's other value
    but is a true answer for a deployment's own configuration, so it cannot be
    refused on sight.
    """
    envelopes: list[dict[str, Any]] = []
    for _code, raise_it, *_expected in _CASES:
        with pytest.raises(APIError) as caught:
            await raise_it()
        envelopes.append(caught.value.to_error_envelope())

    assert len(envelopes) == len(_CASES)
    assert [envelope["code"] for envelope in envelopes if envelope["category"] == "unregistered"] == []


def test_every_plugin_code_the_source_raises_has_a_case_here() -> None:
    """The table cannot be the reason a code goes unexamined.

    Reads the codes out of the source rather than the registry: a new plugin
    code with neither a registry row nor a case above is exactly the omission
    the cases cannot see, because a case that does not exist asserts nothing.
    """
    raised: set[str] = set()
    for path in pathlib.Path("astrabox").rglob("*.py"):
        raised.update(re.findall(r'"(PLUGIN_[A-Z0-9_]+)"', path.read_text(encoding="utf-8")))

    assert raised, "found no plugin codes in the source — the scan, not the family, is broken"
    assert raised == {case[0] for case in _CASES}
