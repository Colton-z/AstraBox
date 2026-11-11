"""Gate Hermes startup on this box's platform initialization and native restore.

The protocol is the shared Assistant state gate (``astrabox_state_gate``); this
module supplies Hermes's layout: its home and environment file, SessionDB as
the native store copied through Hermes' own backup API, the profile state
beside it (``hermes_profile_state``), and the check that a restored SessionDB
belongs to this profile. Hermes modules are imported only where they are used,
so the root-run ``publish`` and ``ready`` steps need no Hermes runtime.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from typing import Any

import astrabox_state_gate as gate

#: The gateway's private creation receipts in ``state_meta``
#: (``POLICY_PREFIX`` in ``hermes_state_local.py`` at the pinned commit).
LOCAL_RECEIPT_KEYS = "gateway.local_policy.v1:*"


def verify_native_identity(home: Path) -> None:
    """Refuse a restored SessionDB whose conversations another home or account owns.

    Hermes' gateway derives a conversation's id from the owning profile's
    absolute HERMES_HOME, the principal that created it and the creation
    request, and it keys each admission by that principal; a client of the
    gateway's own account is ``uid:<uid>`` (``local_identity`` in
    ``gateway/session_local_recovery.py``, the control socket's peer subject in
    ``gateway/control_socket.py``). Each creation receipt records both. Under
    another home or uid no recorded conversation would resume, and an input
    sent again would be admitted as a new one, so the restore stops here.
    """
    database = home / "state.db"
    if not database.exists():
        return
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT key, value FROM state_meta WHERE key GLOB ?", (LOCAL_RECEIPT_KEYS,)
        ).fetchall()
    principal = f"uid:{os.geteuid()}"
    for key, value in rows:
        receipt = json.loads(value)
        owner = receipt.get("principal_id")
        if receipt.get("profile_id") != str(home) or (
            isinstance(owner, str) and owner.startswith("uid:") and owner != principal
        ):
            raise RuntimeError(
                f"Restored Hermes conversation {key.removeprefix(LOCAL_RECEIPT_KEYS.rstrip('*'))} belongs to "
                f"HERMES_HOME {receipt.get('profile_id')!r} as {owner!r}; this profile is "
                f"{str(home)!r} as {principal!r}"
            )


def _snapshot_native(home: Path, destination: Path) -> Any:
    from hermes_native_store import snapshot

    return snapshot(home, destination)


def _restore_native(source: Path, home: Path) -> Any:
    from hermes_native_store import restore

    return restore(source, home)


def _pack_profile(home: Path) -> bytes:
    import hermes_profile_state

    return hermes_profile_state.pack(home)


def _restore_profile(payload: bytes, home: Path) -> None:
    import hermes_profile_state

    hermes_profile_state.restore(payload, home)


def _stamp_profile(home: Path) -> Any:
    import hermes_profile_state

    return hermes_profile_state.stamp(home)


LAYOUT = gate.StateLayout(
    tag="HERMES",
    label="Hermes",
    marker=Path("/run/astrabox-hermes/profile-ready.json"),
    home_name=".hermes",
    env_name="astrabox-hermes.env",
    home_key="hermes_home",
    native_name="state.db",
    native_stamp_names=("state.db", "state.db-wal"),
    snapshot_native=_snapshot_native,
    restore_native=_restore_native,
    verify_native=verify_native_identity,
    pack_profile=_pack_profile,
    restore_profile=_restore_profile,
    stamp_profile=_stamp_profile,
)


def restore_once(env_path: Path) -> None:
    gate.restore_once(env_path, LAYOUT)


if __name__ == "__main__":
    raise SystemExit(gate.main(LAYOUT, __doc__))
