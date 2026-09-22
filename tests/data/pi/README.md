# Pi RPC wire recordings

Every line in these files was written by pi itself. They are the verbatim
stdout of `pi --mode rpc` (`@earendil-works/pi-coding-agent` **0.84.2**),
recorded against a local fake OpenAI-compatible endpoint so the model's
answers are scripted while every RPC record remains the vendor's own.

| File | What it holds |
|---|---|
| `text-reply.jsonl` | One prompt answered with streamed text. One pi turn. |
| `tool-call.jsonl` | One prompt answered by calling `bash`, then a text reply. **Two** pi turns in one platform turn. |
| `testbed-real-model.jsonl` | The same shape, recorded **inside the pi sandbox image on the AWS testbed** with a real model answering through the deployment's gateway. |

The last one is not redundant. The other two script the model's replies, so
they prove how pi frames a stream but not that the terminal this adapter waits
for arrives on the real path — a different model, a real gateway, a real box.
It was recorded by driving `pi --mode rpc` inside a running
`astrabox/sandbox-pi` container against the testbed's LiteLLM route.

Reproduce with `scripts/record_pi_rpc.py` (see its header). The recorder
starts pi with `--no-extensions --no-skills --no-context-files` and a
`models.json` naming a fake provider, so the recording contains no
environment-specific content.

## Why the version is pinned here

`npm install @earendil-works/pi-coding-agent` does **not** always install
0.84.x. The package publishes a second dist-tag, `legacy-node20`, and 0.84.2
declares `engines.node >= 22.19.0`; on an older Node, npm silently resolves
the older 0.74.2 instead of failing.

That difference is not cosmetic. **0.74.2 never emits `agent_settled`** — the
event this adapter treats as the end of a turn. An image built on a Node older
than 22.19.0 therefore installs an engine whose turns never terminate, with no
error at build time and no error at run time. The first recording made here
was against 0.74.2 and hung for exactly that reason.

So the image pins the exact version, and the Node it builds on must satisfy
that engine range.
