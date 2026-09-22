"""A Pi sub-agent, from a delegating tool call to a run the console shows.

The snapshots here were taken verbatim from a live `pi --mode rpc` session in a
real sandbox: the model called `subagent`, and the `pi-subagents` package
pushed its `subagent-async` widget about once a second for as long as the run
lived. That widget is the package's documented seam for an RPC host — "RPC
hosts receive live async status through the bounded `subagent-async` widget" —
and it is the only thing on the wire that can close a background child, because
the package's own completion events travel on `pi.events`, which it documents
as in-process only.

The foreground payloads follow the package's published `SingleResult` rows,
which are what a blocking `async: false` launch answers with.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.pi import PiEngineAdapter
from astrabox.core.service.orchestrator.engine.pi_child_runs import (
    ASYNC_SNAPSHOT_PREFIX,
    PiChildResources,
    PiChildRunError,
    child_reference,
    step_child_id,
    stop_command_line,
)

RUN = "800aa0b3-865c-402d-bec5-873e1eabca26"


def _widget(runs: list[dict[str, Any]], *, version: int = 1) -> dict[str, Any]:
    """One `extension_ui_request`, shaped as the live session emitted it."""

    payload = {
        "kind": "pi-subagents.async-status-snapshot",
        "version": version,
        "generatedAt": 1789013355555,
        "caps": {"maxRuns": 20, "maxChildrenPerNode": 8, "maxDepth": 3},
        "omitted": {"runs": 0, "children": 0, "byteLimitExceeded": False},
        "runs": runs,
    }
    return {
        "type": "extension_ui_request",
        "id": "ac9b90ee-7761-4ac5-b1f2-139c65012dbf",
        "method": "setWidget",
        "widgetKey": "subagent-async",
        "widgetLines": [ASYNC_SNAPSHOT_PREFIX + json.dumps(payload)],
    }


def _run_node(state: str, *, children: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": RUN,
        "kind": "subagent",
        "label": "delegate",
        "state": state,
        "startedAt": 1789013355508,
        "updatedAt": 1789013355524,
        "children": children if children is not None else [],
    }


def _tool_event(
    event_type: str, details: dict[str, Any], *, tool_name: str = "subagent"
) -> dict[str, Any]:
    payload = {"content": "…", "details": details, "isError": False}
    event: dict[str, Any] = {
        "type": event_type,
        "toolCallId": "call-1",
        "toolName": tool_name,
    }
    if event_type == "tool_execution_update":
        event["partialResult"] = payload
    else:
        event["result"] = payload
        event["isError"] = False
    return event


def _facts(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [frame["data"] for frame in frames]


# ── the reference the seam carries ───────────────────────────────────────
def test_a_launch_without_child_nodes_is_addressed_as_its_run() -> None:
    assert child_reference(RUN) == RUN
    assert stop_command_line(RUN) == f"/subagents-stop {RUN}"


def test_a_child_is_addressed_by_the_node_id_the_snapshot_gave_us() -> None:
    """The package takes back "exactly the node id the host received"."""

    reference = child_reference(RUN, "step:0")
    assert reference == f"{RUN}/step:0"
    assert stop_command_line(reference) == f"/subagents-stop {RUN} step:0"


def test_a_row_reported_by_position_is_spelled_the_snapshots_way() -> None:
    assert step_child_id(1) == "step:1"


def test_a_malformed_control_reference_fails_loudly() -> None:
    with pytest.raises(PiChildRunError, match="control reference"):
        stop_command_line("")
    with pytest.raises(PiChildRunError, match="control reference"):
        stop_command_line(f"{RUN}/")


# ── the background run the console shows ─────────────────────────────────
def test_the_status_widget_opens_a_stoppable_run() -> None:
    projector = PiChildResources()

    facts = _facts(projector.observe_ui_request(_widget([_run_node("running")])))

    assert [fact["event"] for fact in facts] == ["opened"]
    assert facts[0]["engineRef"] == RUN
    assert facts[0]["engineStatus"] == "running"
    assert facts[0]["operations"] == ["stop"]
    assert facts[0]["controlRef"] == RUN
    assert facts[0]["description"] == "delegate"
    assert facts[0]["taskType"] == "subagent"


def test_a_workflow_and_its_worker_keep_their_native_kinds_after_cold_replay() -> None:
    """A background workflow publishes two native jobs, not one duplicated worker."""

    workflow_id = "0d5052a6-7ee2-4a0f-9018-02255ba261c5"
    journal = []
    for workflow_status, worker_status in (
        ("running", "running"),
        ("running", "complete"),
        ("complete", "complete"),
    ):
        worker = _run_node(
            worker_status,
            children=[{
                "id": "step:0", "kind": "step", "label": "delegate",
                "state": worker_status,
            }],
        )
        workflow = {
            **_run_node(workflow_status),
            "id": workflow_id,
            "kind": "workflow",
            "children": [{
                "id": "child", "kind": "step", "label": "child",
                "state": worker_status,
            }],
        }
        journal.append(_widget([worker, workflow]))

    projector = PiChildResources()
    live = [
        fact
        for record in journal
        for fact in _facts(projector.observe_ui_request(record))
    ]
    cold = [
        fact.as_frame()["data"]
        for _, fact in PiEngineAdapter().durable_child_resource_facts(
            json.loads(json.dumps(journal))
        )
    ]

    assert cold == live
    assert [
        (fact["engineRef"], fact["taskType"], fact["event"], fact["engineStatus"])
        for fact in live
    ] == [
        (RUN, "subagent", "opened", "running"),
        (workflow_id, "workflow", "opened", "running"),
        (RUN, "subagent", "closed", "complete"),
        (workflow_id, "workflow", "closed", "complete"),
    ]
    assert all("parentEngineRef" not in fact for fact in live)
    assert all(fact["description"] == "delegate" for fact in live)
    assert all(fact["operations"] == [] for fact in live[-2:])


def test_a_single_delegation_is_one_row_from_queued_to_done() -> None:
    """The run is the row, and its one step adds nothing a reader could do.

    Measured on a real box: a run first appears `queued` with no steps, and
    the step arrives once it starts. The run's identity is the one constant,
    and for a one-step run the package states that stopping the run and
    stopping the child are the same act.
    """

    projector = PiChildResources()
    opened = _facts(projector.observe_ui_request(_widget([_run_node("queued")])))
    started = _facts(
        projector.observe_ui_request(
            _widget(
                [
                    _run_node(
                        "running",
                        children=[
                            {"id": "step:0", "kind": "step", "label": "delegate", "state": "running"}
                        ],
                    )
                ]
            )
        )
    )

    assert [(fact["event"], fact["engineRef"]) for fact in opened] == [("opened", RUN)]
    assert [(fact["event"], fact["engineRef"], fact["engineStatus"]) for fact in started] == [
        ("updated", RUN, "running")
    ]


def test_a_fan_out_shows_each_step_under_the_run() -> None:
    """Several children are several agents, each with its own stop."""

    projector = PiChildResources()
    facts = _facts(
        projector.observe_ui_request(
            _widget(
                [
                    _run_node(
                        "running",
                        children=[
                            {"id": "step:0", "kind": "step", "label": "scout", "state": "running"},
                            {"id": "step:1", "kind": "step", "label": "worker", "state": "queued"},
                        ],
                    )
                ]
            )
        )
    )

    assert [fact["engineRef"] for fact in facts] == [
        RUN,
        f"{RUN}/step:0",
        f"{RUN}/step:1",
    ]
    assert [fact.get("parentEngineRef") for fact in facts] == [None, RUN, RUN]
    assert [fact["description"] for fact in facts] == ["delegate", "scout", "worker"]
    assert [fact["taskType"] for fact in facts] == ["subagent", "step", "step"]
    assert all(fact["operations"] == ["stop"] for fact in facts)
    assert [reference for _, reference in projector.inspect_requests()] == [
        RUN, f"{RUN}/step:0", f"{RUN}/step:1",
    ]


def test_a_step_that_fans_out_itself_nests_its_own_steps() -> None:
    """The package nests up to three deep; the rule applies at every level."""

    facts = _facts(
        PiChildResources().observe_ui_request(
            _widget(
                [
                    _run_node(
                        "running",
                        children=[
                            {
                                "id": "step:0",
                                "kind": "step",
                                "label": "scout",
                                "state": "running",
                                "children": [
                                    {"id": "step:0.0", "kind": "step", "label": "a", "state": "queued"},
                                    {"id": "step:0.1", "kind": "step", "label": "b", "state": "queued"},
                                ],
                            },
                            {"id": "step:1", "kind": "step", "label": "worker", "state": "queued"},
                        ],
                    )
                ]
            )
        )
    )

    assert [(fact["engineRef"], fact.get("parentEngineRef")) for fact in facts] == [
        (RUN, None),
        (f"{RUN}/step:0", RUN),
        (f"{RUN}/step:0.0", f"{RUN}/step:0"),
        (f"{RUN}/step:0.1", f"{RUN}/step:0"),
        (f"{RUN}/step:1", RUN),
    ]


def test_the_reply_may_not_rename_the_child() -> None:
    """Measured: the reply's `label` carries the run id, not the agent name."""

    projector = PiChildResources()
    request_id, _ = _opened(projector)

    facts = _facts(
        projector.observe_inspect_reply(
            _inspect_reply(request_id, status="paused", label=RUN, messages=[])
        )
    )

    assert [fact["description"] for fact in facts] == ["delegate"]
    assert [fact["taskType"] for fact in facts] == ["subagent"]


