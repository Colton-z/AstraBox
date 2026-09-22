"""Keep the frontend session-state vocabulary wider than backend output."""

from __future__ import annotations

import re
from pathlib import Path

from astrabox.core.model import SessionState


_FRONTEND_TYPES = Path(__file__).resolve().parents[1] / "frontend" / "src" / "types.ts"

# Projection-backed session reads overlay these values without storing them as
# lifecycle SessionState members.
_RENDERED_SESSION_STATES = {"PROCESSING", "WAITING_INPUT"}


def test_frontend_recognizes_every_backend_session_state() -> None:
    source = _FRONTEND_TYPES.read_text(encoding="utf-8")
    declaration = re.search(
        r"export\s+type\s+SessionState\s*=\s*(?P<body>.*?);",
        source,
        flags=re.DOTALL,
    )
    assert declaration is not None, "frontend types no longer declare SessionState"

    frontend_states = set(
        re.findall(r"[\"']([A-Z][A-Z0-9_]*)[\"']", declaration.group("body"))
    )
    assert frontend_states, "the frontend SessionState declaration contains no states"

    backend_states = {state.value for state in SessionState} | _RENDERED_SESSION_STATES
    missing = backend_states - frontend_states

    assert not missing, (
        "frontend SessionState is missing backend-emitted values: "
        f"{', '.join(sorted(missing))}"
    )
