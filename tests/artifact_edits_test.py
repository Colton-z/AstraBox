"""Exact replacements fail without allowing a partial publish."""

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.artifacts.edits import apply_edits


def test_ordered_replacements_and_replace_all() -> None:
    assert (
        apply_edits(
            "abc abc",
            [
                {"old_string": "abc", "new_string": "x", "replace_all": True},
                {"old_string": "x x", "new_string": "done"},
            ],
        )
        == "done"
    )


@pytest.mark.parametrize(
    "edits",
    [
        [],
        [{"old_string": "missing", "new_string": "x"}],
        [{"old_string": "a", "new_string": "x"}],
        [{"old_string": "", "new_string": "x"}],
        [{"old_string": "aa", "new_string": "x", "replace_all": "yes"}],
        [{"old_string": "aa", "new_string": "x"}, {"old_string": "missing", "new_string": "y"}],
    ],
)
def test_absent_ambiguous_or_invalid_edits_reject(edits: list) -> None:
    with pytest.raises(APIError) as error:
        apply_edits("aa", edits)
    assert error.value.code == "ARTIFACT_EDIT_NOT_APPLICABLE"


def test_overlapping_occurrences_are_also_ambiguous() -> None:
    with pytest.raises(APIError):
        apply_edits("aaa", [{"old_string": "aa", "new_string": "x"}])
