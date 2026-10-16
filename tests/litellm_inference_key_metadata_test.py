from __future__ import annotations

import pytest

from astrabox.providers.litellm_shared_auth import inference_key_metadata


def test_native_grant_preserves_identity_existing_permissions_and_input() -> None:
    original = {
        "astrabox_session_id": "session-a",
        "astrabox_user_id": "user-a",
        "trace_tags": ["existing"],
        "allowed_passthrough_routes": ["/existing-model-api"],
    }
    result = inference_key_metadata(original)
    assert result["astrabox_session_id"] == "session-a"
    assert result["astrabox_user_id"] == "user-a"
    assert result["trace_tags"] == ["existing"]
    assert result["allowed_passthrough_routes"] == ["/existing-model-api", "/v1/deepseek/v1/files"]
    assert original["allowed_passthrough_routes"] == ["/existing-model-api"]
    assert inference_key_metadata(result) == result


@pytest.mark.parametrize("routes", ["/all", {}, [None], [""]])
def test_invalid_existing_passthrough_grants_are_refused(routes) -> None:
    with pytest.raises(ValueError):
        inference_key_metadata({"allowed_passthrough_routes": routes})
