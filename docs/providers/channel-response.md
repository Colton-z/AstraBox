# Channel response judges

A channel Deployment whose `attention_policy` is `participated` or `proactive`
asks a response judge whether the agent should answer an ordinary group
message nobody addressed to it. Core decides which messages are candidates,
freezes the policy with each message, collects the topic's context, records
the decision and recovers it; the judge answers one question for one message.

## Which messages are judged

A direct message, mention or reply to the agent is answered without a judge.
An ordinary message is judged as follows:

| Policy | Topic the agent has not answered | Topic it has answered |
|---|---|---|
| `participated` | Kept as context; the judge is not asked | Judged |
| `proactive` | Judged | Judged |

A topic is the message's conversation: its room or thread, or the reply chain
it continues. "Answered" means the agent answered a message of that topic
since the Deployment was last edited. The policy and instruction are frozen
with each message, so an edit does not change how a recorded message is
judged.

Each message is judged at most once. A message the judge declines creates no
conversation and stays context for the next message the agent answers. A
redelivered copy reads the recorded decision. A judge error is retried with
backoff and is never read as an answer. A copy of the same message that
mentions the agent makes it run even when the judge declines. A message a
platform reports only as context (an `observation` signal) is never judged:
the platform gave no permission to answer it.

## The built-in judge

Without an installed judge, AstraBox uses its own when the title model is
enabled (`ASTRABOX_TITLE_MODEL_ENABLED`). It sends the message, the topic's
last 50 unsubmitted messages (each cut to 2000 characters), whether the agent
already answered in the topic, and the Deployment's `response_instruction` to
the title model, and requires exactly `{"respond": boolean, "reason": string}`
back. Silence is the default; the instruction says when the agent should
speak. Any other output is an error, not an answer.

Set the policy and the instruction on the Deployment, in the console or the API:

```json
{"attention_policy": "proactive",
 "response_instruction": "Answer questions about deployments and incidents; stay silent otherwise."}
```

`response_instruction` (at most 4000 characters) applies only to these two
policies; saving it with another policy is refused, and changing the policy
away from them clears it. With neither a title model nor an installed judge,
saving either policy is refused with `CHANNEL_RESPONSE_JUDGE_UNAVAILABLE`.

## Installing a judge

A deployment installs at most one judge, replacing the built-in one:

```toml
[project.entry-points."astrabox.providers.channel_response"]
enterprise = "my_deployment.judge:ResponseJudge"
```

The target subclasses `astrabox.seams.channel_response.ChannelResponseJudge`
with a stable `name` and an async `judge(*, context)` that returns a
`ChannelResponseDecision(respond, reason)`.

| `ChannelResponseContext` field | Meaning |
| --- | --- |
| `deployment_id`, `channel_name` | The binding and its provider |
| `conversation` | The topic: the conversation key or reply chain core resolved |
| `message` | The candidate's participant, text and original timestamp |
| `policy`, `instruction`, `binding_revision` | Frozen when the message was first recorded |
| `selection_reason` | `participated_topic` or `proactive_policy` |
| `participated` | Whether a message of this topic and revision was already answered |
| `context` | The topic's unsubmitted messages before this one, oldest first |

The same message can be judged again after a host failure, so the judge must
be side-effect free: do not post, record, dispatch, or start a consent flow.
Raise when no decision can be made; core retries with backoff and never treats
an exception as `respond=true`. The reason is kept on the message's record and
must not contain secrets. A judge call is bounded at 30 seconds.
