"""Owned real-database bindings for runtime ensure, native child-list and history reads.

These cases make no model requests and do not start agent runtimes. Native
records are synthetic source fixtures; they exercise the actual repositories
and child service against the deployed relational database.
"""

import pytest

from astrabox.testing.relational_read_conformance import (
    verify_child_listing_indexes_native_relationships,
    verify_existing_runtime_ensure_is_read_only,
    verify_unchanged_history_folds_once,
)

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("database")]


async def test_existing_runtime_ensure_is_read_only() -> None:
    await verify_existing_runtime_ensure_is_read_only()


async def test_child_listing_indexes_native_relationships() -> None:
    await verify_child_listing_indexes_native_relationships()


async def test_unchanged_history_folds_once() -> None:
    await verify_unchanged_history_folds_once()
