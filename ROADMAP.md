# Roadmap

What's coming, in the order the pain is felt. Items move when reality
disagrees with the plan; this file is honest, not contractual. Pre-1.0,
stored schemas may still change between releases.

## Near term (weeks)

- **Notifications** — a webhook/ping when an approval is waiting or a long
  task finishes. Today you find out by looking; that's the first thing
  everyone asks for after their first background run.
- **Claude subscription tokens** — blocked on LiteLLM's OAuth passthrough
  ([BerriAI/litellm#19618](https://github.com/BerriAI/litellm/issues/19618));
  lands here once that fix ships and verifies against a real subscription.

## Mid term (months)

- **A second engine** — the engine seam already models it (capability
  flags, per-engine translator); Codex is the natural candidate. "Bring your
  own agent CLI" should be demonstrably true, not architecturally true.
- **Repository credentials into the vault** — deploy keys currently resolve
  from the server environment; they belong in the console-managed vault with
  the other secrets.
- **Schema migrations** — a real mechanism before stored shapes change
  under anyone's feet.
- **Crash-matrix CI lane** — the recovery scenarios (host restart, box
  death mid-turn, pending approval across death) as a public, visible gate.
- **Plugin cookbook** — every seam (sandbox, engine, model gateway, secret
  store, channel, identity, persistence) is an entry-point group with a
  conformance suite; each deserves a "build one in an afternoon" walkthrough.

## Explicitly not planned

- **A hosted service.** This repository is the self-hosted runtime.
- **Our own agent loop.** The vendor CLI runs the turn; that is the point.
- **Our own password store.** Identity comes from an IdP (bundled Casdoor or
  yours); credentials-at-rest live in the vault.
