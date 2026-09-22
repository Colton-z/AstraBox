"""EnginePlatform — typed services used by platform runtime orchestration.

The runtime manager implements this protocol for the platform-owned startup,
attach, preparation, and cleanup coordinators. Engine adapters do not receive
it; they receive immutable prepared contexts and therefore cannot allocate,
claim, mount, write credentials, or perform lifecycle cleanup. The remaining
vendor process-disposal hook receives this surface only for an already-owned
durable turn anchor.

This module is a pure typing leaf: stdlib imports only, no runtime dependency
on the manager (so adapters can import it without dragging the orchestrator).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from astrabox.seams.egress_credentials import MCPOutboundCredentialResolution
    from astrabox.seams.model import ResolvedModelAccess
    from astrabox.seams.sandbox import SandboxAllocation


@runtime_checkable
class EnginePlatform(Protocol):
    """Engine-facing surface of the session runtime manager."""

    # ── deployment config (read-only) ───────────────────────────────────────

    @property
    def deployment_settings(self) -> Any:
        """The deployment settings object (read-only).

        Engines read engine-specific knobs from it (e.g. the Hermes skill
        sources and platform callback base URL). Treat as immutable.
        """
        ...

    # ── model / credential resolution ───────────────────────────────────────

    def resolve_model_access(self, mc: dict[str, Any]) -> "ResolvedModelAccess":
        """Resolve endpoint, model and credential through the model seam."""
        ...

    def resolve_sandbox_backend_secret(
        self,
        template: Any,
        *,
        backend: str | None = None,
    ) -> str:
        """Sandbox-backend secret material for the resolved Agent view."""
        ...

    async def resolve_session_egress_credentials(
        self,
        session_id: str,
        *,
        placeholder_context: str | None = None,
    ) -> list[Any]:
        """The session's environment and HTTP Basic credentials, egress-side.

        Each item is an ``astrabox.seams.egress_credentials.EgressCredential``
        (named loosely here to keep this module a typing leaf) carrying the real
        secret plus a placeholder minted for this call. An engine hands the
        placeholders to the box and the credentials to the backend's egress
        vault; the two must come from one call, or the box holds a placeholder
        the proxy will not substitute.

        ``HTTPBasicEgressCredentialSet`` items have no workload environment value;
        only the sandbox provider receives their selected credentials. Empty
        sets revoke that managed scope on the next application.

        ``placeholder_context`` is supplied only when a Session claims a
        process that was already started under a prepared workload id. Its
        Session-scoped Vault snapshot still chooses which credentials may be
        resolved; the explicit context reproduces the placeholders already in
        that process rather than minting a second, unusable set.

        Empty list when the session attached no vault or none of its credentials
        are of this type. Raises when a secret is missing or two credentials
        claim one environment variable.
        """
        ...

    async def resolve_session_mcp_credentials(
        self,
        session_id: str,
        server_urls: list[str],
    ) -> "MCPOutboundCredentialResolution":
        """Resolve this Session's MCP Vault scope and destination headers.

        Real headers stay on the host and are written to the backend's egress
        Vault; engine-native MCP configuration receives only the destination
        URL. The scope fingerprint prevents two conversations in a shared box
        from treating coincidentally empty current results as the same policy.
        """
        ...

    # ── sandbox / runtime bookkeeping ───────────────────────────────────────

    async def record_startup_allocation(
        self,
        session_id: str,
        allocation: "SandboxAllocation",
        *,
        replaces: "SandboxAllocation | None" = None,
    ) -> None:
        """Persist a just-provisioned resource before startup can await again.

        The allocation says whether this startup owns the whole sandbox or only
        exact isolated sessions in a longer-lived sandbox. Engines provide the
        fact; the platform owns persistence, adoption, and cleanup.
        """
        ...

    async def record_attached_runtime_identity(
        self,
        session_id: str,
        *,
        sandbox_id: str,
        runtime_identity: dict[str, Any],
    ) -> None:
        """Persist identity facts re-established while attaching a live box."""

        ...

    async def connect_sandbox_only(self, sandbox_id: str) -> Any:
        """Connect to the exact persisted sandbox without starting an engine.

        Backend selection comes from the sandbox's durable owner; adapters must
        not infer a provider from an id or fall back to a deployment default.
        """
        ...

    # ── workspace bring-up ───────────────────────────────────────────────────

    async def clone_default_repo(
        self,
        sandbox: Any,
        template: Any,
        target_cwd: str,
        session_id: str,
        *,
        runtime_identity: dict[str, Any] | None = None,
    ) -> None:
        """Clone the resolved Agent's default repository into its workspace."""
        ...

    async def mount_assistant_workspace_storage(
        self,
        sandbox: Any,
        *,
        user_id: str,
        assistant_id: str,
        engine_kind: str,
    ) -> None:
        """Mount the platform-owned persistent Assistant workspace."""
        ...

    async def prepare_workspace_storage(
        self,
        sandbox: Any,
        *,
        workspace_ref: Any,
        box_path: str,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        """Materialize one workspace from the configured storage provider."""
        ...

    async def resolve_runtime_sandbox_backend(
        self,
        session_id: str,
        *,
        workspace_plan: Any,
    ) -> str:
        """Reload the authoritative sandbox backend for an engine operation."""
        ...

__all__ = ["EnginePlatform"]
