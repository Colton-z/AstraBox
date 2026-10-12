"""Skill content acquisition by deployment-installed providers.

Catalogs select sources; these providers resolve and fetch their content. Core
owns staging, validation, cache publication and conversation installation.
Provider configuration contains public metadata and approved secret names only.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:
    from astrabox.seams.sandbox import SandboxHandle

SkillDescriptor: TypeAlias = str | dict[str, Any]


@dataclass(frozen=True)
class SkillSource:
    """A source declaration, with a relative directory and installed name.

    ``config`` is validated by the selected provider. It must contain no secret
    material: declarations are persisted and returned by the public API.
    """

    provider: str
    source: str
    version: str
    name: str
    path: str = ""
    config: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedSkillSource:
    """The immutable content identity authorized for this acquisition.

    ``version`` is the concrete version the provider fetches. ``content_id``
    distinguishes content when the supplier's version is not globally unique.
    Both are public cache metadata, never credentials or signed URLs.
    """

    version: str
    content_id: str


class SkillContentProvider(ABC):
    """Trusted acquisition code registered by the deployment, never an Agent.

    Core calls ``validate`` on save and use, and ``resolve`` before EVERY cache
    lookup. Resolution must authorize the source for the named credential and
    return an immutable identity. A cache hit skips only ``fetch``.

    Both async methods receive an invocation-private, root-only directory
    outside the workspace. Put temporary credentials there, using the sandbox
    file API rather than shell text or command environment. Core removes it on
    success, error and cancellation. Providers must close their own connections
    and must not put secrets in results, commands, logs or exception messages.

    ``fetch`` writes the source tree only to ``destination``. It never publishes
    cache markers. Core does that after fetch returns and content is validated,
    so a command surviving a cancelled caller cannot publish its own result.
    Required in-box tools must already be present in the sandbox image.
    """

    name: str

    async def write_credential(
        self, sandbox: SandboxHandle, private_dir: str, name: str, content: bytes,
    ) -> None:
        """Write a private file and settle its write before propagating cancellation.

        Cancelling an HTTP file write cannot retract a request already accepted
        by a sandbox. Waiting for its outcome keeps cleanup after the write,
        so an in-flight write cannot recreate a credential after cleanup.
        """
        if not name or name in {".", ".."} or "/" in name:
            raise ValueError("a skill credential file needs one basename")
        task = asyncio.create_task(sandbox.files.write_bytes(
            f"{private_dir}/{name}", content, mode=0o600,
        ))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    @abstractmethod
    def validate(self, source: SkillSource) -> None:
        """Refuse unsupported configuration and unauthorized credential names."""

    @abstractmethod
    def egress_hosts(self, source: SkillSource) -> tuple[str, ...]:
        """All sandbox network hosts acquisition needs, for the author boundary."""

    @abstractmethod
    async def resolve(
        self, source: SkillSource, sandbox: SandboxHandle, private_dir: str,
    ) -> ResolvedSkillSource:
        """Authorize the source and resolve its immutable content identity."""

    @abstractmethod
    async def fetch(
        self, source: SkillSource, resolved: ResolvedSkillSource,
        sandbox: SandboxHandle, destination: str, private_dir: str,
    ) -> None:
        """Prepare the resolved source tree in the initially absent destination."""


_PROVIDERS: dict[str, SkillContentProvider] = {}


def register_skill_content_provider(provider: SkillContentProvider) -> None:
    """Register one complete provider under its explicit name."""
    if not isinstance(provider, SkillContentProvider) or not provider.name.strip():
        raise ValueError("a skill content provider needs a name and the complete contract")
    existing = _PROVIDERS.get(provider.name)
    if existing is not None and type(existing) is not type(provider):
        raise ValueError(f"skill content provider {provider.name!r} is already registered")
    _PROVIDERS[provider.name] = provider


def registered_skill_content_provider_names() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


def skill_content_provider(name: str) -> SkillContentProvider:
    """Select the named provider; an unknown name never selects Git."""
    try:
        return _PROVIDERS[name]
    except KeyError:
        raise ValueError(f"unknown skill content provider {name!r}") from None
