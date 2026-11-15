"""Publish complete Hermes SessionDB snapshots with the image's mirror driver.

The profile state beside SessionDB (``hermes_profile_state``) is published to
its own archive the same way, checked less often because it changes rarely.
The commit protocol is the shared Assistant state mirror
(``astrabox_state_mirror``) over Hermes's layout.
"""

from __future__ import annotations

import astrabox_state_mirror
from hermes_state_bootstrap import LAYOUT

if __name__ == "__main__":
    raise SystemExit(astrabox_state_mirror.main(LAYOUT, __doc__))
