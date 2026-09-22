"""The built-in model endpoint provider: ``litellm`` — one gateway, any model.

AstraBox has no per-vendor model adapters and no ungated passthrough. Every
sandbox's model traffic goes through a `LiteLLM <https://docs.litellm.ai>`_
proxy. Claude Code keeps speaking Anthropic Messages through ``/v1/messages``;
Hermes keeps speaking OpenAI Chat Completions through ``/chat/completions``.
The proxy maps both inputs to providers configured in ``model_list``
(Anthropic, OpenAI, Gemini, DeepSeek, vLLM, …), and every call is metered where
a passthrough would be blind (cost tracking, Langfuse, the console's model
dropdown).

The proxy is **embedded**: the server image carries LiteLLM in its own venv
and starts it alongside the platform (one container, second process), so a
bare ``docker run`` has the full multi-provider gateway. Wiring:

* ``ASTRABOX_LITELLM_BASE_URL`` unset (the default) — the embedded proxy.
  With protected delivery on, sandboxes use the private
  ``gateway.astrabox.test`` name on HTTP port 80; the bundled resolver maps it
  to the server container without involving public DNS. With protection
  explicitly off, the direct platform address is used. Server-side calls use
  in-container loopback.
* ``ASTRABOX_LITELLM_BASE_URL`` set — an external gateway (your own LiteLLM,
  an enterprise proxy); the embedded proxy is not started and the value is
  handed to sandboxes as-is, so it must be reachable FROM a sandbox.
* ``ASTRABOX_LITELLM_SERVER_BASE_URL`` set — the platform uses this second
  address only for server-side model discovery. It is useful when sandboxes
  reach the external gateway through private DNS while the platform uses a
  loopback or service-network address.

A different gateway registers its own
:class:`~astrabox.seams.model.ModelEndpointProvider` at the
``astrabox.providers.model`` entry-point group without touching this module.
There is deliberately no bundled ``direct`` passthrough: it would be a
second, unmetered path to the same place, and every capability it could carry
(Anthropic-compatible vendor endpoints included) is one ``model_list`` entry
here.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

from astrabox.common.logger.logger_factory import get_logger
from astrabox.seams.model import (
    ModelEndpoint,
    ModelEndpointConfigurationError,
    ModelEndpointProvider,
    ModelRequestContext,
    register_model_endpoint,
)

logger = get_logger(__name__)

#: Env vars the ``litellm`` provider reads (provider-owned config, like the
#: docker provider's ``ASTRABOX_AGENT_IMAGE``).
LITELLM_BASE_URL_ENV = "ASTRABOX_LITELLM_BASE_URL"
LITELLM_SERVER_BASE_URL_ENV = "ASTRABOX_LITELLM_SERVER_BASE_URL"
LITELLM_API_KEY_ENV = "ASTRABOX_LITELLM_API_KEY"
LITELLM_MASTER_KEY_ENV = "LITELLM_MASTER_KEY"

#: The embedded proxy's port. Fixed, not a knob: it binds inside the server
#: container (and on the dev host's loopback), where nothing competes for it,
#: and the sandbox-facing URL is derived — nobody types it.
# OpenSandbox Credential Vault derives a binding's destination port from its
# scheme (HTTP=80, HTTPS=443). The bundled gateway is sandbox-facing and its
# master key is protected by that vault by default, so a non-standard port would
# produce a binding that can never match. Port 80 is private to the container /
# sandbox network; the public AstraBox console remains on 8000.
EMBEDDED_LITELLM_PORT = 80

# RFC 2606 reserves .test. The one-container image and source-dev stack serve
# this exact private name from CoreDNS; no public resolver participates in a
# credential-bound destination.
EMBEDDED_LITELLM_SANDBOX_HOST = "gateway.astrabox.test"


def _platform_host(settings: Any = None) -> str:
    """The host a box reaches this platform by (from ``mcp_proxy_base_url``)."""
    base = str(getattr(settings, "mcp_proxy_base_url", "") or "").strip()
    if not base:
        from astrabox.common.utils.settings import load_astrabox_settings

        base = str(load_astrabox_settings().mcp_proxy_base_url or "").strip()
    if not base:
        return ""
    return urlsplit(base if "//" in base else f"//{base}").hostname or ""


class LiteLLMModelEndpointProvider(ModelEndpointProvider):
    """Point the sandbox at the LiteLLM proxy (embedded by default).

    Overrides ``base_url`` (+ ``api_key`` when a proxy key is configured) with
    the gateway's; the requested ``model_name`` passes through untouched — it
    is LiteLLM's routing key (``model_list`` in the proxy config decides which
    upstream serves it). Each Agent or Assistant therefore selects its own
    protocol-specific route without a deployment-wide model override.

    Fails loud when no gateway address can be determined — a gateway provider
    must never silently degrade to vendor passthrough.
    """

    name = "litellm"
    is_default = True

    def _sandbox_base_url(self, settings: Any = None) -> str:
        configured = str(os.environ.get(LITELLM_BASE_URL_ENV) or "").strip()
        if configured:
            return configured
        if settings is None:
            from astrabox.common.utils.settings import load_astrabox_settings

            settings = load_astrabox_settings()
        if bool(getattr(settings, "sandbox_credential_vault_enabled", True)):
            return f"http://{EMBEDDED_LITELLM_SANDBOX_HOST}"
        host = _platform_host(settings)
        if host:
            return f"http://{host}:{EMBEDDED_LITELLM_PORT}"
        raise RuntimeError(
            "cannot address the LiteLLM gateway: ASTRABOX_LITELLM_BASE_URL is "
            "unset and no platform callback host is configured to derive the "
            "embedded proxy's address from (mcp_proxy_base_url / "
            "ASTRABOX_MCP_PROXY_BASE_URL). Set one of them."
        )

    @staticmethod
    def server_side_base_url() -> str:
        """Return the LiteLLM address used by the AstraBox server."""
        server_url = str(
            os.environ.get(LITELLM_SERVER_BASE_URL_ENV) or ""
        ).strip()
        if server_url:
            return server_url
        configured = str(os.environ.get(LITELLM_BASE_URL_ENV) or "").strip()
        if configured:
            configured_host = (urlsplit(configured).hostname or "").lower()
            # This RFC 2606 name exists only inside the OpenSandbox egress DNS
            # path. The platform process shares the embedded gateway's network
            # namespace, so asking host DNS to resolve it makes model discovery
            # silently return an empty catalogue.
            if configured_host != EMBEDDED_LITELLM_SANDBOX_HOST:
                return configured
        # The embedded proxy shares this process's network namespace (same
        # container, or the dev host) — loopback, not the derived sandbox host.
        return f"http://127.0.0.1:{EMBEDDED_LITELLM_PORT}"

    @staticmethod
    def _sandbox_inference_credential() -> str | None:
        """The credential a sandbox's model traffic carries.

        A LiteLLM virtual key of type ``llm_api``, not the master key. The
        gateway maps the master key to PROXY_ADMIN; the model binding admits
        ``/v1/*``; and the gateway's management surface lives under that same
        prefix. A workload never has to read a credential to spend it — the
        egress sidecar attaches whatever the binding names to whatever request
        the workload makes — so the credential the binding names must not be an
        administrator's.

        Derived here rather than read from configuration or storage. LiteLLM
        returns a key's plaintext only when it is created, and the deployment
        creates this one at startup (``ensure_sandbox_inference_key``) with the
        same derivation, so both sides arrive at one value with no secret to
        distribute.

        ``ASTRABOX_LITELLM_API_KEY`` remains the escape hatch for a gateway that
        issues its own keys and does not run this adapter. It takes effect only
        when set explicitly.
        """

        configured = str(os.environ.get(LITELLM_API_KEY_ENV) or "").strip()
        if configured:
            return configured
        from astrabox.identity.session_signing import read_session_signing_secret
        from astrabox.providers.litellm_shared_auth import sandbox_inference_key

        secret = str(read_session_signing_secret() or "").strip()
        if not secret:
            return None
        return sandbox_inference_key(secret)

    def resolve(self, *, requested: ModelEndpoint, settings: Any = None) -> ModelEndpoint:
        api_key = self._sandbox_inference_credential()
        return ModelEndpoint(
            base_url=self._sandbox_base_url(settings),
            api_key=api_key,
            model_name=requested.model_name,
            credential_kind="bearer",
        )

    def list_models(self, *, provider_access: Any = None, settings: Any = None) -> list[str]:
        """Query the LiteLLM proxy's OpenAI-standard ``GET /v1/models`` for the
        routing keys its ``model_list`` serves — the console's model dropdown.

        base_url + key resolve per-environment (``provider_access``) first,
        then the explicit server-side address, then an external shared address,
        then the embedded proxy's loopback.
        Best-effort: proxy unreachable / auth error → ``[]`` (the console
        free-texts). A vault-only key (``api_key_secret_name`` with no
        plaintext) is not resolved here — enumeration then returns ``[]`` and
        the operator types the model id.
        """
        import httpx

        _ = settings
        access = provider_access if isinstance(provider_access, dict) else {}
        base_url = (
            str(access.get("base_url") or "").strip() or self.server_side_base_url()
        )
        api_key = (
            str(access.get("api_key") or "").strip()
            or str(os.environ.get(LITELLM_API_KEY_ENV) or "").strip()
            # Model discovery runs in the AstraBox service, never in a browser
            # or sandbox. The provider service credential authorizes this
            # read-only control-plane request.
            or str(os.environ.get(LITELLM_MASTER_KEY_ENV) or "").strip()
        )
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            response = httpx.get(
                base_url.rstrip("/") + "/v1/models", headers=headers, timeout=5.0
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return []
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        models = {
            str(item.get("id") or "").strip()
            for item in items
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
        return sorted(models)

    def request_headers(
        self,
        *,
        endpoint: ModelEndpoint,
        context: ModelRequestContext,
    ) -> dict[str, str]:
        """Return the correlation headers understood by this gateway."""

        if not str(endpoint.base_url or "").strip() or not str(
            context.conversation_id or ""
        ).strip():
            return {}
        headers = {
            "langfuse_session_id": str(context.conversation_id).strip(),
        }
        user_id = str(context.user_id or "").strip()
        if user_id:
            headers["langfuse_trace_user_id"] = user_id
        return headers

    def session_credential(self, *, context: ModelRequestContext) -> str | None:
        """This Session's own virtual key, derived from the signing secret.

        Derivation rather than storage is what lets any replica — and a
        re-claim after recovery — name the key the gateway already holds
        without one of them persisting it.
        """

        session_id = str(getattr(context, "conversation_id", "") or "").strip()
        if not session_id:
            return None
        from astrabox.identity.session_signing import session_signing_secret
        from astrabox.providers.litellm_shared_auth import session_inference_key

        return session_inference_key(session_signing_secret(), session_id)

    async def ensure_session_credential(
        self, *, context: ModelRequestContext
    ) -> str | None:
        """Create this Session's virtual key at the proxy, carrying its identity."""

        session_id = str(getattr(context, "conversation_id", "") or "").strip()
        if not session_id:
            return None
        from astrabox.providers.litellm_session_keys import (
            ensure_session_inference_key,
        )

        return await ensure_session_inference_key(
            session_id,
            user_id=str(getattr(context, "user_id", "") or "") or None,
        )

    async def release_session_credential(
        self, *, context: ModelRequestContext
    ) -> bool:
        """Delete a finished Session's virtual key from the proxy."""

        session_id = str(getattr(context, "conversation_id", "") or "").strip()
        if not session_id:
            return False
        from astrabox.providers.litellm_session_keys import (
            delete_session_inference_key,
        )

        return await delete_session_inference_key(session_id)

    def validate_configuration(self, *, settings: Any = None) -> None:
        """Validate transport policy for the bundled or external gateway."""

        external = str(os.environ.get(LITELLM_BASE_URL_ENV) or "").strip()
        required = bool(getattr(settings, "model_gateway_require_https", False))
        if not external:
            if required:
                raise ModelEndpointConfigurationError(
                    "the bundled model gateway uses private HTTP; configure "
                    f"{LITELLM_BASE_URL_ENV} with an external https:// endpoint "
                    "when model_gateway_require_https is enabled"
                )
            return
        super().validate_configuration(settings=settings)
        if not required and urlsplit(external).scheme.lower() == "http":
            logger.error(
                "SECURITY: the external model gateway %s uses plaintext HTTP; "
                "configure HTTPS and enable "
                "ASTRABOX_MODEL_GATEWAY_REQUIRE_HTTPS for a team deployment",
                external,
            )


register_model_endpoint(LiteLLMModelEndpointProvider())