def test_a_blocking_launchs_rows_have_no_run_above_them() -> None:
    """That launch reports rows only; naming a parent nobody projected would
    leave a row pointing at a run the console never received."""

    facts = _facts(
        PiChildResources().observe_tool_event(
            _tool_event(
                "tool_execution_end",
                {
                    "mode": "single",
                    "runId": RUN,
                    "results": [
                        {"index": 0, "agent": "scout", "task": "a", "exitCode": 0, "usage": {}}
                    ],
                },
            )
        )
    )

    assert all("parentEngineRef" not in fact for fact in facts)
    assert all("taskType" not in fact for fact in facts)


def test_foreground_workflow_result_settles_the_native_worker_without_a_duplicate_root() -> None:
    projector = PiChildResources()
    journal = [
        _widget([_run_node("running", children=[{"id": "step:0", "kind": "step", "state": "running"}])]),
        _tool_event("tool_execution_end", {
            "mode": "workflow", "runId": "workflow-call",
            "workflowChildren": {"children": [{"childId": "child", "runId": RUN, "state": "completed"}]},
            "results": [{"index": 0, "workflowKey": "child", "agent": "delegate", "exitCode": 0}],
        }),
        _widget([_run_node("complete", children=[{"id": "step:0", "kind": "step", "state": "complete"}])]),
    ]

    def replay(target):
        return [fact for record in journal for fact in _facts(
            target.observe_tool_event(record) if record["type"] == "tool_execution_end"
            else target.observe_ui_request(record)
        )]

    facts = replay(projector)
    assert [(fact["engineRef"], fact["event"]) for fact in facts] == [(RUN, "opened"), (RUN, "closed")]
    assert all("parentEngineRef" not in fact for fact in facts)
    assert replay(PiChildResources()) == facts


