"""Channel response judgments: failure, recovery and race behaviour, and their configuration.

A judge error, a re-drive after the judgment was recorded, and an addressed
copy arriving mid-judgment are timings a live deployment cannot place on
demand; the flows here use the channel spine harness (real sqlite channel
store, faked kernel). Live E2E covers the policies' ordinary paths.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

import astrabox.seams.channel_response as channel_response_seam
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.channel_response_judge import (
    ChannelResponseJudgmentError,
    ModelChannelResponseJudge,
)
from astrabox.persistence.repository.channel_repository import (
    INBOUND_RECEIVED,
    INBOUND_SETTLED,
)
from astrabox.seams.channel import (
    ChannelAttention,
    ChannelInbound,
    register_channel,
)
from astrabox.seams.channel_response import (
    ChannelContextMessage,
    ChannelResponseContext,
    ChannelResponseDecision,
    ChannelResponseJudge,
)
from tests.channel_spine_reliability_test import (
    _drain_spawned,
    _item_id,
    _RecorderChannel,
    _spine,
)
from tests.channel_spine_reliability_test import (
    _isolated_sqlite_state as _isolated_sqlite_state,
)

_T0 = "2026-10-01T00:00:00Z"
_T2 = "2026-10-01T00:00:02Z"
_INSTRUCTION = "Help with deploys."
_MENTION = ChannelAttention(is_mention=True)


class _Judge(ChannelResponseJudge):
    """Records every context it is asked about; answers ``decide``."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[ChannelResponseContext] = []
        self.decide: Any = True
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def judge(self, *, context: ChannelResponseContext) -> ChannelResponseDecision:
        self.calls.append(context)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if isinstance(self.decide, BaseException):
            raise self.decide
        return ChannelResponseDecision(respond=bool(self.decide), reason=f"respond={self.decide}")


@pytest.fixture
def judge(monkeypatch: pytest.MonkeyPatch) -> _Judge:
    fake = _Judge()
    monkeypatch.setattr(channel_response_seam, "_JUDGE", fake)
    return fake


def _said(
    dedup_key: str, content: str, *, at: str, attention: ChannelAttention | None = None,
) -> ChannelInbound:
    return ChannelInbound(
        content=content, dedup_key=dedup_key, conversation_key="room",
        participant="u-1", message_timestamp=at, retain_context=True, attention=attention,
    )


def _policy_spine(policy: str) -> tuple[_RecorderChannel, Any, Any, Any, Any]:
    provider = _RecorderChannel()
    register_channel(provider)
    service, ingress, kernel, spawned = _spine(binding_extra={
        "updated_at": "rev-1", "attention_policy": policy,
        "response_instruction": _INSTRUCTION,
    })
    return provider, service, ingress, kernel, spawned


async def _send(service: Any, provider: _RecorderChannel, inbound: ChannelInbound) -> dict[str, Any]:
    provider.inbound = inbound
    return await service.trigger("wh-1", headers={}, raw_body=b"{}")


def _sessions_started(service: Any) -> int:
    agent = service._test_agent_service
    return int(agent.start_conversation.await_count + agent.start_integration_conversation.await_count)


async def test_a_judge_error_fails_the_drive_with_backoff_and_is_never_an_answer(judge) -> None:
    judge.decide = RuntimeError("judge model unreachable")
    provider, service, ingress, kernel, spawned = _policy_spine("proactive")
    repo = ingress._channel_repo
    await _send(service, provider, _said("m-a", "can someone deploy?", at=_T0))
    await _drain_spawned(spawned)

    item_id = _item_id("wh-1", "m-a")
    record = await repo.get_inbound(item_id)
    assert record["state"] == INBOUND_RECEIVED and int(record["attempts"]) == 1
    assert "judge model unreachable" in record["last_error"]
    assert "judgment" not in record
    assert kernel.stream_calls == 0 and _sessions_started(service) == 0
    assert await repo.list_recoverable_inbound() == [], "not before the backoff watermark"
    assert [row["_id"] for row in await repo.list_recoverable_inbound(now=time.time() + 3600)] == [item_id]


