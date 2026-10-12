"""The standard data-plane implementation over execd, the image's in-box daemon.

Every built-in agent image carries OpenSandbox's execd. A sandbox provider whose
boxes run it, and whose ``get_endpoint`` resolves its port with the headers a
request needs, gets the contract's faces from this package instead of writing
its own. Core uses it too where it reaches a second execd inside a box, such as
a conversation's own execd under shared tenancy. Nothing here imports a
provider, so any provider and core can depend on it.
"""

from __future__ import annotations

from astrabox.providers.execd.commands import ExecdCommands
from astrabox.providers.execd.files import ExecdFiles
from astrabox.providers.execd.processes import ExecdProcesses

#: The port execd listens on inside a box; fixed by the daemon.
EXECD_PORT = 44772

__all__ = ["EXECD_PORT", "ExecdCommands", "ExecdFiles", "ExecdProcesses"]
