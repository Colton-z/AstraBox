"""The runtime manifest is closed and validates every supported capability."""

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.artifacts.manifest import validate_manifest


def test_all_supported_capabilities_and_agent_are_validated() -> None:
    value = {
        "artifact": {},
        "db": {"rules": []},
        "sample": {},
        "agent": {},
        "user": {},
        "room": {"topics": {"chat": "interact"}},
        "downloads": True,
    }
    assert validate_manifest(value) == value


@pytest.mark.parametrize("name", ["permissions", "mcp", "files", "unknown"])
def test_unknown_and_built_in_names_are_not_declarations(name: str) -> None:
    with pytest.raises(APIError) as error:
        validate_manifest({name: {}})
    assert error.value.code == "ARTIFACT_CAPABILITY_UNKNOWN"


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"agent": True},
        {"agent": {"model": "x"}},
        {"downloads": {}},
        {"db": {"rules": [{}] * 65}},
        {"room": {"topics": {"x": "none"}}},
        {"room": {"topics": {"x": []}}},
    ],
)
def test_invalid_manifest_configuration(value: object) -> None:
    with pytest.raises(APIError) as error:
        validate_manifest(value)
    assert error.value.code == "ARTIFACT_MANIFEST_INVALID"
