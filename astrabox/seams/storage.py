"""Storage seam — the ``StorageProvider`` contract and registry.

One provider owns the deployment's workspace medium. Dispatch is keyed by the
configured provider name (``ASTRABOX_STORAGE_PROVIDER``), and deliberately not
by which sandbox backend runs the box: where a workspace's files live is a
deployment decision about durability, so keying it to the backend would mean
changing sandbox runtime moved everyone's files, and a provider could only ever
be reached by a deployment running a same-named backend.

Reading a session's transcript back is not part of this seam: the conversation
transcript's authority is the configured database's durable mirror
(``persistence/repository/transcript_entry_repository.py``), which the backend
reads without the sandbox.

To add a provider: implement :class:`StorageProvider`, call
:func:`register_storage` at import, and advertise it under the
``astrabox.providers.storage`` entry-point group. An unknown name fails with
the registered names. When no name is configured, a sole registered provider
is selected; multiple providers require an explicit choice.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple

#: Entry-point group an external package registers a storage backend under.
ENTRY_POINT_GROUP = "astrabox.providers.storage"

#: Sandbox metadata key carrying the storage assignment a box's persistent
#: workspace mounts were provisioned for. The platform writes it into the create
#: metadata and finds a box by it; a route may label its own per-box resources
#: with the same key.
WORKSPACE_ASSIGNMENT_METADATA_KEY = "astrabox.storage-assignment"


@dataclass(frozen=True)
class StorageMountPlan:
    """Backing filesystem and workspace paths supplied to a workspace route."""

    volume_name: str
    mounts: tuple[tuple[str, str], ...]


class WorkspaceRef(NamedTuple):
    """Which workspace, as the platform names it.

    The logical key names an Agent or Assistant and, when present, a
    conversation beneath it. Physical backing paths are planned separately
    from the durable ``workspace_id`` in ``runtime/storage/_scope.py``;
    this reference does not determine whether a persistent volume is enabled.

    The key is built here rather than by each provider, because two providers
    left to build their own would grow two key spaces for one concept.
    """

    subject_kind: str
    subject_id: str
    conversation_session_id: str | None = None

    def key(self) -> str:
        root = f"{_SUBJECT_ROOTS[self.subject_kind]}/{self.subject_id}"
        if self.conversation_session_id:
            return f"{root}/conversations/{self.conversation_session_id}"
        return root


#: One root per subject. A subject absent here is a product this seam does not
#: address, and asking for its key fails rather than inventing a root.
_SUBJECT_ROOTS = {"agent": "agents", "assistant": "assistants"}


class StorageProvider(ABC):
    """One storage medium, addressed by the name a deployment configures.

    Supply the backing filesystem and its workspace paths. How that medium
    reaches a box is the sandbox backend's :class:`WorkspaceRoute`; the
    platform owns box assignment and delivery ordering independently of which
    medium is selected. Writes reach the mounted medium without a per-turn
    copy or flush step.
    """

    name: str = ""

    def validate_configuration(self) -> None:
        """Validate the selected provider's deployment requirements."""

    async def provision_mounts(
        self, assignment_id: str, mounts: tuple[tuple[str, str], ...]
    ) -> StorageMountPlan:
        """Provide backing resources to the sandbox backend's workspace route."""
        raise NotImplementedError(f"storage provider {self.name!r} cannot provision mounts")

    @abstractmethod
    async def prepare(
        self,
        ref: WorkspaceRef,
        *,
        box: Any,
        box_path: str,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        """The workspace's files are present at `box_path`.

        Called for each planned mount during workspace preparation, before
        engine activation. The backend's workspace route separately verifies
        what it mounted, and binds an unassigned prepared entry before user
        input is delivered.

        `box` is the sandbox the orchestrator already holds. A provider whose
        medium the backend mounted has nothing to do with it; one that
        synchronizes reaches the box's files through it. The orchestrator learns
        nothing about the medium either way, which is the point — it hands over
        the box and the path and is told whether the workspace is there. A
        subject with more than one mount gets one call per path; `ref` names the
        subject, `box_path` names which of its directories this call is about.

        `owner` and `group` name the workload account that must be able to use
        the files. A provider that mounts has nothing to do with them — the
        medium arrived with its own ownership — while one that materializes
        files must hand them to that account, or the box boots with a workspace
        its own agent cannot write.

        A refusal means the box must not be used. An agent writing into the
        wrong directory looks exactly like an agent working.
        """

class WorkspaceRoute(ABC):
    """How one sandbox backend brings a storage medium into its boxes.

    A sandbox backend offers persistent workspaces by returning one from
    ``SandboxProvider.workspace_route()``. The platform keeps the workflow —
    which assignment a box gets, the order of provisioning, create, receipt,
    verification and binding, and when per-box resources may be released —
    and asks the box's own backend for each step. A route answers only how its
    runtime mounts: the bundled OpenSandbox backend routes through host
    mergerfs helpers; a platform that mounts its own storage at create time
    returns that mount as the create plan and has no per-box resources.

    Every method addresses a *storage assignment*: the platform's identity
    for one box's mounts, stable across a create that is retried.
    """

    #: The route can point an already created box at a workspace chosen after
    #: create (a prepared box claimed by a session that already has files).
    #: A route that cannot is never asked to: such a session gets a cold
    #: create carrying its own mounts.
    supports_late_binding: bool = False

    def validate_configuration(self) -> None:
        """Refuse at startup what the route needs and cannot reach."""

    @abstractmethod
    async def provision_mounts(
        self, assignment_id: str, backing: StorageMountPlan, *, sandbox_backend: str
    ) -> StorageMountPlan:
        """The volume and box paths a create of this backend mounts.

        Called before create. A route that creates per-box resources must
        leave a receipt that :meth:`unattached_mounts` reports, so a create
        that failed or never answered can still be released.
        """

    @abstractmethod
    async def attach_sandbox(
        self, assignment_id: str, *, sandbox_id: str, sandbox_backend: str
    ) -> None:
        """Record which box mounts an assignment. Idempotent; called again on replay."""

    @abstractmethod
    async def prepare(
        self,
        ref: WorkspaceRef,
        *,
        box: Any,
        box_path: str,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        """Prove the box sees what this route mounted at ``box_path``.

        Called before engine activation, after the storage provider's own
        check. A refusal means the box must not be used.
        """

    async def bind(
        self, assignment_id: str, backing: StorageMountPlan, binding: str
    ) -> None:
        """Point a created box's mounts at ``binding``'s workspace, once, before delivery.

        Required when :attr:`supports_late_binding` is set.
        """
        raise NotImplementedError("this workspace route cannot bind after create")

    async def attached_mounts(self) -> list[tuple[str, str, str]]:
        """This installation's (storage assignment, sandbox backend, sandbox id) receipts."""
        return []

    async def unattached_mounts(self) -> list[tuple[str, str, datetime]]:
        """This installation's assignments no box was recorded for, with their age."""
        return []

    async def release_mounts(self, assignment_id: str) -> bool:
        """Release an assignment's per-box resources; never the backing data.

        Called only after the box is confirmed gone, or for an unattached
        assignment past its grace period. Returns False when the runtime is
        still letting go of the box (a backend can report a box gone while
        its container is still shutting down and mounts the view); the
        platform asks again on its next reconciliation. Raises when a consumer
        that is not going away still mounts the view.
        """
        return True


_PROVIDERS: dict[str, StorageProvider] = {}
_CONFIGURED: str = ""


def register_storage(name: str, provider: StorageProvider) -> None:
    """Register a storage provider under a name. Last registration wins."""
    _PROVIDERS[name.strip().lower()] = provider


def set_configured_storage_provider(name: str | None) -> None:
    """Publish the deployment's storage provider name.

    Called by the composition root at bootstrap with the settings value, which
    is the same shape :func:`astrabox.seams.sandbox.set_default_sandbox_backend`
    uses: the seam holds no opinion about configuration, and the composition
    root holds no opinion about providers.
    """
    global _CONFIGURED
    _CONFIGURED = str(name or "").strip().lower()


def storage_provider(name: str | None = None) -> StorageProvider:
    """Resolve a storage provider by name, defaulting to the configured one.

    Raises listing the registered names rather than falling back to any of them.
    A workspace written to the wrong medium is not a degraded success: the files
    are somewhere nobody will look for them, and nothing later can tell that
    from an empty workspace.
    """
    resolved = (name or _CONFIGURED or "").strip().lower()
    if not resolved and len(_PROVIDERS) == 1:
        # The unambiguous case, on the same terms as
        # :func:`astrabox.seams.sandbox.default_sandbox_backend`: with one
        # provider registered there is no second answer to choose wrongly
        # between. Two registered and none configured still fails.
        resolved = next(iter(_PROVIDERS))
    if not resolved:
        raise RuntimeError(
            "no storage provider configured; set ASTRABOX_STORAGE_PROVIDER "
            f"(registered: {sorted(_PROVIDERS)})"
        )
    provider = _PROVIDERS.get(resolved)
    if provider is None:
        raise RuntimeError(
            f"no StorageProvider registered under {resolved!r} "
            f"(registered: {sorted(_PROVIDERS)})"
        )
    return provider


__all__ = [
    "ENTRY_POINT_GROUP",
    "WORKSPACE_ASSIGNMENT_METADATA_KEY",
    "StorageProvider",
    "StorageMountPlan",
    "WorkspaceRef",
    "WorkspaceRoute",
    "register_storage",
    "set_configured_storage_provider",
    "storage_provider",
]
