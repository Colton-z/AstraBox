# Held browser specs

Specs in this directory are written against confirmed product findings that
the product does not yet satisfy. They are typechecked with the suite
(`tsconfig.json` includes `held/`), and they are not collected by any lane:
the lanes collect `specs/*.parallel.spec.ts` and `specs/*.exclusive.spec.ts`
only. A spec moves to `specs/` in the same change as the product work that
turns it green, with its `suite-contract.json` rows.

Each entry names the assertion that is red on the current tree and the
product change it waits for.

| Spec | Red assertion | Waits for |
|---|---|---|
| `an-exposed-port-outlives-the-idle-window` | the exposed page is unreachable after the idle sweep parks the box | `expose_port_service.expose_port` recording the exposure, and `_is_idle_past` (expiration_watcher.py) taking the later of the snapshot clock and the newest exposure |
| `a-second-conversation-opened-seconds-later-is-sendable-as-fast-as-the-first` | the second conversation is not sendable inside the prepared budget | prepared capacity deeper than one slot per Agent — a design decision, not a fix |
| `deploy-during-conversation-create-reaches-a-verdict` | the header stays CREATING past 60s | converging a CREATING row abandoned by a restart on "the owning worker is gone" rather than the 300s boot grace — a design question against "the runtime judges its own turn" |
