from __future__ import annotations

__all__ = ["AgentPlatformService"]


def __getattr__(name: str):
    if name == "AgentPlatformService":
        from .platform_service import AgentPlatformService

        return AgentPlatformService
    raise AttributeError(name)
