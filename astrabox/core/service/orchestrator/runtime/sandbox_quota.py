"""The deployment-wide quota on what each sandbox backend runs.

Every box the platform creates — a conversation's own box, an Agent's shared
box, a member of an Agent's pool — is reserved here before its backend is
asked for it, through :func:`create_sandbox_with_storage`, the one create path.
A backend's usage is what its last inventory reported this installation running
plus the reservations for boxes not in that inventory yet. A box whose
destruction is not confirmed is still in the inventory, so it is still charged.

A box is charged its own limits plus whatever its backend runs beside it under
a ceiling (:meth:`SandboxProvider.per_box_overhead`). The charge is written into
the box's metadata, so a later inventory charges each box what it was created
with. A paused box whose backend released it
(:meth:`SandboxProvider.paused_box_releases_resources`) holds nothing, and its
resume is reserved here like a create.

A reservation is admitted under the backend's ledger lock
(:class:`~astrabox.persistence.repository.sandbox_quota_repository.SandboxQuotaRepository`),
so two replicas that each see room for one more box cannot both take it. A
refused create reconciles the ledger against the backend once before it is
refused, so a box destroyed since the last sweep frees its room at once.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.config.settings import cpu_quantity, get_settings, memory_quantity
from astrabox.persistence.repository.sandbox_quota_repository import (
    InventoryNotTaken,
    SandboxQuotaRepository,
)

logger = get_logger(__name__)

#: Metadata a created box carries: what the quota charged it.
QUOTA_CPU_METADATA_KEY = "astrabox.quota-cpu-millicores"
QUOTA_MEMORY_METADATA_KEY = "astrabox.quota-memory-bytes"

#: Memory the derived default leaves to the platform, its stores and the host.
_HOST_RESERVE_BYTES = 4 * 2**30
#: A reservation whose create never answered is released when its box has not
#: appeared in an inventory taken this long after it was made.
_CREATE_GRACE = timedelta(minutes=15)
_GIB = 2**30


@dataclass(frozen=True)
class SandboxCharge:
    """What one box holds against the quota."""

    cpu: float
    memory: int

    @property
    def cpu_millicores(self) -> int:
        return round(self.cpu * 1000)

    def metadata(self) -> dict[str, str]:
        return {
            QUOTA_CPU_METADATA_KEY: str(self.cpu_millicores),
            QUOTA_MEMORY_METADATA_KEY: str(self.memory),
        }


@dataclass(frozen=True)
class SandboxQuota:
    """One backend's ceilings; ``None`` is no ceiling."""

    boxes: int | None = None
    memory: int | None = None
    cpu: float | None = None
    concurrent_creates: int | None = None
    #: Where each ceiling came from: configured, derived (and from what), or unlimited.
    sources: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxUsage:
    boxes: int
    memory: int
    cpu: float
    creating: int

    @classmethod
    def from_ledger(cls, ledger: Mapping[str, Any]) -> SandboxUsage:
        return cls(
            boxes=int(ledger["boxes"]),
            memory=int(ledger["memory_bytes"]),
            cpu=int(ledger["cpu_millicores"]) / 1000,
            creating=int(ledger["creating"]),
        )


class SandboxCapacityExhausted(APIError):
    """A box would take a backend past one of its quota ceilings."""

    def __init__(
        self,
        backend: str,
        *,
        ceiling: str,
        used: str,
        limit: str,
        requested: str,
    ) -> None:
        super().__init__(
            code="SANDBOX_CAPACITY_EXHAUSTED",
            message=(
                f"sandbox backend {backend!r} has no room for another box: {ceiling} "
                f"{used} of {limit} in use, and this box needs {requested}"
            ),
            status_code=429,
        )
        self.backend = backend
        self.ceiling = ceiling
        self.used = used
        self.limit = limit
        self.requested = requested


_QUOTAS: dict[str, SandboxQuota] = {}
_repository: SandboxQuotaRepository | None = None


def _repo() -> SandboxQuotaRepository:
    global _repository
    if _repository is None:
        _repository = SandboxQuotaRepository()
    return _repository


def _count_setting(value: str) -> tuple[int | None, str]:
    if value == "unlimited":
        return None, "unlimited"
    if value:
        return int(value), "configured"
    return None, "unlimited (unset)"


