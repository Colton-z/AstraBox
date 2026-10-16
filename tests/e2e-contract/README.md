# Background work contracts

`background_subagent` covers native child launch, lifecycle projection and
control. `background_completion_response` separately declares that child
completion starts a native parent response after its previous reply ended.
An engine may support the former without supporting the latter. The automatic
response E2E is N/A only when that specific supplier capability is false; the
child projection/control cases still run.

Codex's image pins CLI 0.160.0. The deployed `/opt/codex/node_modules/.bin/codex`
reported that exact version, and the failed native scene contained a completed
child but no second root turn. Official tag `rust-v0.160.0` resolves to
`a956835d020762cb2b570053af06f643a11c0ecc`:

- [`agent/control.rs`](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/core/src/agent/control.rs)
  records v1 completion through `inject_fragment_without_turn`; the v2
  completion sets `trigger_turn=false`.
- [`session/inject.rs`](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/core/src/session/inject.rs)
  injects into an active turn or records history while idle, without scheduling
  a new turn.

Thus Codex keeps `background_subagent=true` and declares
`background_completion_response=false`. The other profiles retain their
existing expected automatic-response behavior. Review this declaration against
the supplier implementation when changing the pinned CLI; do not manufacture
a platform turn or suppress ordinary child coverage to satisfy this case.