async def test_a_re_drive_reads_the_recorded_judgment_instead_of_asking_again(judge) -> None:
    provider, service, ingress, kernel, spawned = _policy_spine("proactive")
    repo = ingress._channel_repo
    kernel.fail_next_stream = True  # the judged turn dies before its command
    await _send(service, provider, _said("m-a", "can someone deploy?", at=_T0))
    await _drain_spawned(spawned)
    item_id = _item_id("wh-1", "m-a")
    failed = await repo.get_inbound(item_id)
    assert failed["state"] == INBOUND_RECEIVED and failed["judgment"]["respond"] is True

    judge.decide = False  # a second judgement would decline
    later = time.time() + 3600
    reclaimed = await repo.claim_inbound_for_recovery(
        item_id=item_id, expected_generation=int(failed["generation"]), now=later,
    )
    assert reclaimed is not None
    await ingress._drive_work_item(reclaimed)

    record = await repo.get_inbound(item_id)
    assert len(judge.calls) == 1
    assert record["state"] == INBOUND_SETTLED
    assert record["judgment"] == failed["judgment"]
    assert kernel.stream_calls == 2 and len(kernel.commands) == 1


async def test_an_addressed_copy_during_judgment_runs_the_message_despite_a_decline(judge) -> None:
    judge.decide = False
    judge.release = asyncio.Event()
    provider, service, ingress, kernel, spawned = _policy_spine("proactive")
    repo = ingress._channel_repo
    await _send(service, provider, _said("m-a", "deploy please", at=_T0))
    drive = asyncio.create_task(spawned.pop(0))
    await asyncio.wait_for(judge.entered.wait(), timeout=5)

    copy = await _send(service, provider, _said("m-a", "deploy please", at=_T0, attention=_MENTION))
    assert copy["status"] == "in_progress"
    assert (await repo.get_inbound(_item_id("wh-1", "m-a")))["explicit_attention"] is True

    judge.release.set()
    await asyncio.wait_for(drive, timeout=10)
    await _drain_spawned(spawned)
    record = await repo.get_inbound(_item_id("wh-1", "m-a"))
    assert len(judge.calls) == 1
    assert record["state"] == INBOUND_SETTLED
    assert kernel.stream_calls == 1


@pytest.mark.parametrize("dead", [False, True], ids=["backoff_passed", "retries_exhausted"])
async def test_an_addressed_copy_reclaiming_a_failed_judgment_runs_the_message(judge, dead) -> None:
    judge.decide = RuntimeError("judge model unreachable")
    provider, service, ingress, kernel, spawned = _policy_spine("proactive")
    repo = ingress._channel_repo
    await _send(service, provider, _said("m-a", "deploy please", at=_T0))
    await _drain_spawned(spawned)
    item_id = _item_id("wh-1", "m-a")
    failed = await repo.get_inbound(item_id)
    owned = {"item_id": item_id, "owner_token": failed["owner_token"], "generation": int(failed["generation"])}
    if dead:
        assert await repo.fail_inbound(**owned, error="exhausted", retry_at_epoch=None)
    else:
        assert await repo.renew_inbound_lease(**owned, now=time.time() - 100_000)

    judge.decide = False  # a judgement now would decline
    copy = await _send(service, provider, _said("m-a", "deploy please", at=_T0, attention=_MENTION))
    assert copy["status"] == "accepted"
    await _drain_spawned(spawned)
    record = await repo.get_inbound(item_id)
    assert record["state"] == INBOUND_SETTLED and record["explicit_attention"] is True
    assert kernel.stream_calls == 1


