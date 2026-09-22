"""Kernel-facing alias of the session-snapshot ownership vocabulary.

The actual definitions live with the data model —
:mod:`astrabox.persistence.models.session_snapshot` — because the DAL
repository enforces the ownership split at the write boundary and must not
import kernel internals to do so (the dependency arrow points core → common).
Kernel projections keep importing the vocabulary under this path.
"""

from __future__ import annotations

from astrabox.persistence.models.session_snapshot import (
    SESSION_SNAPSHOT_FIELD_OWNERSHIP,
    SESSION_SNAPSHOT_WATERMARK_FIELDS,
    owned_fields_for_channel,
    validate_channel_ownership,
    watermark_field_for_channel,
)

__all__ = [
    "SESSION_SNAPSHOT_FIELD_OWNERSHIP",
    "SESSION_SNAPSHOT_WATERMARK_FIELDS",
    "owned_fields_for_channel",
    "validate_channel_ownership",
    "watermark_field_for_channel",
]
