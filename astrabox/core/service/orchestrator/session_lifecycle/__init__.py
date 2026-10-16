"""A conversation's lifecycle on the platform side: its box, not its turns.

* :mod:`.service`: create, recover, take offline, end, and the permission mode.
* :mod:`.startup`: one startup from ``CREATING`` to ``READY``.
* :mod:`.recovery`: reattach to a live box or start a new one.
* :mod:`.ownership`: the claim that lets one startup drive a session.

The package imports nothing at package level, so importing :mod:`.ownership`
from the runtime manager does not pull in the services built on it.
"""
