"""The seam is asked, and what it answers decides whether the box may be used.

The seam existed for a long time with nothing calling it. It was declared, one
provider implemented it, and no code path in the product ever invoked it —
while durable volumes were wired straight into the sandbox create path beside
it. Two mechanisms for one guarantee, and the one with the contract was the one
that did nothing.

So these do not test path arithmetic. They test that the platform ASKS, and
WHEN: readiness is a condition of the BOX, settled once per planned mount while
the box is still just a box and before any engine runs against it. A provider
that fails is the only way to see it, because a provider that succeeds looks
exactly like no provider at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.provisioning import (
    workspace_is_ready,
    workspace_ref_for_subject,
)
from astrabox.seams.storage import StorageProvider, WorkspaceRef, register_storage


class _RecordingProvider(StorageProvider):
    """A medium that records what it was asked, and can refuse."""

    name = "test_recording"

    def __init__(self) -> None:
        self.prepared: list[tuple[str, str]] = []
        self.routed: list[tuple[str, str]] = []
        self.refuse = False

    async def prepare(self, ref: WorkspaceRef, *, box: Any, box_path: str) -> None:
        if self.refuse:
            raise RuntimeError("medium is not present")
        self.prepared.append((ref.key(), box_path))


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _RecordingProvider:
    """A refusing medium, registered where it cannot outlive the test.

    The registry is replaced with a copy first: a double left in the process
    registry is indistinguishable from a real installed plugin, and the next
    test to enumerate what is bundled sees it as one.
    """

    import astrabox.seams.storage as storage_seam

    monkeypatch.setattr(storage_seam, "_PROVIDERS", dict(storage_seam._PROVIDERS))
    recording = _RecordingProvider()
    register_storage(recording.name, recording)
    monkeypatch.setattr(storage_seam, "_CONFIGURED", recording.name)

    # The platform's mergerfs view is confirmed per mount as well;
    # this double records that check instead of running findmnt in a box.
    from astrabox.core.service.orchestrator.runtime.storage.mergerfs import workspace_router

    async def _route_confirmed(ref: WorkspaceRef, *, box: Any, box_path: str, **_: Any) -> None:
        recording.routed.append((ref.key(), box_path))

    monkeypatch.setattr(workspace_router, "prepare", _route_confirmed)
    return recording


def test_an_assistant_conversation_is_keyed_by_its_assistant() -> None:
    """The subject outlives every box and every conversation it holds."""

    ref = workspace_ref_for_subject(
        subject_kind="assistant_runtime",
        agent_id=None,
        assistant_id="asst_abc",
        session_id="session-1",
    )

    assert ref is not None
    assert ref.key() == "assistants/asst_abc"


def test_an_agent_conversation_hangs_below_its_agent() -> None:
    """The subject outlives the conversation, so the conversation is a subpath."""

    ref = workspace_ref_for_subject(
        subject_kind="deployment_conversation",
        agent_id="agent-7",
        assistant_id=None,
        session_id="session-1",
    )

    assert ref is not None
    assert ref.key() == "agents/agent-7/conversations/session-1"


def test_a_prepared_slot_names_the_agent_root_it_was_mounted_against() -> None:
    """No conversation exists yet, which is what makes the box lendable.

    A slot keyed to a conversation could only ever serve that one; the Agent
    root is the coarsest thing a mount decided before its subject exists can
    name, and every conversation that borrows the box lands under it.
    """

    ref = workspace_ref_for_subject(
        subject_kind="deployment_runtime", agent_id="agent-7", assistant_id=None
    )

    assert ref is not None
    assert ref.key() == "agents/agent-7"


def test_a_subject_this_seam_does_not_address_names_no_workspace() -> None:
    """None is an ordinary answer, not a failure."""

    assert (
        workspace_ref_for_subject(
            subject_kind="chat_session", agent_id="agent-7", assistant_id=None
        )
        is None
    )
    assert (
        workspace_ref_for_subject(
            subject_kind="deployment_runtime", agent_id="", assistant_id=None
        )
        is None
    )


@pytest.mark.asyncio
async def test_every_planned_mount_is_confirmed_before_the_box_is_used(
    provider: _RecordingProvider,
) -> None:
    """An Assistant plans two, and confirming one of them proves nothing.

    Its files and its engine's memory are separate mounts, and a box that got
    the workspace but not the config keeps the user's files while losing every
    conversation — which is the defect this line of work was reported as.
    """

    await workspace_is_ready(
        object(),
        (
            (
                "/home/conversations/asst/workspace",
                "/nas/assistants/asst_abc/workspace",
            ),
            ("/home/conversations/asst/.hermes", "/nas/assistants/asst_abc/config"),
        ),
        ref=WorkspaceRef(subject_kind="assistant", subject_id="asst_abc"),
    )

    assert provider.prepared == [
        ("assistants/asst_abc", "/home/conversations/asst/workspace"),
        ("assistants/asst_abc", "/home/conversations/asst/.hermes"),
    ]
    assert provider.routed == provider.prepared


@pytest.mark.asyncio
async def test_a_box_whose_mount_did_not_arrive_is_refused(
    provider: _RecordingProvider,
) -> None:
    """Starting an engine on it is how an agent writes where nothing will look.

    The failure has to reach the caller while the box is still just a box: work
    done against the container's own disk produces files the next box cannot
    see, and every frame of it looks like success.
    """

    provider.refuse = True

    with pytest.raises(RuntimeError):
        await workspace_is_ready(
            object(),
            (("/workspace", "/nas/assistants/asst_abc/workspace"),),
            ref=WorkspaceRef(subject_kind="assistant", subject_id="asst_abc"),
        )


@pytest.mark.asyncio
async def test_a_deployment_that_plans_no_mounts_asks_nothing(
    provider: _RecordingProvider,
) -> None:
    """Disposable boxes are a deployment shape, not a failure to report."""

    await workspace_is_ready(
        object(), (), ref=WorkspaceRef(subject_kind="agent", subject_id="agent-7")
    )

    assert provider.prepared == []
