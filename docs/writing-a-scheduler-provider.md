# Add a scheduler

A `SchedulerProvider` runs scheduled Deployments. It fires each due time, gives
every invocation a durable Run ID, calls AstraBox back to start the Run's
Session and admit its input, retries and recovers invocations, and keeps the Run history
that the console lists and replays.

AstraBox includes one provider, `dbos`. Worker processes embed DBOS Transact;
control-only processes use its native client without launching workflows. Both
use the PostgreSQL database AstraBox already uses. Write another provider when
an installation needs a different durable engine.

## What AstraBox and the provider each own

| AstraBox | The provider |
| --- | --- |
| The Deployment record: name, prompt, cron expression, IANA timezone, and enabled flag | Firing each due time in the schedule's timezone |
| Who may create, change, run, or replay a Schedule | One durable Run ID per due time, across every replica |
| Starting a Run's Session and admitting its input, idempotently by the Run ID | Calling AstraBox back until the Run succeeds or fails permanently |
| The public Run status: the Session's response to the Run's input, else the provider's dispatch state | Retries, recovery of interrupted invocations, and the Run ledger |

The Deployment record stays the desired state. When AstraBox starts, it passes
each worker the stored Schedules belonging to its Environment (`reconcile`);
a control-only process does not reconcile worker state. After a change, it
passes the one Schedule that changed (`apply` or `delete`). The provider stores the
payload AstraBox gives it — the target and the prompt — and hands it back with
every invocation, so editing a Schedule never rewrites a Run that already
exists.

## Guarantees the provider makes

1. **Due times.** Compute each due time from the five-field cron expression in
   the Schedule's IANA timezone. Skip a time that passes while no AstraBox
   process is running or while the Schedule is disabled; never backfill it or
   merge it into a later Run. Each due time is its own Run, even while an
   earlier one is still running.
2. **One Run per due time.** One due time of one Deployment produces exactly
   one Run across every replica. Enforce this with a uniqueness constraint in
   the provider's store, not with a read before the write.
3. **Dispatch.** For each Run, call `RunHandler.start_session`, store the
   Session ID it returns, then call `RunHandler.admit_input` and store the
   input ID of the `RunInputBinding` it returns as the Run's `input_id`.
   Either call may be repeated with the same `RunInvocation`; AstraBox makes
   both idempotent on the Run ID. Retry other failures with bounded backoff.
   Record the Run as `FAILED` on `RunPermanentError` or when retries run out.
4. **Recovery.** A Run interrupted by a lost process continues under the same
   Run ID, with the Session ID it already stored.
5. **Disable.** After `apply` returns with a disabled definition, no new
   scheduled Run starts later than the provider's
   `pause_visibility_seconds`. Runs already started continue.
6. **Run now and Replay.** `enqueue` returns once the Run is stored, and does
   not move the calendar.
7. **History.** `list_runs` returns a Deployment's Runs of every trigger,
   newest first.
8. **Failures.** Raise `SchedulerUnavailable` when the store or the engine
   fails, `InvalidSchedule` when the engine rejects an expression, and
   `ScheduleCapacityExceeded` past the engine's ceiling. The engine's own
   vocabulary and error text never reach an API response.

The full contract, with every type, is in `astrabox/seams/scheduler.py`.

## Declare the engine's properties

Engines legitimately differ in three ways. Declare them in
`SchedulerProperties`; AstraBox logs them at startup.

| Property | Meaning | `dbos` |
| --- | --- | --- |
| `recovery_scope` | `"cluster"` when any replica resumes a Run a lost process left behind; `"executor"` when only a process restarted with the same identity does. | `"executor"` |
| `max_active_schedules` | The most enabled Schedules the engine supports, or `None`. `apply` refuses past it. | `64` |
| `pause_visibility_seconds` | How long a disabled Schedule may still fire. | `11` |

## Check the deployment's configuration

AstraBox calls `configure(database_url=..., environment_name=...)` at startup.
The URL is the selected relational backend's native URL; the provider validates
whether it can use that store. `environment_name` is the stable namespace whose
work this process may execute. Raise `SchedulerConfigurationError` for an
unsupported configuration before any invocation can start.

`start(handler)` opens control operations and starts the Environment's worker.
`start(None)` opens control operations without firing schedules, claiming
invocations or recovering workflows. `stop()` drains the worker and closes
clients while AstraBox's services are still available.

`ScheduleDefinition.environment_name` and `RunInvocation.environment_name`
carry durable ownership. Control methods `delete`, `enqueue`, `get_run` and
`list_runs` take an explicit Environment and refuse a foreign one. A process
can manage its local bindings without running their work. Reconciliation is
limited to that same Environment. The platform validates ownership again
before starting a Session or admitting a Run input.

## Use the engine's complete execution capability

Use a maintained durable engine's supported routing, queueing and recovery
APIs. The built-in adapter maps Environments to DBOS applications and named
native queues; all control operations use `DBOSClient`. A control-only process
initializes its local native schema through the pinned DBOS CLI migration
entry point, then constructs the client without launching a workflow worker.
DBOS application names accept 3–30 lowercase letters, digits, underscores and
hyphens. The default Environment `astrabox` retains its native application and queue
names. See the fixed dependency's [client interface](https://github.com/dbos-inc/dbos-transact-py/blob/a51254c6c5ab2f1f217444778d306d4c967b1ba8/dbos/_client.py).

Keep generic scheduling machinery in that engine. Document an unmet requirement
and inspect maintained counterparts before introducing custom leases, retry
queues, invocation state or recovery.

## Package the provider

Declare the provider in its Python distribution:

```toml
[project.entry-points."astrabox.providers.scheduler"]
example = "astrabox_scheduler_example:ExampleSchedulerProvider"
```

Set the class's `name` to the entry-point name. A deployment selects it with
`ASTRABOX_SCHEDULER_PROVIDER=example`; `none` is reserved for running without
scheduled Deployments. AstraBox instantiates and registers the class when it
starts. The class may also set `seams_api_version`; an incompatible declared
version fails at startup instead of at the first due time. The target-aware
scheduler contract requires provider API9.

## Verify the provider

Bind the reusable contract suite in the provider's tests:

```python
from astrabox.testing.scheduler_conformance import SchedulerProviderContractSuite

from astrabox_scheduler_example import ExampleSchedulerProvider


class TestExampleScheduler(SchedulerProviderContractSuite):
    def make_provider(self, store):
        provider = ExampleSchedulerProvider()
        provider.configure(database_url=..., environment_name="astrabox")  # fresh `store`
        return provider

    async def fire_due_times(self, provider, deployment_id, start, end):
        ...  # fire every due time in (start, end) the way the engine does
```

The suite runs in one process. It checks dispatch through the handler,
permanent and transient failures, the Run history, the payload a Run captures,
Replay provenance, one Run when the same due time fires twice, and Schedule
removal. Two guarantees need more than one process — another replica firing
the same due time, and a Run resumed after its process died — so prove them on
a multi-replica deployment of the provider's store.

Then follow a Schedule through a real deployment: create it, let a due time
fire, select **Run now** and **Replay**, disable it, and delete it.

## Related

- [Schedule tasks](schedules.md) — Schedules from the user's side
- [Automate Agent runs](deployments.md) — Deployments and their Runs
- [Embed AstraBox](embedding.md) — entry points and startup
