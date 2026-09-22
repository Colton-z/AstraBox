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
| `an-expired-oauth-credential-is-reported-before-a-conversation-dies-on-it` | preflight refuses unless the cold Environment's engine equals the matrix Agent's; red on three of four engine rounds by construction | a per-engine cold conversation-tenancy Environment on the lane deployment (the record surface itself landed in 5d23cb8c) |
| `idle-parked-conversation-serves-its-workspace-and-its-next-message` | the Files panel of a parked conversation lists nothing | `SessionFileService._resolve_session_context` taking the same wake the turn path takes (`_wake_parked_sandbox`), shared rather than copied |
| `background-launcher-lost-before-its-terminal-still-materializes` | no `turn.background_tasks_opened` is journaled for a launcher lost before its terminal | journaling the manifest where the adapter reports it (bridge_loop.py), with an answer for the no-parent-message hold in `_materialize_background_continuation_event` |
| `diff-tab-answers-from-durable-history-not-the-loaded-window` | the reloaded Diff tab reads `Diff (1)` for a conversation that changed two files | a session-scoped durable file-changes read (`GET /sessions/{id}/file-changes`) feeding `useSessionRightPanelState` instead of the 50-record window |
| `a-second-conversation-opened-seconds-later-is-sendable-as-fast-as-the-first` | the second conversation is not sendable inside the prepared budget | prepared capacity deeper than one slot per Agent — a design decision, not a fix |
| `untouched-tab-catches-up-after-backend-restart` | no ok `/ai-stream` response after the restart | the reopen decision deriving "busy" from whether a subscription task owns the session rather than from the render status (`useSessionChat.ts`, `turnStream.ts`), confirmed on the testbed first |
| `deploy-during-conversation-create-reaches-a-verdict` | the header stays CREATING past 60s | converging a CREATING row abandoned by a restart on "the owning worker is gone" rather than the 300s boot grace — a design question against "the runtime judges its own turn" |
| `incident-record-follows-its-session-or-states-its-read-time` | the verdict poll settles on `stale`: a second incident lands on an open session record and the page neither shows it nor says when it was read | a session record that keeps reading while its session is busy, or discloses its read time. READY is in `RESTING_SESSION_STATES` (SessionDetailPage.tsx:77) so `follow` goes false (`:116-118`), and READY is also what the row reads for the whole of a turn — the alternative the spec accepts is a read-time line plus a Refresh on the record itself, which no `ConsoleRecordPage` consumer has |
| `agent-page-reports-warm-capacity-not-a-stale-promise` | the Agent page issues no read of `/agents/{id}/prepared-runtime` | a prewarm readout on the Agent record page (the route exists; no console surface reads it — audit console-08) |
| `older-history-loads-without-a-wheel` | the 26 real turns that push the transcript past its 50-record first page do not fit the 180s wall on the lane's model (timed out at 180s in e0526) | an honest way to give a conversation more than one durable page inside the wall — a producer-backed seeding helper for session_events, or a first-page size the lane can lower; the keyboard journey itself is aa3e1960 |
| `an-assistant-used-today-keeps-its-box-overnight` | the morning turn sent right after the runtime eviction is refused with 409 (e0528) | the spec re-admitting the session after `adminEvictRuntime` before it sends, or the eviction path leaving the session sendable; the renewal horizon itself is 5d23cb8c and unit-covered |
| `background-result-outlives-a-newer-manifest-backlog` | the model answered inline twice instead of launching a background Agent, so no manifest existed to bury (e0530) | a launch the lane's model performs reliably (the sibling background-agent-writes spec's prompt does), or a seeded open manifest; the backlog scan itself is b0bee7a4 and unit-covered |
| `background-work-keeps-its-box-off-the-idle-sweep` | the header never read BACKGROUND_RUNNING after the launch prompt on the lane's model (e0534) | a background launch the lane's model performs reliably, held open long enough to outlast the idle window; the sweep's open-manifest check is b0bee7a4 and unit-covered |
| `prewarm-readout-agrees-with-what-a-claim-does` | `/agents/{id}/prepared-runtime` reports `ready` over a manifest whose runtime generation a claim refuses | the readout comparing the manifest's generation the way `claim_prepared_slot` does (agent_service.py `_prepared_runtime_status`) |
| `dead-agent-box-does-not-leave-a-claimable-prepared-slot` | a fresh manifest keeps naming a box that is gone; the claim hits SANDBOX_GONE with no cold-start fallback | the sweeps that record a box's death withdrawing the manifest that names it, and the claim path probing liveness or falling back to a cold start |
| `idle-park-refuses-a-box-holding-a-prepared-slot` | the idle park parks a shared box whose only other occupant is a prepared slot | `_box_is_this_session_s_alone` (expiration_watcher.py) asking the same four occupancy questions the destroy path asks in `agent_box_has_other_occupants` |
| `a-prepared-seat-is-not-a-promise-the-box-can-still-keep` | a claim succeeds on a seat whose box has no room left for it | memory admission (`_has_room`, shared_sandbox_lease.py) asked at claim time as well as at build time; note the spec fills a shared box to its refusal line and must stay serial |