def test_a_repushed_snapshot_says_nothing_new() -> None:
    """The widget arrives about once a second; only changes are facts."""

    projector = PiChildResources()
    projector.observe_ui_request(_widget([_run_node("running")]))

    assert projector.observe_ui_request(_widget([_run_node("running")])) == []


def test_a_terminal_state_closes_the_run() -> None:
    """Nothing else on the wire can: completion events never leave Pi."""

    projector = PiChildResources()
    projector.observe_ui_request(_widget([_run_node("running")]))

    facts = _facts(projector.observe_ui_request(_widget([_run_node("complete")])))

    assert [fact["event"] for fact in facts] == ["closed"]
    assert facts[0]["engineStatus"] == "complete"
    assert facts[0]["operations"] == []
    assert "controlRef" not in facts[0]


def test_a_stopped_run_closes_with_the_packages_own_word() -> None:
    projector = PiChildResources()
    projector.observe_ui_request(_widget([_run_node("running")]))

    facts = _facts(projector.observe_ui_request(_widget([_run_node("stopped")])))

    assert [fact["engineStatus"] for fact in facts] == ["stopped"]


def test_a_paused_run_stays_open_but_offers_no_stop() -> None:
    """The package stops "only pending or running children" and rejects the rest."""

    projector = PiChildResources()
    projector.observe_ui_request(_widget([_run_node("running")]))

    facts = _facts(projector.observe_ui_request(_widget([_run_node("paused")])))

    assert [fact["event"] for fact in facts] == ["updated"]
    assert facts[0]["operations"] == []