class _Bindings:
    """Dict-backed deployment rows with the repository's write signatures."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def upsert(self, deployment_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        self.rows[deployment_id] = dict(doc)
        return dict(doc)

    async def get_for_deployment(self, deployment_id: str, agent_id: str, **_: Any) -> dict[str, Any] | None:
        row = self.rows.get(deployment_id)
        return dict(row) if row and row.get("agent_id") == agent_id else None

    async def update_for_deployment(
        self, deployment_id: str, agent_id: str, updates: dict[str, Any], **_: Any,
    ) -> dict[str, Any] | None:
        self.rows[deployment_id].update(updates)
        return dict(self.rows[deployment_id])


async def test_response_policies_need_a_judge_and_own_the_instruction(monkeypatch) -> None:
    register_channel(_RecorderChannel())
    service, _ingress, _kernel, _spawned = _spine()
    bindings = _Bindings()
    service._deployment_repo = bindings
    create = {"agent_id": "agent-1", "creator_user_id": "creator", "scene": "channel:recorder"}

    monkeypatch.setattr(channel_response_seam, "_JUDGE", None)
    monkeypatch.setenv("ASTRABOX_TITLE_MODEL_ENABLED", "false")
    for policy in ("participated", "proactive"):
        with pytest.raises(APIError) as refused:
            await service.create(**create, attention_policy=policy)
        assert (refused.value.code, refused.value.status_code) == ("CHANNEL_RESPONSE_JUDGE_UNAVAILABLE", 409)
    assert bindings.rows == {}

    monkeypatch.setattr(channel_response_seam, "_JUDGE", _Judge())
    for policy in ("all", "mentions"):
        with pytest.raises(APIError) as refused:
            await service.create(**create, attention_policy=policy, response_instruction="answer deploys")
        assert (refused.value.code, refused.value.status_code) == ("INVALID_REQUEST", 400)
    assert bindings.rows == {}

    created = await service.create(
        **create, attention_policy="proactive", response_instruction=f"  {_INSTRUCTION}  ",
    )
    deployment_id = created["deployment_id"]
    stored = bindings.rows[deployment_id]
    assert (stored["attention_policy"], stored["response_instruction"]) == ("proactive", _INSTRUCTION)

    updated = await service.update(
        deployment_id, agent_id="agent-1", patch={"attention_policy": "mentions"},
    )
    assert updated["attention_policy"] == "mentions"
    assert bindings.rows[deployment_id].get("response_instruction") is None


class _Model:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.prompts: list[str] = []

    async def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        self.prompts.append(user_prompt)
        return self.raw


def _context(rows: int = 0) -> ChannelResponseContext:
    return ChannelResponseContext(
        deployment_id="wh-1", channel_name="recorder", conversation="room",
        message=ChannelContextMessage(participant="u-1", content="now", timestamp=_T2),
        policy="proactive", instruction=_INSTRUCTION, binding_revision="rev-1",
        selection_reason="proactive_policy", participated=False,
        context=tuple(
            ChannelContextMessage(participant="u-2", content=f"row {index}", timestamp=_T0)
            for index in range(rows)
        ),
    )


@pytest.mark.parametrize("raw", [
    "not json",
    "[]",
    json.dumps({"respond": True, "reason": "useful", "extra": 1}),
    json.dumps({"respond": 1, "reason": "useful"}),
    json.dumps({"respond": True, "reason": "   "}),
])
async def test_the_model_judge_refuses_a_malformed_decision(raw) -> None:
    with pytest.raises(ChannelResponseJudgmentError):
        await ModelChannelResponseJudge(model=_Model(raw)).judge(context=_context())


async def test_the_model_judge_reads_a_strict_decision_over_the_last_fifty_rows() -> None:
    model = _Model(json.dumps({"respond": True, "reason": " it asks for a deploy "}))
    decision = await ModelChannelResponseJudge(model=model).judge(context=_context(rows=60))
    assert decision == ChannelResponseDecision(respond=True, reason="it asks for a deploy")
    [prompt] = model.prompts
    sent = json.loads(prompt)
    assert [row["text"] for row in sent["prior_messages"]] == [f"row {index}" for index in range(10, 60)]
    assert sent["current_message"]["text"] == "now"
    assert (sent["selection_reason"], sent["channel_instruction"]) == ("proactive_policy", _INSTRUCTION)