async def configure_sandbox_quotas(providers: Mapping[str, Any]) -> dict[str, SandboxQuota]:
    """Resolve each enabled backend's ceilings once, at startup, and log them.

    A ceiling on a backend that cannot list its sandboxes could never be
    checked against what it runs, so startup refuses it.
    """
    settings = get_settings()
    boxes, boxes_source = _count_setting(settings.sandbox_quota_boxes)
    creates, creates_source = _count_setting(settings.sandbox_quota_concurrent_creates)
    cpu: float | None = None
    cpu_source = "unlimited (unset)"
    if settings.sandbox_quota_cpu == "unlimited":
        cpu_source = "unlimited"
    elif settings.sandbox_quota_cpu:
        cpu, cpu_source = cpu_quantity(settings.sandbox_quota_cpu), "configured"
    resolved: dict[str, SandboxQuota] = {}
    for name, provider in providers.items():
        memory: int | None = None
        memory_source = "unlimited (unset)"
        if settings.sandbox_quota_memory == "unlimited":
            memory_source = "unlimited"
        elif settings.sandbox_quota_memory:
            memory = int(memory_quantity(settings.sandbox_quota_memory))
            memory_source = "configured"
        else:
            host = await provider.host_memory_bytes()
            if host is not None:
                memory = max(host - _HOST_RESERVE_BYTES, 0)
                memory_source = (
                    f"derived: host memory {host / _GIB:.1f}Gi less "
                    f"{_HOST_RESERVE_BYTES // _GIB}Gi for the platform and the host"
                )
        quota = SandboxQuota(
            boxes=boxes,
            memory=memory,
            cpu=cpu,
            concurrent_creates=creates,
            sources={
                "boxes": boxes_source,
                "memory": memory_source,
                "cpu": cpu_source,
                "concurrent_creates": creates_source,
            },
        )
        limited = any(v is not None for v in (boxes, memory, cpu, creates))
        if limited and not bool(getattr(provider, "supports_inventory_listing", False)):
            raise RuntimeError(
                f"sandbox backend {name!r} cannot list its sandboxes, so a sandbox quota "
                "cannot be checked against what it runs; set every ASTRABOX_SANDBOX_QUOTA_* "
                "to 'unlimited' for it, or use a backend that declares "
                "supports_inventory_listing"
            )
        resolved[name] = quota
        logger.info(
            "sandbox quota for backend %s: boxes=%s (%s) memory=%s (%s) cpu=%s (%s) "
            "concurrent_creates=%s (%s)",
            name,
            boxes if boxes is not None else "unlimited", boxes_source,
            _format_memory(memory) if memory is not None else "unlimited", memory_source,
            cpu if cpu is not None else "unlimited", cpu_source,
            creates if creates is not None else "unlimited", creates_source,
        )
    _QUOTAS.clear()
    _QUOTAS.update(resolved)
    return resolved


def quota_for(backend: str) -> SandboxQuota:
    return _QUOTAS.get(backend) or SandboxQuota()


def box_charge(provider: Any, resource_limits: Mapping[str, str]) -> SandboxCharge:
    """The box's own limits plus what its backend runs beside it."""
    overhead_cpu, overhead_memory = provider.per_box_overhead()
    return SandboxCharge(
        cpu=cpu_quantity(str(resource_limits["cpu"])) + overhead_cpu,
        memory=int(memory_quantity(str(resource_limits["memory"]))) + overhead_memory,
    )


def _format_memory(value: float) -> str:
    return f"{value / _GIB:.2f}Gi"


def _refusal(backend: str, quota: SandboxQuota, usage: SandboxUsage, charge: SandboxCharge) -> SandboxCapacityExhausted | None:
    if quota.boxes is not None and usage.boxes + 1 > quota.boxes:
        return SandboxCapacityExhausted(
            backend, ceiling="boxes", used=str(usage.boxes), limit=str(quota.boxes), requested="1"
        )
    if quota.memory is not None and usage.memory + charge.memory > quota.memory:
        return SandboxCapacityExhausted(
            backend,
            ceiling="memory",
            used=_format_memory(usage.memory),
            limit=_format_memory(quota.memory),
            requested=_format_memory(charge.memory),
        )
    if quota.cpu is not None and usage.cpu + charge.cpu > quota.cpu + 1e-9:
        return SandboxCapacityExhausted(
            backend,
            ceiling="cpu",
            used=f"{usage.cpu:g}",
            limit=f"{quota.cpu:g}",
            requested=f"{charge.cpu:g}",
        )
    if quota.concurrent_creates is not None and usage.creating + 1 > quota.concurrent_creates:
        return SandboxCapacityExhausted(
            backend,
            ceiling="concurrent creates",
            used=str(usage.creating),
            limit=str(quota.concurrent_creates),
            requested="1",
        )
    return None


