"""AstraBox seams as ``typing.Protocol`` contracts.

Each Protocol in this package is a dependency-free contract carrying stable method
names, parameters and return types. A provider — the built-in ``open_sandbox``
provider or a third-party plugin — satisfies these structurally and
registers itself via PEP 621 entry-points; resolution is name-keyed and fails loud.

Concrete implementations in this repository:

* ``SandboxProvider`` (+ ``SandboxLifecycleProbeResult`` and the
  ``SANDBOX_LIFECYCLE_PROBE_*`` constants, the create spec
  ``SandboxCreateSpec`` / ``SandboxRuntimeDefaults``, plus the in-box transport
  types ``SandboxDataPlane`` + ``SandboxHttpResponse``) — one provider owns a
  sandbox platform end to end: creation, the agent executor, backend
  decisions, by-id lifecycle, and the in-box transport.
* ``ModelEndpointProvider`` + ``ModelEndpoint`` — which LLM endpoint the agent
  runtime talks to.
* ``ExtensionProvider`` — extension catalog normalization, runtime bindings,
  and server-side MCP authorization.
* ``SecretStore`` — where credential secret material lives (``local`` and
  ``aws_kms`` encrypted-at-rest built-ins; external stores plug in).
* ``StorageProvider`` — one provider owns a session's storage medium
* ``WebIdentityResolver`` — one identity seam for browser, API, and public MCP
  requests.
* ``RuntimePreparationProvider`` — one deployment integration for Agent
  resources, Session startup materials, per-input preparation and user
  prerequisites.

Everything here imports only the standard library, ``typing`` and
``astrabox.common.utils.errors`` (the transport-neutral error type) — never the
orchestrator core, so a provider distribution can depend on the seams without
dragging the host in.
"""

from __future__ import annotations

#: The seams contract version. Bumped ONLY on a breaking change to any Protocol
#: in this package (a method removed/renamed, a required parameter added — NOT
#: additive optional surface). A provider distribution can pin the contract it
#: was built against by setting ``seams_api_version = <int>`` on its provider
#: class; the entry-point loaders compare it against this value at registration
#: and fail loud on mismatch, so an incompatible plugin is rejected at bootstrap
#: instead of failing mid-turn with an AttributeError.
SEAMS_API_VERSION = 10

from astrabox.seams.extensions import ExtensionProvider
from astrabox.seams.skills import SkillContentProvider, SkillSource, ResolvedSkillSource
from astrabox.seams.identity import WebIdentityResolver
from astrabox.seams.deployment_execution import DeploymentExecutionContext, DeploymentExecutionValidator
from astrabox.seams.model import ModelEndpoint, ModelEndpointProvider
from astrabox.seams.secrets import SecretStore
from astrabox.seams.sandbox import (
    SANDBOX_LIFECYCLE_PROBE_FAILED,
    SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
    SANDBOX_LIFECYCLE_PROBE_OK,
    SandboxCreateSpec,
    SandboxDataPlane,
    SandboxHttpResponse,
    SandboxLifecycleProbeResult,
    SandboxProvider,
    SandboxRuntimeDefaults,
)
from astrabox.seams.runtime_preparation import (
    PreparedPackage, PreparationSubject, PreparationCommitGuard,
    ResourcePreparationOperation, ResourcePreparationContext,
    RuntimePreparationProvider, RuntimePreparationPlan, RuntimeMaterials, RuntimeFile, SessionMCPServer,
    SessionPreparationContext, TurnPreparationContext, PreparationUserActionRequired,
    SessionUserActionContext, SessionUserActionCommand, SessionUserActionState,
    TURN_PREPARATION_FAILED, register_runtime_preparation_provider,
)
from astrabox.seams.storage import (
    StorageProvider,
)

__all__ = [
    # contract version (providers may pin it; loaders enforce on mismatch)
    "SEAMS_API_VERSION",
    "PreparedPackage",
    "PreparationSubject",
    "PreparationCommitGuard",
    "ResourcePreparationOperation",
    "ResourcePreparationContext",
    "RuntimePreparationProvider",
    "RuntimePreparationPlan",
    "SessionMCPServer",
    "RuntimeMaterials",
    "RuntimeFile",
    "SessionPreparationContext",
    "TurnPreparationContext",
    "PreparationUserActionRequired",
    "TURN_PREPARATION_FAILED",
    "register_runtime_preparation_provider",
    "SessionUserActionContext",
    "SessionUserActionCommand",
    "SessionUserActionState",
    # sandbox (creation + agent execution + backend decisions + by-id
    # lifecycle + in-box transport)
    "SandboxProvider",
    "SandboxCreateSpec",
    "SandboxRuntimeDefaults",
    "SandboxLifecycleProbeResult",
    "SANDBOX_LIFECYCLE_PROBE_OK",
    "SANDBOX_LIFECYCLE_PROBE_NOT_FOUND",
    "SANDBOX_LIFECYCLE_PROBE_FAILED",
    "SandboxDataPlane",
    "SandboxHttpResponse",
    # model endpoint
    "ModelEndpointProvider",
    "ModelEndpoint",
    # extension catalog and runtime bindings
    "ExtensionProvider",
    "SkillContentProvider",
    "SkillSource",
    "ResolvedSkillSource",
    # secret store (vault secret material at rest)
    "SecretStore",
    # storage
    "StorageProvider",
    # identity for every HTTP surface
    "WebIdentityResolver",
    "DeploymentExecutionContext",
    "DeploymentExecutionValidator",
]