def test_a_closed_run_is_never_reopened() -> None:
    projector = PiChildResources()
    projector.observe_ui_request(_widget([_run_node("running")]))
    projector.observe_ui_request(_widget([_run_node("complete")]))

    assert projector.observe_ui_request(_widget([_run_node("running")])) == []


def test_a_state_this_version_does_not_know_fails_loudly() -> None:
    """Guessing an unknown state closes or holds a run on an invention."""

    projector = PiChildResources()

    with pytest.raises(PiChildRunError, match="unknown state"):
        projector.observe_ui_request(_widget([_run_node("quantum")]))


def test_a_snapshot_version_we_did_not_read_fails_loudly() -> None:
    projector = PiChildResources()

    with pytest.raises(PiChildRunError, match="version"):
        projector.observe_ui_request(_widget([_run_node("running")], version=2))


def test_another_widget_is_not_a_sub_agent_snapshot() -> None:
    projector = PiChildResources()
    other = _widget([_run_node("running")])
    other["widgetKey"] = "some-other-extension"

    assert projector.observe_ui_request(other) == []


# ── the foreground run the tool call owns ────────────────────────────────
def test_a_blocking_launch_closes_when_its_call_does() -> None:
    projector = PiChildResources()

    facts = _facts(
        projector.observe_tool_event(
            _tool_event(
                "tool_execution_end",
                {
                    "mode": "single",
                    "runId": RUN,
                    "results": [
                        {"index": 0, "agent": "scout", "task": "look", "exitCode": 0, "usage": {}}
                    ],
                },
            )
        )
    )

    assert [fact["event"] for fact in facts] == ["opened", "closed"]
    assert facts[-1]["engineStatus"] == "complete"
    assert facts[-1]["operations"] == []
    assert all("controlRef" not in fact for fact in facts)


def test_a_background_launch_is_left_to_the_status_widget() -> None:
    """Its call answers the moment the run exists, which is not its end.

    Reading `asyncId` as an ordinary result would close every background child
    at launch, leaving nothing to stop — and `async` is the package's default.
    """

    projector = PiChildResources()

    facts = projector.observe_tool_event(
        _tool_event(
            "tool_execution_end",
            {
                "mode": "single",
                "runId": RUN,
                "results": [],
                "asyncId": RUN,
                "asyncDir": f"/tmp/pi-subagents-uid-2000/async-subagent-runs/{RUN}",
                "context": "fresh",
            },
        )
    )

    assert facts == []


def test_the_capability_listing_is_not_a_launch() -> None:
    """`action: "list"` answers on the same tool and names no run."""

    projector = PiChildResources()

    assert (
        projector.observe_tool_event(
            _tool_event(
                "tool_execution_end",
                {"mode": "management", "results": [], "agentCapabilities": {"agents": []}},
            )
        )
        == []
    )


