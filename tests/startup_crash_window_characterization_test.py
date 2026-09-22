"""What a replica can find after a worker dies with the provider ahead of it.

These pin the two facts the sandbox-lifecycle work exists to preserve. Each
states an outcome a deployment needs rather than an implementation, so the
provider is free to use an idempotency key or searchable create metadata as
long as a replay reaches exactly the resource made by the same durable command.

The window is the ordinary one for a create that is not transactional with its
own record: the provider has made a sandbox and the caller has not yet written
down that it did. A single-writer control plane can leave that to a restart
sweep, because the process that made the call is the process that would have
recorded it. A control plane with several replicas cannot: the survivor has to
answer "is this box mine" from durable state and the provider's API alone.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.seams.sandbox import (
    SANDBOX_ASSIGNMENT_ID_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_VALUE,
    SANDBOX_SESSION_ID_METADATA_KEY,
)
from astrabox.seams.sandbox_disposal import claim_from_metadata


pytestmark = pytest.mark.asyncio


class _Inventory:
    """A provider's view: what it is running, and the stamps each box carries."""

    def __init__(self, boxes: dict[str, dict[str, str]]) -> None:
        self.boxes = dict(boxes)
        self.listed_pages = 0
        self.assignment_lookups: list[str] = []

    async def list_sandboxes(self, *, page: int = 1, page_size: int = 50) -> Any:
        self.listed_pages += 1
        items = [
            {"sandbox_id": sandbox_id, "metadata": dict(metadata)}
            for sandbox_id, metadata in sorted(self.boxes.items())
        ]
        start = (page - 1) * page_size
        return items[start : start + page_size]

    async def find_sandbox_by_assignment(
        self, assignment_id: str
    ) -> dict[str, Any] | None:
        self.assignment_lookups.append(assignment_id)
        matches = [
            {"sandbox_id": sandbox_id, "metadata": dict(metadata)}
            for sandbox_id, metadata in sorted(self.boxes.items())
            if metadata.get(SANDBOX_ASSIGNMENT_ID_METADATA_KEY) == assignment_id
        ]
        if len(matches) > 1:
            raise RuntimeError("one create assignment named multiple sandboxes")
        return matches[0] if matches else None


def _stamped(session_id: str, assignment_id: str) -> dict[str, str]:
    """The exact metadata a create writes, in the producer's own key names."""

    return {
        SANDBOX_SESSION_ID_METADATA_KEY: session_id,
        SANDBOX_MANAGED_BY_METADATA_KEY: SANDBOX_MANAGED_BY_METADATA_VALUE,
        SANDBOX_ASSIGNMENT_ID_METADATA_KEY: assignment_id,
    }


async def test_a_box_created_before_its_record_is_reachable_from_durable_state() -> None:
    """A survivor must be able to reach a box whose record was never written.

    The Session row exists — it is written before startup — and the box carries
    this deployment's stamp naming that Session. Both halves of the answer are
    therefore already durable at the moment of the crash. What a replica needs
    is a query that starts from the Session and asks the provider, rather than
    one that starts from a record the dead worker never got to write.
    """

    session_id = "sess-crashed-before-record"
    startup_command_id = "startup-command-survives-worker-crash"
    inventory = _Inventory(
        {"box-orphan": _stamped(session_id, startup_command_id)}
    )

    # The durable state a survivor actually has: a Session, and no allocation.
    session_row: dict[str, Any] = {
        "session_id": session_id,
        "state": "CREATING",
        "sandbox_id": None,
        "startup_allocation": None,
        "startup_command_id": startup_command_id,
    }

    # The candidate set every current sweep is built from.
    candidates = [
        row
        for row in [session_row]
        if isinstance(row.get("startup_allocation"), dict)
    ]
    assert candidates == [], (
        "precondition: a record-less Session is invisible to an allocation sweep"
    )

    # The same accepted startup command is replayed after a worker crash. Its
    # durable id is the provider lookup key, even though no allocation row was
    # ever written by the dead worker.
    reachable = await inventory.find_sandbox_by_assignment(
        session_row["startup_command_id"]
    )
    assert reachable is not None
    assert reachable["sandbox_id"] == "box-orphan"
    assert claim_from_metadata(
        sandbox_id=reachable["sandbox_id"],
        metadata=reachable["metadata"],
        expected_session_id=session_id,
        session_id_key=SANDBOX_SESSION_ID_METADATA_KEY,
        managed_by_key=SANDBOX_MANAGED_BY_METADATA_KEY,
        managed_by_value=SANDBOX_MANAGED_BY_METADATA_VALUE,
    ).verdict == "MINE"
    assert inventory.assignment_lookups == [startup_command_id]
    assert inventory.listed_pages == 0, (
        "startup recovery must not scan the provider's complete inventory"
    )


async def test_the_stamp_names_a_mutable_owner_rather_than_one_create_attempt() -> None:
    """Two boxes stamped for one Session cannot be told apart.

    `claim_of` decides what may be destroyed from the stamp a create wrote, and
    that stamp carries the Session id. A Session outlives its sandbox — it is
    replaced after an out-of-band death, and again after a reclaim — so the
    same value can be true of a box that is finished and of the box that
    replaced it. Asking "whose box is this" then has more than one answer, and
    the caller cannot tell which one it holds.
    """

    session_id = "sess-replaced-its-box"
    retired_assignment = "startup-command-retired"
    replacement_assignment = "startup-command-replacement"
    retired = _stamped(session_id, retired_assignment)
    replacement = _stamped(session_id, replacement_assignment)

    verdicts = {
        sandbox_id: claim_from_metadata(
            sandbox_id=sandbox_id,
            metadata=metadata,
            expected_session_id=session_id,
            session_id_key=SANDBOX_SESSION_ID_METADATA_KEY,
            managed_by_key=SANDBOX_MANAGED_BY_METADATA_KEY,
            managed_by_value=SANDBOX_MANAGED_BY_METADATA_VALUE,
        ).verdict
        for sandbox_id, metadata in (
            ("box-retired", retired),
            ("box-replacement", replacement),
        )
    }
    assert verdicts == {"box-retired": "MINE", "box-replacement": "MINE"}

    inventory = _Inventory(
        {"box-retired": retired, "box-replacement": replacement}
    )
    found = await inventory.find_sandbox_by_assignment(replacement_assignment)
    assert found is not None
    assert found["sandbox_id"] == "box-replacement"
    assert retired != replacement
