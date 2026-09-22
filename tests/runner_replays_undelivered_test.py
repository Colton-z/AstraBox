"""A turn that finishes while the host is away still delivers its terminal.

The runner journal keeps each frame addressable by its original sequence until
a store-covered Result permits prefix compaction. Reattach replays strictly
after the host cursor; a failed write or replay attempt cannot remove a frame.
If that cursor has expired, the separate gap contract requests an authoritative
SessionStore rebuild instead of synthesizing the missing interval.
"""

from __future__ import annotations

from typing import Any

from astrabox.core.service.orchestrator.sandbox_runner import (
    EnvelopeSender,
    HistoryStoreSequence,
)


class FakeLink:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.frames: list[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return self.connected

    async def send(self, frame: dict[str, Any]) -> bool:
        self.frames.append(frame)
        return True


class DyingLink:
    """Reports connected until a write fails, exactly as a real socket does.

    A websocket does not announce its death; the first write to it raises, and
    ``WsLink.send`` turns that into ``False`` plus a closed flag. Until that
    write happens ``is_connected()`` answers ``True``, which is why a caller
    that asks before writing cannot know.
    """

    def __init__(self, fail_from: int = 1) -> None:
        self.connected = True
        self.frames: list[dict[str, Any]] = []
        self._writes = 0
        self._fail_from = fail_from

    def is_connected(self) -> bool:
        return self.connected

    async def send(self, frame: dict[str, Any]) -> bool:
        self._writes += 1
        if self._writes >= self._fail_from:
            self.connected = False
            return False
        self.frames.append(frame)
        return True


async def test_frames_sent_while_detached_arrive_on_reattach() -> None:
    live = FakeLink()
    sender = EnvelopeSender(live, "s-1")
    seen = await sender.send("event", message_type="AssistantMessage")

    live.connected = False
    await sender.send(
        "event",
        result_store_sequence=HistoryStoreSequence(7),
        message_type="ResultMessage",
    )
    await sender.send("status", state="idle")
    assert len(live.frames) == 1, "a dead link takes nothing"

    fresh = FakeLink()
    sender.set_link(fresh)
    replay = await sender.replay_after(seen)

    assert replay.gap is False
    assert replay.replayed == 2
    assert [f["op"] for f in fresh.frames] == ["event", "status"]
    assert fresh.frames[0]["message_type"] == "ResultMessage"
    # Original seq, not a renumbering: a consumer tracking the counter must see
    # a continuous run, and the seq is what binds a frame to its turn.
    assert [f["seq"] for f in fresh.frames] == [2, 3]


async def test_a_failed_reattach_keeps_the_replay_for_the_next_attach() -> None:
    # Dropping them again on the way out would turn one outage into permanent
    # loss, which is the failure this whole mechanism exists to prevent.
    sender = EnvelopeSender(FakeLink(connected=False), "s-1")
    await sender.send("event", message_type="A")
    await sender.send("event", message_type="B")

    dead = FakeLink(connected=False)
    sender.set_link(dead)
    failed = await sender.replay_after(0)
    assert failed.replayed == 0

    good = FakeLink()
    sender.set_link(good)
    replay = await sender.replay_after(0)
    assert replay.gap is False
    assert replay.replayed == 2
    assert [f["message_type"] for f in good.frames] == ["A", "B"]


async def test_the_frame_that_discovers_the_dead_link_is_held_not_lost() -> None:
    """The write that fails is the one a connectedness check cannot protect.

    Measured: a turn's ResultMessage was the frame that discovered the socket
    was gone. The link swallowed the failure and reported nothing, the sender
    counted it delivered, and the terminal existed nowhere — not on the wire,
    not in the hold. The session settled minutes later by another path while
    the bridge logged its quiet interval for 17 minutes.
    """
    dying = DyingLink()
    sender = EnvelopeSender(dying, "s-1")

    await sender.send(
        "event",
        result_store_sequence=HistoryStoreSequence(7),
        message_type="ResultMessage",
    )

    assert dying.frames == [], "the write failed, so nothing reached the host"

    fresh = FakeLink()
    sender.set_link(fresh)
    replay = await sender.replay_after(0)
    assert replay.gap is False
    assert replay.replayed == 1
    assert fresh.frames[0]["message_type"] == "ResultMessage"
    assert fresh.frames[0]["seq"] == 1, "the seq it was numbered with, unchanged"


async def test_a_replay_that_fails_mid_way_keeps_the_failing_frame() -> None:
    """Re-holding only the frames *after* the failure loses the failing one."""
    sender = EnvelopeSender(FakeLink(connected=False), "s-1")
    await sender.send("event", message_type="A")
    await sender.send("event", message_type="B")
    await sender.send("event", message_type="C")

    # Accepts A, then dies on B — B is unsent and must survive with C.
    dying = DyingLink(fail_from=2)
    sender.set_link(dying)
    interrupted = await sender.replay_after(0)
    assert interrupted.replayed == 1
    assert [f["message_type"] for f in dying.frames] == ["A"]

    good = FakeLink()
    sender.set_link(good)
    replay = await sender.replay_after(dying.frames[-1]["seq"])
    assert replay.gap is False
    assert replay.replayed == 2
    assert [f["message_type"] for f in good.frames] == ["B", "C"]


async def test_a_connected_host_never_accumulates_a_backlog() -> None:
    live = FakeLink()
    sender = EnvelopeSender(live, "s-1")
    for _ in range(5):
        await sender.send("event", message_type="x")

    assert len(live.frames) == 5
    assert sender.undelivered_count == 0, (
        "retaining delivered frames in the journal does not make them a backlog"
    )
    fresh = FakeLink()
    sender.set_link(fresh)
    replay = await sender.replay_after(sender.last_seq)
    assert replay.replayed == 0
    assert fresh.frames == []