def test_a_non_integer_exit_code_fails_loudly() -> None:
    """Guessing settlement from a shape closes a run that is still going."""

    projector = PiChildResources()

    with pytest.raises(PiChildRunError, match="exitCode"):
        projector.observe_tool_event(
            _tool_event(
                "tool_execution_end",
                {
                    "mode": "single",
                    "runId": RUN,
                    "results": [
                        {"index": 0, "agent": "s", "task": "a", "exitCode": "0", "usage": {}}
                    ],
                },
            )
        )


def test_another_tool_is_not_a_sub_agent() -> None:
    projector = PiChildResources()

    assert (
        projector.observe_tool_event(
            _tool_event(
                "tool_execution_end",
                {"mode": "single", "runId": RUN, "results": []},
                tool_name="bash",
            )
        )
        == []
    )


# ── the package has to reach the file Pi actually reads ──────────────────
def test_the_launch_settings_carry_the_images_sub_agent_package() -> None:
    """The launch REPLACES `settings.json`, so the image cannot seed it.

    A copy written into the image is overwritten before Pi reads it, and the
    only sign is a model reporting that no sub-agent tool exists — measured,
    with the package installed and its own settings file sitting unread in
    another account's home.
    """

    from types import SimpleNamespace

    from astrabox.core.service.orchestrator.engine.pi import (
        PI_SUBAGENT_PACKAGE_PATH,
        _settings,
    )

    settings = _settings(SimpleNamespace(engine_options=None))

    assert settings["packages"] == [PI_SUBAGENT_PACKAGE_PATH]


def test_an_agents_own_packages_do_not_displace_it() -> None:
    """Declaring a package is adding one, not taking the engine's away."""

    from types import SimpleNamespace

    from astrabox.core.service.orchestrator.engine.pi import (
        PI_SUBAGENT_PACKAGE_PATH,
        _settings,
    )

    settings = _settings(
        SimpleNamespace(
            engine_options={
                "settings": {
                    "packages": ["npm:pi-goal-x"],
                    "defaultThinkingLevel": "medium",
                }
            }
        )
    )

    assert settings["packages"] == ["npm:pi-goal-x", PI_SUBAGENT_PACKAGE_PATH]
    assert settings["defaultThinkingLevel"] == "medium"


def test_the_package_is_never_listed_twice() -> None:
    from types import SimpleNamespace

    from astrabox.core.service.orchestrator.engine.pi import (
        PI_SUBAGENT_PACKAGE_PATH,
        _settings,
    )

    settings = _settings(
        SimpleNamespace(
            engine_options={"settings": {"packages": [PI_SUBAGENT_PACKAGE_PATH]}}
        )
    )

    assert settings["packages"] == [PI_SUBAGENT_PACKAGE_PATH]


# ── what the child said ──────────────────────────────────────────────────
def _inspect_reply(request_id: str, **fields: Any) -> dict[str, Any]:
    """One `extension_ui_request` shaped as the package's inspect reply."""

    from astrabox.core.service.orchestrator.engine.pi_child_runs import (
        INSPECT_REPLY_PREFIX,
    )

    payload = {
        "kind": "pi-subagents.inspect-reply",
        "version": 1,
        "requestId": request_id,
        "asyncId": RUN,
        **fields,
    }
    return {
        "type": "extension_ui_request",
        "id": "0b1e0c2f-1111-4a4a-9f9f-abcdefabcdef",
        "method": "setWidget",
        "widgetKey": "subagent-inspect",
        "widgetLines": [INSPECT_REPLY_PREFIX + json.dumps(payload, ensure_ascii=False)],
    }


def _opened(projector: PiChildResources) -> tuple[str, str]:
    """Open the run and take the one inspect request that opening owes."""

    projector.observe_ui_request(_widget([_run_node("running")]))
    (request,) = projector.inspect_requests()
    return request