def _has_ceiling(quota: SandboxQuota) -> bool:
    return any(
        ceiling is not None
        for ceiling in (quota.boxes, quota.memory, quota.cpu, quota.concurrent_creates)
    )


async def reserve(
    provider: Any,
    charge: SandboxCharge,
    *,
    reason: str,
    session_id: str,
    assignment_id: str,
) -> str:
    """Take room for one box on ``provider``'s ledger, or raise the ceiling it would pass."""
    backend = str(provider.name)
    quota = quota_for(backend)
    key = uuid.uuid4().hex
    reservation = {
        "reservation_key": key,
        "state": "creating",
        "cpu_millicores": charge.cpu_millicores,
        "memory_bytes": charge.memory,
        "reason": reason,
        "session_id": session_id or None,
        "assignment_id": assignment_id,
    }

    def refuse(ledger: dict[str, int]) -> SandboxCapacityExhausted | None:
        return _refusal(backend, quota, SandboxUsage.from_ledger(ledger), charge)

    async def admit() -> SandboxCapacityExhausted | None:
        return await _repo().admit(
            backend, reservation, refuse if _has_ceiling(quota) else None
        )

    try:
        refusal = await admit()
    except InventoryNotTaken:
        # A ledger no inventory has filled yet would count every running box as
        # absent.
        await reconcile_sandbox_quota(provider)
        refusal = await admit()
    if refusal is not None:
        # The ledger may still charge a box destroyed since the last sweep.
        await reconcile_sandbox_quota(provider)
        refusal = await admit()
        if refusal is not None:
            logger.warning("sandbox quota refused a %s box: %s", reason, refusal.message)
            raise refusal
    return key


async def reserve_resume(provider: Any, sandbox_id: str) -> str | None:
    """Take room for a paused box its backend released, or raise the ceiling it would pass.

    None when the backend keeps a paused box's resources, or the ledger still
    charges the box: its resume adds nothing.
    """
    if not provider.paused_box_releases_resources() or not bool(
        getattr(provider, "supports_inventory_listing", False)
    ):
        return None
    box = await _repo().inventory_box(str(provider.name), sandbox_id)
    if box is not None and not box["released"]:
        return None
    if box is not None:
        charge = SandboxCharge(cpu=box["cpu_millicores"] / 1000, memory=box["memory_bytes"])
    else:
        charge = box_charge(provider, _shared_limits())
    return await reserve(provider, charge, reason="resume", session_id="", assignment_id=sandbox_id)


async def mark_created(backend: str, key: str, sandbox_id: str) -> None:
    await _repo().mark_created(backend, key, sandbox_id)


async def release(backend: str, key: str) -> None:
    await _repo().release(backend, key)


async def settle_failed_create(provider: Any, key: str, *, assignment_id: str) -> None:
    """After a create raised: keep the room if a box exists, release it if none does.

    A backend that cannot look its box up by assignment cannot say, so the
    reservation stays until an inventory taken after the grace shows no box.
    """
    backend = str(provider.name)
    if not bool(getattr(provider, "supports_correlated_create", False)):
        return
    existing = await provider.find_sandbox_by_assignment(assignment_id)
    if existing is None:
        await release(backend, key)
    else:
        await mark_created(backend, key, str(existing.sandbox_id))


