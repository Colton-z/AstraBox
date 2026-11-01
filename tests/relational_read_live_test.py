"""Owned real-database bindings for runtime ensure and native child-list reads.

These cases make no model requests and do not start agent runtimes. Native
records are synthetic source fixtures; they exercise the actual repositories
and child service against the deployed relational database.
"""

import pytest

from astrabox.testing.relational_read_conformance import (
    verify_child_listing_indexes_native_relationships,
    verify_existing_runtime_ensure_is_read_only,
)

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("database")]


async def test_existing_runtime_ensure_is_read_only() -> None:
    await verify_existing_runtime_ensure_is_read_only()


async def test_child_listing_indexes_native_relationships() -> None:
    await verify_child_listing_indexes_native_relationships()