def test_the_inspect_command_names_the_run_and_the_child_the_snapshot_gave() -> None:
    from astrabox.core.service.orchestrator.engine.pi_child_runs import (
        inspect_command_line,
    )

    assert inspect_command_line(RUN, "astrabox-1") == (
        f"/subagents-inspect-rpc astrabox-1 {RUN} --lines 200"
    )
    assert inspect_command_line(f"{RUN}/step:0", "astrabox-2") == (
        f"/subagents-inspect-rpc astrabox-2 {RUN} step:0 --lines 200"
    )


def test_a_snapshot_change_owes_exactly_one_read() -> None:
    projector = PiChildResources()
    projector.observe_ui_request(_widget([_run_node("running")]))

    first = projector.inspect_requests()
    again = projector.inspect_requests()

    assert [reference for _, reference in first] == [RUN]
    assert again == []


def test_a_repushed_snapshot_owes_no_read() -> None:
    projector = PiChildResources()
    _opened(projector)

    projector.observe_ui_request(_widget([_run_node("running")]))

    assert projector.inspect_requests() == []


def test_a_hidden_single_step_is_read_without_closing_its_running_parent() -> None:
    projector = PiChildResources()
    node = _run_node(
        "running",
        children=[{"id": "step:0", "kind": "step", "label": "delegate", "state": "running"}],
    )
    opened = _facts(projector.observe_ui_request(_widget([node])))
    assert [fact["engineRef"] for fact in opened] == [RUN]
    assert opened[0]["controlRef"] == RUN
    ((request_id, target),) = projector.inspect_requests()
    assert target == f"{RUN}/step:0"

    facts = _facts(
        projector.observe_inspect_reply(
            _inspect_reply(
                request_id,
                childId="step:0",
                status="complete",
                messages=[{"role": "user", "kind": "text", "text": "Delegated task"}],
            )
        )
    )
    assert [(fact["kind"], fact["engineRef"]) for fact in facts] == [("message", RUN)]
    assert projector.observe_ui_request(_widget([node])) == []
    assert projector.inspect_requests() == []

    closed = _facts(projector.observe_ui_request(_widget([{**node, "state": "complete"}])))
    assert [(fact["event"], fact["engineRef"]) for fact in closed] == [("closed", RUN)]


@pytest.mark.parametrize(
    "activity_change",
    [
        {"updatedAt": 1789495876682},
        {
            "activity": {
                "lastActivityAt": 1789495876682,
                "currentTool": "bash",
                "turnCount": 1,
                "toolCount": 1,
            }
        },
    ],
)
def test_running_activity_reads_again_after_an_early_empty_inspection(
    activity_change: dict[str, Any],
) -> None:
    projector = PiChildResources()
    projector.observe_ui_request(_widget([_run_node("queued")]))
    ((first_id, reference),) = projector.inspect_requests()
    assert reference == RUN
    projector.observe_inspect_reply(_inspect_reply(first_id, status="running", label=RUN))

    # The native e0097 sequence stayed running while the child's bash began.
    # Its first successful inspection had neither task nor messages yet.
    running = _run_node("running") | activity_change
    assert projector.observe_ui_request(_widget([running])) == []
    ((next_id, reference),) = projector.inspect_requests()
    assert reference == RUN
    assert next_id != first_id
    marker = "CHILD_pi_5144bf68cc014425915769b11febf5e3"
    facts = _facts(
        projector.observe_inspect_reply(
            _inspect_reply(
                next_id,
                status="running",
                messages=[{"role": "user", "kind": "text", "text": marker}],
            )
        )
    )
    assert [(fact["kind"], fact["role"], fact["content"][0]["text"]) for fact in facts] == [
        ("message", "user", marker),
    ]

    repushed = _widget([running])
    payload = json.loads(repushed["widgetLines"][0][len(ASYNC_SNAPSHOT_PREFIX):])
    payload["generatedAt"] += 1
    repushed["widgetLines"] = [ASYNC_SNAPSHOT_PREFIX + json.dumps(payload)]
    assert projector.observe_ui_request(repushed) == []
    assert projector.inspect_requests() == []


