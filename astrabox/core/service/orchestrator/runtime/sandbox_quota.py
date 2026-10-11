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

Reservations are written by compare-and-set on the backend's ledger, so two
replicas that each see room for one more box cannot both take it. A refused
create reconciles the ledger against the backend once before it is refused,
so a box destroyed since the last sweep frees its room at once.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.config.settings import cpu_quantity, get_settings, memory_quantity
from astrabox.persistence.repository.sandbox_quota_repository import SandboxQuotaRepository

logger = get_logger(__name__)

#: Metadata a created box carries: what the quota charged it.
QUOTA_CPU_METADATA_KEY = "astrabox.quota-cpu-millicores"
QUOTA_MEMORY_METADATA_KEY = "astrabox.quota-memory-bytes"

#: Memory the derived default leaves to the platform, its stores and the host.
_HOST_RESERVE_BYTES = 4 * 2**30
#: A reservation whose create never answered is released when its box has not
#: appeared in an inventory taken this long after it was made.
_CREATE_GRACE = timedelta(minutes=15)
_CAS_ATTEMPTS = 20
_GIB = 2**30


@dataclass(frozen=True)
class SandboxCharge:
    """What one box holds against the quota."""

    cpu: float
    memory: int

    def metadata(self) -> dict[str, str]:
        return {
            QUOTA_CPU_METADATA_KEY: str(round(self.cpu * 1000)),
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


def _held(inventory: Mapping[str, Any], sandbox_id: Any) -> bool:
    """Whether the inventory charges this box: listed, and not paused and released."""
    box = inventory.get(str(sandbox_id)) if sandbox_id else None
    return box is not None and "released_charge" not in box


def _usage(ledger: Mapping[str, Any]) -> SandboxUsage:
    inventory = dict(ledger.get("inventory") or {})
    boxes = sum(1 for sandbox_id in inventory if _held(inventory, sandbox_id))
    memory = sum(int(box.get("memory") or 0) for box in inventory.values())
    cpu = sum(float(box.get("cpu") or 0) for box in inventory.values())
    creating = 0
    for reservation in ledger.get("reservations") or []:
        if _held(inventory, reservation.get("sandbox_id")):
            continue
        boxes += 1
        memory += int(reservation.get("memory") or 0)
        cpu += float(reservation.get("cpu") or 0)
        if reservation.get("state") == "creating":
            creating += 1
    return SandboxUsage(boxes=boxes, memory=memory, cpu=cpu, creating=creating)


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


async def _update(backend: str, change: Any) -> Any:
    """Apply ``change(ledger) -> (reservations, result)`` by compare-and-set."""
    for _ in range(_CAS_ATTEMPTS):
        ledger = await _repo().read(backend)
        reservations, result = change(ledger)
        if reservations is None:
            return result
        if await _repo().compare_and_set(
            backend, version=int(ledger.get("version") or 0), reservations=reservations
        ):
            return result
        await asyncio.sleep(random.uniform(0.01, 0.05))
    raise RuntimeError(f"sandbox quota ledger for {backend!r} stayed contended")


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

    def take(ledger: Mapping[str, Any]) -> tuple[list[dict[str, Any]] | None, Any]:
        refusal = _refusal(backend, quota, _usage(ledger), charge)
        if refusal is not None:
            return None, refusal
        reservations = list(ledger.get("reservations") or [])
        reservations.append({
            "key": key,
            "state": "creating",
            "cpu": charge.cpu,
            "memory": charge.memory,
            "reason": reason,
            "session_id": session_id,
            "assignment_id": assignment_id,
            "sandbox_id": None,
            "reserved_at": utcnow_iso(),
        })
        return reservations, None

    if (await _repo().read(backend)).get("inventory_at") is None:
        # A ledger no inventory has filled yet would count every running box as
        # absent.
        await reconcile_sandbox_quota(provider)
    refusal = await _update(backend, take)
    if refusal is not None:
        # The ledger may still charge a box destroyed since the last sweep.
        await reconcile_sandbox_quota(provider)
        refusal = await _update(backend, take)
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
    inventory = dict((await _repo().read(str(provider.name))).get("inventory") or {})
    box = inventory.get(sandbox_id)
    if box is not None and "released_charge" not in box:
        return None
    if box is not None:
        released = box["released_charge"]
        charge = SandboxCharge(cpu=float(released["cpu"]), memory=int(released["memory"]))
    else:
        charge = box_charge(provider, _shared_limits())
    return await reserve(provider, charge, reason="resume", session_id="", assignment_id=sandbox_id)


async def mark_created(backend: str, key: str, sandbox_id: str) -> None:
    def created(ledger: Mapping[str, Any]) -> tuple[list[dict[str, Any]] | None, None]:
        reservations = [dict(r) for r in ledger.get("reservations") or []]
        for reservation in reservations:
            if reservation.get("key") == key:
                reservation.update(state="created", sandbox_id=sandbox_id, created_at=utcnow_iso())
                return reservations, None
        return None, None

    await _update(backend, created)


async def release(backend: str, key: str) -> None:
    def drop(ledger: Mapping[str, Any]) -> tuple[list[dict[str, Any]] | None, None]:
        reservations = [r for r in ledger.get("reservations") or [] if r.get("key") != key]
        if len(reservations) == len(ledger.get("reservations") or []):
            return None, None
        return reservations, None

    await _update(backend, drop)


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
                cpu = int(metadata[QUOTA_CPU_METADATA_KEY]) / 1000
                memory = int(metadata[QUOTA_MEMORY_METADATA_KEY])
            except (KeyError, ValueError):
                # Created before this deployment recorded charges: charge the
                # larger kind of box, so the quota never counts it short.
                cpu, memory = unknown.cpu, unknown.memory
            box: dict[str, Any] = {
                "cpu": cpu,
                "memory": memory,
                "state": str(item.state or ""),
                "session_id": item.session_id,
            }
            if releases_when_paused and str(item.state or "").upper() == "PAUSED":
                # Saved and released: it holds nothing until reserve_resume
                # takes its charge back.
                box.update(cpu=0.0, memory=0, released_charge={"cpu": cpu, "memory": memory})
            boxes[str(item.sandbox_id)] = box
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
    unanswered past the grace with no box to show for it.
    """
    backend = str(provider.name)
    taken_at = datetime.now(timezone.utc)
    inventory = await _inventory(provider)
    released = 0
    for _ in range(_CAS_ATTEMPTS):
        ledger = await _repo().read(backend)
        kept: list[dict[str, Any]] = []
        released = 0
        for reservation in ledger.get("reservations") or []:
            sandbox_id = reservation.get("sandbox_id")
            if _held(inventory, sandbox_id):
                continue
            stamp = reservation.get("created_at") or reservation.get("reserved_at")
            at = datetime.fromisoformat(str(stamp)) if stamp else taken_at
            if sandbox_id and at < taken_at:
                released += 1
                continue
            if not sandbox_id and at + _CREATE_GRACE < taken_at:
                released += 1
                continue
            kept.append(reservation)
        if await _repo().compare_and_set(
            backend,
            version=int(ledger.get("version") or 0),
            reservations=kept,
            inventory=inventory,
            inventory_at=taken_at.isoformat(),
        ):
            break
    else:
        raise RuntimeError(f"sandbox quota ledger for {backend!r} stayed contended")
    return {f"{backend}.quota_boxes": len(inventory), f"{backend}.quota_released": released}


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
        ledger = await _repo().read(name)
        quota = quota_for(name)
        usage = _usage(ledger)
        inventory = dict(ledger.get("inventory") or {})
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
            "inventory_at": ledger.get("inventory_at"),
            "boxes": [
                {"sandbox_id": sandbox_id, **{k: box.get(k) for k in ("cpu", "memory", "state", "session_id")}}
                for sandbox_id, box in sorted(inventory.items())
            ],
            "reservations": [
                {k: r.get(k) for k in (
                    "state", "reason", "session_id", "sandbox_id", "cpu", "memory", "reserved_at",
                )}
                for r in ledger.get("reservations") or []
                if not _held(inventory, r.get("sandbox_id"))
            ],
        })
    return report
