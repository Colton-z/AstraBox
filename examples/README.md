# Examples

Runnable scripts against a deployment you already started
([Quickstart](../docs/quickstart.md)). They use `httpx` and nothing else.

| Script | What it shows |
|---|---|
| [`ask_agent.py`](ask_agent.py) | Start a Session on an Agent, send a message, and print the reply as it streams. |

```bash
pip install httpx
python examples/ask_agent.py "What is 1 + 1?"
```

Progress goes to stderr and the Agent's answer to stdout, so the answer pipes
cleanly into another command:

```console
$ python examples/ask_agent.py "What is 1 + 1? Reply with just the number."
agent 711a02bf-950f-4ca0-a7d1-1be52539123f -> session 0d330936-d743-4518-b744-a4e4ba32051a
turn 42278a96-2034-4dd5-94ee-ab33783a267a accepted
2
```

Set `ASTRABOX_BASE_URL` when the deployment is not on
`http://127.0.0.1:8088`, and `ASTRABOX_AGENT_ID` to choose an Agent other than
the first one the deployment lists.

## Sending and receiving are two calls

The shape these scripts follow is worth copying, because it is what makes a
client survive a dropped connection.

`POST /api/v1/sessions/{id}/turn-inputs` submits the message and answers with a
receipt naming the `turn_id`. It does not carry the reply. The reply arrives on
the Session's own stream:

```http
GET /api/v1/sessions/{id}/ai-stream?follow=session
```

That stream sends frames from `after_seq` and then holds the connection open
for later ones, and an idle Session holds it rather than closing. Two things
follow. Opening the stream *after* the POST loses nothing, because the stream
replays what was already produced — which is why the script can submit first
and connect second. And the stream reports its own position in a
`data-resume-cursor` frame carrying `frameSeq`, so a client that drops
mid-answer reconnects with `after_seq` set to the last value it saw instead of
resending the message and paying for a second turn.

A client that keeps one request open for both — sending a message and reading
its answer down the same connection — has no way to do either.

The routes each script calls are described in [HTTP API](../docs/api.md).