def test_the_reply_becomes_the_childs_own_messages() -> None:
    projector = PiChildResources()
    request_id, _ = _opened(projector)

    facts = _facts(
        projector.observe_inspect_reply(
            _inspect_reply(
                request_id,
                status="running",
                label="delegate",
                messages=[
                    {"role": "user", "kind": "text", "text": "Compute 6*7."},
                    {"role": "assistant", "kind": "toolCall", "text": "bash", "name": "bash"},
                    # Measured: a tool result arrives as a text row under
                    # pi's own role; the platform's child view carries only
                    # assistant and user and refuses the whole list otherwise.
                    {"role": "toolResult", "kind": "text", "text": "EISDIR: illegal operation"},
                    {"role": "assistant", "kind": "text", "text": "42"},
                ],
            )
        )
    )

    messages = [fact for fact in facts if fact["kind"] == "message"]
    assert [(fact["role"], fact["content"][0]["text"]) for fact in messages] == [
        ("user", "Compute 6*7."),
        ("assistant", "42"),
    ]
    assert all(fact["engineRef"] == RUN for fact in messages)


def test_a_re_read_does_not_say_the_same_thing_twice() -> None:
    """The reply is a window re-read from the artifacts, not a delta."""

    projector = PiChildResources()
    request_id, _ = _opened(projector)
    rows = [{"role": "assistant", "kind": "text", "text": "half"}]
    projector.observe_inspect_reply(_inspect_reply(request_id, status="running", messages=rows))

    projector.observe_ui_request(_widget([_run_node("paused")]))
    (second_id, _) = projector.inspect_requests()[0]
    facts = _facts(
        projector.observe_inspect_reply(
            _inspect_reply(
                second_id,
                status="paused",
                messages=rows + [{"role": "assistant", "kind": "text", "text": "done"}],
            )
        )
    )

    texts = [fact["content"][0]["text"] for fact in facts if fact["kind"] == "message"]
    assert texts == ["done"]


def test_the_final_output_arrives_once_and_closes_the_run() -> None:
    projector = PiChildResources()
    request_id, _ = _opened(projector)

    facts = _facts(
        projector.observe_inspect_reply(
            _inspect_reply(request_id, status="complete", finalOutput="42", messages=[])
        )
    )

    assert [(fact["kind"], fact.get("event")) for fact in facts] == [
        ("message", None),
        ("lifecycle", "closed"),
    ]
    assert facts[0]["content"][0]["text"] == "42"
    assert facts[1]["engineStatus"] == "complete"
    # The close came from the read itself; it owes no further read.
    assert projector.inspect_requests() == []


def test_a_reply_nobody_asked_for_is_dropped() -> None:
    """The package tells hosts to "drop unmatched replies"."""

    projector = PiChildResources()
    _opened(projector)

    assert projector.observe_inspect_reply(
        _inspect_reply("someone-elses", status="running", messages=[])
    ) == []


def test_a_run_the_package_no_longer_holds_is_not_an_error() -> None:
    projector = PiChildResources()
    request_id, _ = _opened(projector)

    assert projector.observe_inspect_reply(
        _inspect_reply(request_id, error={"code": "stale", "message": "cleaned up"})
    ) == []


def test_a_malformed_request_is_reported_loudly() -> None:
    projector = PiChildResources()
    request_id, _ = _opened(projector)

    with pytest.raises(PiChildRunError, match="invalid_request"):
        projector.observe_inspect_reply(
            _inspect_reply(request_id, error={"code": "invalid_request", "message": "bad id"})
        )


def test_the_retracting_update_is_not_a_reply() -> None:
    from astrabox.core.service.orchestrator.engine.pi_child_runs import inspect_reply

    assert inspect_reply(
        {"type": "extension_ui_request", "method": "setWidget", "widgetKey": "subagent-inspect", "widgetLines": []}
    ) is None
