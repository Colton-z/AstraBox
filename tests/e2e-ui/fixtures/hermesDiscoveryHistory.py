"""Seed a historical failed answer through Hermes' native session.create API.

Only the isolated acceptance image imports this fixture. A profile-local marker
supplies the seed for one new session; native SessionDB owns its persistence.
"""

import json
import os
from pathlib import Path

from tui_gateway import server

_create_session = server._create_session


def create_session(rid, params, *, copy_parent_history=False):
    marker = Path(os.environ["HERMES_HOME"]) / ".astrabox-e2e-discovery-history.json"
    if marker.is_file():
        messages = json.loads(marker.read_text())
        marker.unlink()
        params = {**params, "messages": messages}
    return _create_session(rid, params, copy_parent_history=copy_parent_history)


server._create_session = create_session
