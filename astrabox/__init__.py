"""AstraBox.

A self-hosted agent runtime. Registered engine adapters own their vendor agent
loops; AstraBox owns session orchestration, the AI-SDK Data-Stream-Protocol SSE
surface, and the sandbox/runtime seams. Claude Code and Hermes are the built-in
engines.

Extension model
---------------
The runtime/sandbox layer is decomposed into a small set of seams, each a
``typing.Protocol`` declared in :mod:`astrabox.seams`. Concrete backends
(the built-in ``open_sandbox`` provider, or a third-party provider plugin)
implement these Protocols and register themselves through PEP 621 entry-points.
Resolution is name-keyed and fails loud: an unknown configured backend raises,
never silently falls back.

This top-level package intentionally carries no heavy imports so that
``import astrabox`` stays cheap and side-effect free; import the concrete
subpackages (``astrabox.seams``, ``astrabox.providers`` …) explicitly.
"""

from __future__ import annotations

from importlib.metadata import version as _distribution_version

__all__ = ["__version__"]

#: Package version, read from the installed distribution so ``pyproject.toml``
#: stays its only source. The release workflow tags every published image with
#: this value, and :mod:`astrabox.config.release_images` names the images a
#: deployment runs by it, so the two cannot disagree.
__version__ = _distribution_version("astrabox")
