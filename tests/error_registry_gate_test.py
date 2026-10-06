"""The owner vocabulary that every registered error code draws from.

A row cannot invent an owner outside the vocabulary, and a code that nobody
registered says so instead of blaming the platform.
"""

from __future__ import annotations

import pytest


def test_a_row_cannot_invent_an_owner() -> None:
    from astrabox.common.utils.errors import ERROR_OWNERS, register_error

    with pytest.raises(ValueError) as caught:
        register_error("MADE_UP_OWNER_CODE", owner="whoever")

    assert "whoever" in str(caught.value)
    assert "unknown" not in ERROR_OWNERS, "the fallback's admission is not a row's answer"


def test_an_unregistered_code_owns_up_instead_of_blaming_the_platform() -> None:
    """`platform` was a claim about a code that can arrive from outside."""
    from astrabox.common.utils.errors import APIError, error_spec

    spec = error_spec("A_CODE_NOBODY_REGISTERED")
    envelope = APIError(
        code="A_CODE_NOBODY_REGISTERED", message="m", status_code=502
    ).to_error_envelope()

    assert spec.owner == "unknown"
    assert envelope["owner"] == "unknown"
    assert envelope["category"] == "unregistered"
