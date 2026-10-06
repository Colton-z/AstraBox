"""Artifact query predicates use JSON values and do not cross bool/number types."""

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.artifacts.service import matches_where, validate_query


@pytest.mark.parametrize(
    "op,value,expected",
    [
        ("==", 3, True),
        ("==", 3.0, True),
        ("!=", 3, False),
        ("<", 4, True),
        ("<=", 3, True),
        (">", 2, True),
        (">=", 3, True),
        ("in", [2, 3], True),
        ("<", "4", False),
    ],
)
def test_comparison_operators(op: str, value, expected: bool) -> None:
    assert matches_where({"n": 3}, [{"field": "n", "op": op, "value": value}]) is expected


def test_missing_and_nested_fields_and_array_membership() -> None:
    assert matches_where({"nested": {"n": 3}}, [{"field": "nested.n", "op": "==", "value": 3}])
    assert not matches_where({}, [{"field": "n", "op": "!=", "value": 3}])
    assert not matches_where({"n": True}, [{"field": "n", "op": "==", "value": 1}])
    assert matches_where(
        {"tags": ["x", "y"]}, [{"field": "tags", "op": "array-contains", "value": "x"}]
    )
    assert not matches_where({"n": True}, [{"field": "n", "op": "in", "value": [1]}])
    assert not matches_where(
        {"tags": [True]}, [{"field": "tags", "op": "array-contains", "value": 1}]
    )
    assert not matches_where(
        {"data": {"n": True}}, [{"field": "data", "op": "==", "value": {"n": 1}}]
    )


@pytest.mark.parametrize(
    "where,order_by,limit",
    [
        ([{"field": "n", "op": "in", "value": 3}], [], 1),
        ([{"field": "n", "op": "unknown", "value": 3}], [], 1),
        ([], [{"field": "n", "direction": "sideways"}], 1),
        ([], [], 0),
        ([], [], 10001),
    ],
)
def test_invalid_query_shapes_are_refused(where, order_by, limit) -> None:
    with pytest.raises(APIError):
        validate_query(where, order_by, limit)
