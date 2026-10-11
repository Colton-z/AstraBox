# Claude Code journal recordings

Every row in these files was written by the platform's own journal writers
during a real Claude Code conversation on a deployment. The rows are verbatim
`session_events` documents; nothing in them was typed by hand.

| File | What it holds |
|---|---|
| `background-child-reattach.json` | One background Agent launched before a server restart and completed after it. `engine.message` rows are the in-box runner's serialization of the SDK's typed task messages, persisted when the restarted server attached to the runner; `turn.background_tasks_materialized` is recovery's projection of the same child from the SessionStore mirror. |

The rows keep the `event_seq` they were recorded with. A test that needs the
journal in another order assigns new sequence numbers in the test and states
which order it reproduces.
