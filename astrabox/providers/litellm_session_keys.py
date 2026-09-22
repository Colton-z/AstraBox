"""Mint Session-scoped LiteLLM virtual keys at claim time.

A prepared engine child cannot carry Session identity in its frozen
environment, so identity rides the credential instead: the claim path mints a
key whose metadata names the Session, the egress sidecar substitutes the
child's per-slot placeholder for it, and the gateway's pre-call hook copies
the metadata into Langfuse's trace fields. Created at claim rather than
updated so the proxy's in-memory key cache can never serve a stale identity
for the first turn.
"""

from __future__ import annotations

import json
import os

import httpx

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.identity.session_signing import session_signing_secret
from astrabox.providers.litellm_shared_auth import (
    SANDBOX_INFERENCE_KEY_TYPE,
    session_inference_key,
)

logger = get_logger(__name__)

SESSION_KEY_METADATA_SESSION_FIELD = "astrabox_session_id"
SESSION_KEY_METADATA_USER_FIELD = "astrabox_user_id"


async def ensure_session_inference_key(
    session_id: str,
    *,
    user_id: str | None = None,
) -> str:
    """Create (or confirm) this Session's virtual key; return its value.

    Idempotent by construction: the value is derived, so a re-claim after
    recovery asks the gateway for the same key and treats "already exists" as
    success. Any other refusal is raised — a Session whose key cannot exist
    would spend an identity the gateway refuses on its first model call, and
    that must fail the claim, not the turn.
    """

    from astrabox.providers.model import LiteLLMModelEndpointProvider

    target = str(session_id or "").strip()
    if not target:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="a session inference key requires a session id",
            status_code=500,
        )
    master_key = str(os.environ.get("LITELLM_MASTER_KEY") or "").strip()
    if not master_key:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                "cannot mint a Session model key without LITELLM_MASTER_KEY; "
                "the gateway management credential is a deployment input"
            ),
            status_code=500,
        )
    key = session_inference_key(session_signing_secret(), target)
    base = LiteLLMModelEndpointProvider.server_side_base_url().rstrip("/")
    payload = {
        "key": key,
        "key_alias": f"astrabox-session-{target}",
        "key_type": SANDBOX_INFERENCE_KEY_TYPE,
        "metadata": {
            SESSION_KEY_METADATA_SESSION_FIELD: target,
            **(
                {SESSION_KEY_METADATA_USER_FIELD: str(user_id or "").strip()}
                if str(user_id or "").strip()
                else {}
            ),
        },
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{base}/key/generate",
            content=json.dumps(payload),
            headers={
                "Authorization": f"Bearer {master_key}",
                "Content-Type": "application/json",
            },
        )
    if response.status_code == 200:
        logger.info("minted session model key: session=%s", target)
        return key
    body = response.text[:300]
    if "already exists" in body or "duplicate" in body.lower():
        return key
    raise APIError(
        code="AGENT_RUNTIME_ERROR",
        message=(
            f"the gateway refused this Session's model key "
            f"(HTTP {response.status_code}): {body}"
        ),
        status_code=502,
    )


async def delete_session_inference_key(session_id: str) -> bool:
    """Best-effort removal of a dead Session's key; True when deleted.

    Called by slot-ledger pruning, not teardown hot paths: a leaked key is
    spend-scoped and expires with the deployment's rotation, so cleanup is
    garbage collection, never a correctness gate.
    """

    target = str(session_id or "").strip()
    master_key = str(os.environ.get("LITELLM_MASTER_KEY") or "").strip()
    if not target or not master_key:
        return False
    from astrabox.providers.model import LiteLLMModelEndpointProvider

    key = session_inference_key(session_signing_secret(), target)
    base = LiteLLMModelEndpointProvider.server_side_base_url().rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{base}/key/delete",
            content=json.dumps({"keys": [key]}),
            headers={
                "Authorization": f"Bearer {master_key}",
                "Content-Type": "application/json",
            },
        )
    if response.status_code != 200:
        logger.warning(
            "session model key delete refused: session=%s status=%s",
            target,
            response.status_code,
        )
        return False
    return True
