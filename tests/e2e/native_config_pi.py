"""Verify Agent JSON reaches Pi's actual per-conversation settings file."""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from tests.e2e._sandbox_helpers import (
    create_agent_variant_session,
    data,
    engine_kind,
    get_session,
    poll_until_agent_ready,
)
from tests.e2e.test_transcript_mirror import _assert_pi_settings


def verify_native_config(client: httpx.Client) -> None:
    """Save via the API, start Pi, and read back the native configuration node."""

    assert engine_kind() == "pi", "Pi native configuration proof requires the Pi shard"
    settings: dict[str, Any] = {
        "defaultThinkingLevel": "off",
        "compaction": {"enabled": False, "keepRecentTokens": 12000},
        "futureVendorSetting": {
            "marker": uuid.uuid4().hex,
            "nested": [True, None, {"literal": "中文 'quoted' $(unchanged)"}],
        },
    }
    created = create_agent_variant_session(
        client, name_suffix="native-settings", engine_options={"settings": settings},
    )
    sid = str(created.get("session_id") or "")
    assert sid, created
    poll_until_agent_ready(client, sid)
    detail = get_session(client, sid)
    agent_id = str(detail.get("agent_id") or created.get("agent_id") or "")
    assert agent_id, detail
    stored = data(client.get(f"/api/v1/agents/{agent_id}"))
    assert stored["engine_options"]["settings"] == settings
    _assert_pi_settings(client, sid, settings)
