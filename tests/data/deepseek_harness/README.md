# DeepSeek Harness golden streams

One recording of the vendor's product, used as contract material rather than
as a snapshot to diff against.

## `product-two-turns.mux.jsonl`

The `events.mux` downlink of a real `dsh --profile web` server
(`@deepseek-ai/dsh@0.1.0-rc.6`) answering
two real prompts against a real model: a text reply, then a `write` tool call
that created a file. 147 frames, verbatim, with the session id replaced by
`{{sessionId}}`.

Recorded rather than written because two facts the client depends on are only
true of the running product and are not visible in its schemas:

* the harness echoes the caller's own prompt `rpcId` into the session log as
  the enqueued message's `source` — the consumption boundary's only hard
  identity, since `session.prompt` answers with no receipt;
* a turn emits a **second** `user/message`, written by a plugin's context
  snapshot, which is what makes "the first user message" the wrong anchor.

Re-record by driving a live server, not by editing this file.

## Current schema test input

The shared `tests/deepseek_harness_fixtures.py` loader moves
recorded `assistant/chunk` payloads into their named `assistant/message`
settlement's `stream`, using the raw `{type: "chunk", time, chunk}` member of
`@deepseek-ai/dsh-llm@0.1.5-rc.2`'s `AssistantStreamRecord` union
(`lib/types/assistant-stream.d.ts`). Every original chunk is matched against
`sourceEventSeqs` in order before that retired field is removed. Each collection
checks one complete root turn; the subagent scene retains the root's spawn tool
call and result. That snapshot redacts parent and child ids to the same
placeholder; its explicit `subagent.started`/`subagent.finished` notifications
bracket the contiguous child scene, allowing the loader to restore a distinct
fixture address before selecting the root turn. Child catalog/follow behavior
has separate client tests.

The client tests use that conversion. This exercises
the current client's durable settlement translation with recorded
content. It is not a capture of the current live assistant-stream transport and
does not establish real-deployment acceptance of the supplier upgrade.
