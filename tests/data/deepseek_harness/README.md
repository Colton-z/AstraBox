# DeepSeek Harness golden streams

Two recordings, from two different points in the vendor's product, both used
as contract material rather than as snapshots to diff against.

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

## `*.notifications.jsonl`

Vendored verbatim from `deepseek-ai/deepseek-harness` (MIT), commit
`47f943859bef60e4160492346772ded9b24f765a`, path
`examples/jsonrpc-agent/tests/snapshots/<case>/notifications.expected.jsonl`.

The vendor's own byte-exact snapshots of the same session-log events, taken
from their SDK example runtime. They predate the product wire and carry no
prompt `rpcId`, so they are contract material for the **translator** only —
which reads events, not envelopes. Placeholders (`{{sessionId}}`,
`{{system}}`, `{{tools}}`, `{{cwd}}`) are substituted by the test loader,
never edited here. Refresh by re-copying from the pinned vendor commit.

## Current schema test input

The shared `tests/deepseek_harness_fixtures.py` loader retains these files unchanged and moves
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

The client, translator and UI-schema tests use that same conversion. This exercises
the current client's durable settlement translation with recorded
content. It is not a capture of the current live assistant-stream transport and
does not establish real-deployment acceptance of the supplier upgrade.