async def _inventory(provider: Any) -> dict[str, dict[str, Any]]:
    from astrabox.seams.sandbox import SANDBOX_INSTALLATION_METADATA_KEY, sandbox_installation_id

    unknown = box_charge(provider, _shared_limits())
    releases_when_paused = bool(provider.paused_box_releases_resources())
    installation = await sandbox_installation_id()
    boxes: dict[str, dict[str, Any]] = {}
    page = 1
    while True:
        listing = await provider.list_sandboxes(
            page=page, page_size=100, metadata={SANDBOX_INSTALLATION_METADATA_KEY: installation}
        )
        for item in listing.items:
            metadata = dict(item.metadata or {})
            try:
                cpu = int(metadata[QUOTA_CPU_METADATA_KEY])
                memory = int(metadata[QUOTA_MEMORY_METADATA_KEY])
            except (KeyError, ValueError):
                # Created before this deployment recorded charges: charge the
                # larger kind of box, so the quota never counts it short.
                cpu, memory = unknown.cpu_millicores, unknown.memory
            boxes[str(item.sandbox_id)] = {
                "cpu_millicores": cpu,
                "memory_bytes": memory,
                # Saved and released: it holds nothing until reserve_resume
                # takes its charge back.
                "released": releases_when_paused and str(item.state or "").upper() == "PAUSED",
                "state": str(item.state or ""),
                "session_id": item.session_id,
            }
        if not listing.has_next_page:
            return boxes
        page += 1


def _shared_limits() -> dict[str, str]:
    from astrabox.core.service.orchestrator.engine.provisioning import sandbox_create_resources

    return sandbox_create_resources(shared=True)[0]


async def reconcile_sandbox_quota(provider: Any) -> dict[str, int]:
    """Fold the backend's inventory into its ledger and clear settled reservations.

    A reservation leaves the ledger when the inventory charges its box from
    then on, when its box was created (or resumed) before the inventory was
    taken and the inventory does not charge it, or when its create has gone
    unanswered past the grace with no box to show for it. An inventory taken
    before the one the ledger already holds changes nothing.
    """
    backend = str(provider.name)
    taken_at = await _repo().clock()
    inventory = await _inventory(provider)
    released = await _repo().fold_inventory(
        backend,
        taken_at=taken_at,
        boxes=[{"sandbox_id": sandbox_id, **box} for sandbox_id, box in inventory.items()],
        create_grace=_CREATE_GRACE,
    )
    return {f"{backend}.quota_boxes": len(inventory), f"{backend}.quota_released": released or 0}


async def reconcile_at_startup(providers: Mapping[str, Any]) -> None:
    """Fold each listing backend's inventory into its ledger before serving.

    Boxes may have gone while the server was down; the ledger the console and
    the next create read should start from what the backend runs now. A backend
    that cannot be listed yet is logged: the periodic sweep, and any create the
    stale ledger would refuse, reconcile it later.
    """
    for name, provider in providers.items():
        if not bool(getattr(provider, "supports_inventory_listing", False)):
            continue
        try:
            await reconcile_sandbox_quota(provider)
        except Exception:
            logger.exception("sandbox quota: startup reconcile of backend %s failed", name)


async def quota_report(providers: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each enabled backend's ceilings, usage and boxes, for the console.

    A backend that cannot list its sandboxes keeps no ledger and is left out.
    """
    report = []
    for name, provider in providers.items():
        if not bool(getattr(provider, "supports_inventory_listing", False)):
            continue
        ledger = await _repo().ledger(name)
        quota = quota_for(name)
        usage = SandboxUsage.from_ledger(ledger)
        report.append({
            "backend": name,
            "quota": {
                "boxes": quota.boxes,
                "memory_bytes": quota.memory,
                "cpu": quota.cpu,
                "concurrent_creates": quota.concurrent_creates,
                "sources": dict(quota.sources),
            },
            "usage": {
                "boxes": usage.boxes,
                "memory_bytes": usage.memory,
                "cpu": usage.cpu,
                "creating": usage.creating,
            },
            "inventory_at": ledger["inventory_at"],
            "boxes": [
                {
                    "sandbox_id": box["sandbox_id"],
                    # A released box holds none of its charge.
                    "cpu": 0.0 if box["released"] else box["cpu_millicores"] / 1000,
                    "memory": 0 if box["released"] else box["memory_bytes"],
                    "state": box["state"],
                    "session_id": box["session_id"],
                }
                for box in await _repo().list_inventory(name)
            ],
            "reservations": [
                {
                    "state": reservation["state"],
                    "reason": reservation["reason"],
                    "session_id": reservation["session_id"],
                    "sandbox_id": reservation["sandbox_id"],
                    "cpu": reservation["cpu_millicores"] / 1000,
                    "memory": reservation["memory_bytes"],
                    "reserved_at": reservation["reserved_at"],
                }
                for reservation in await _repo().list_pending_reservations(name)
            ],
        })
    return report
