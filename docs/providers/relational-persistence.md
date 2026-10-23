# Relational persistence providers

A relational provider supplies physical database services through SQLAlchemy.
AstraBox keeps its repository statements, business predicates and transaction
workflows in the platform. A provider does not replace repositories or interpret
runtime Facts.

The built-in `postgres` provider uses PostgreSQL and asyncpg. External provider
selection and the reusable MySQL statement component do not by themselves
establish a supported or complete backend. Validate the actual database,
dialect, schema and coordination implementation before claiming support.

## Installation boundary {#installation-boundary}

One selected provider serves one Environment's definitions, authorization,
Sessions, complete native state and runtime records through the same repository
engine and transaction contract. Joins, foreign keys and atomic workflows stay
inside that database. Another Environment uses a separate business database,
role and credentials; shared schema names or query filters are not a substitute
for that access boundary. Database schema initialization refuses a different
persisted Environment owner before applying an upgrade.

Runtime generations, pools, quota records, Sessions, Assistant workspaces and
Deployments retain their Environment ownership. Providers preserve complete
keys and foreign keys. All replicas follow the [Environment deployment
contract](/docs/deploy#environments). A provider must enforce the configured
Environment binding; an unscoped or unsupported backend is refused.

## Register and select a provider

Ship the provider in a separate Python distribution and register its class:

```toml
[project.entry-points."astrabox.providers.persistence"]
example = "example_storage:ExamplePersistence"
```

Set `ASTRABOX_PERSISTENCE_PROVIDER=example` and set `ASTRABOX_DB_URL` to that
provider's native URL. The provider class accepts the keyword argument
`database_url: str` and `environment_name: str`. AstraBox loads only the selected persistence entry point;
unknown names and missing required operations fail before schema initialization.
No provider is substituted silently.

Implement
[`RelationalPersistenceProvider`](../../astrabox/seams/relational_persistence.py):

| Member | Responsibility |
|---|---|
| `environment_name` | Immutable Environment identity bound to this provider. |
| `database_url` | Native configured URL, normalized by the provider. |
| `get_engine()` | The same SQLAlchemy `AsyncEngine` for a given event loop and configuration. Connections passed to shared recipes come from this engine. |
| `statements` | The three native conflict operations described below. |
| `coordination` | Runtime ownership, commit-bound notifications and listening/resynchronization. |
| `initialize_schema()` | Initialize this build's schema while serializing concurrent replicas. |
| `translate_error(error)` | Classify native integrity failures with their actual constraint identity; return unrecognized errors unchanged. |
| `pool_report(holders=5)` | Diagnostics without opening a connection or exposing credentials. |
| `dispose()` | Release this loop's provider resources after their users stop. |

Use a maintained SQLAlchemy async dialect where it meets the database's actual
contract. Driver dependencies and engine/pool setup belong to the provider.
Coordination owns the capacity and lifetime resources its guarantees require;
there is no public engine-pool selection flag. Returning a new engine on every
call violates the stable-engine contract.

## Native conflict operations

`RelationalStatements` contains only these three operations. Existing shared
functions in `astrabox.persistence.sql.statements` validate target/value contracts
and delegate to the selected provider's `statements` member.

- `upsert_primary_key(connection, table, rows, *, update_columns, update_values=None)`
  constructs an actual SQLAlchemy `Insert` targeting the primary key. Other
  unique violations must retain their meaning. Updated values remain ordinary
  SQLAlchemy values or expressions.
- `insert_if_absent_primary_key(connection, table, rows)` constructs an insert or
  native no-op conflict operation for a table with no competing unique keys.
  It makes no inserted/existing outcome or rowcount claim. The caller executes
  it before taking its own locking read.
- `await insert_if_absent(connection, table, values, *, index_columns, where=None)`
  executes on the caller's existing transaction. It returns `True` for insertion,
  or `False` for no eligible row or a proven conflict on the exact requested
  target. A supplied SQLAlchemy `where` expression stays inside the insert;
  the implementation must not replace it with a separate eligibility prelock.

Shared validation accepts plain unconditional unique column targets and concrete
target values. NULL-distinct semantics remain intact. Partial, functional,
prefix and NULLS-NOT-DISTINCT targets require a separately reviewed recipe.
Finding a row at the requested target is insufficient evidence for suppressing
an integrity error: the actual violated constraint must match. Unknown or
unrelated violations propagate. Duplicate readback is not exclusive ownership.

`PostgresStatements` and `MySQLStatements` in
`astrabox.providers.relational_statements` are reusable physical implementations.
The MySQL component refuses PK upserts when another unique constraint or unique
index could broaden the conflict target. Its outcome-bearing insert uses a
savepoint and exact-constraint confirmation; its PK-only no-op upsert avoids
shared-lock upgrades when the caller needs subsequent write exclusion.
Neither component supplies a schema, driver, scheduler or coordination service.

An independently named SQLAlchemy dialect can supply its own `RelationalStatements`
without changing the shared helpers. Types and expression-only differences,
including `TimestampAfter`, use SQLAlchemy's existing dialect compilation
extensions. Do not introduce another query language or database executor.

## Transaction and coordination guarantees

Every operation uses the supplied actual `AsyncConnection`. Providers must not
commit it, switch databases, retry the business callback or treat a lost COMMIT
response as proof of rollback. Shared recipes retain their atomic predicates,
input deduplication, reader fences and complete native SessionStore custody.

`coordination.notify(connection, notice)` enlists notification intent before
COMMIT on that same connection. Rollback must not publish committed-state
notifications; service-level publish-after-commit creates a crash gap and is
not equivalent. Listening reports readiness only after subscription is active
and reports reconnect gaps so consumers reread durable state. Runtime ownership
must preserve exclusion, detected-loss behavior and the yielded ownership check;
a successful ping or an engine alone does not establish those guarantees.

`coordination.startup_lock(session_id, wait=False)` must raise `RuntimeLockBusy`
immediately if another holder owns the lock. The ordinary call waits. Startup
and recovery tasks hold this same lock while driving their Session; the periodic
startup sweep skips live holders and ends abandoned claims after their grace
period, including starts that never acquired a sandbox allocation. A provider
must support both modes with the same ownership boundary. The built-in provider
uses PostgreSQL transaction advisory locks and their native nonblocking form.
The startup lock has a separate key from `runtime_lock`: accepting an input
must remain possible while its Session starts.

Schema/type mappings, constraints, isolation and database time must support the
shared recipes. The built-in DBOS scheduler separately requires PostgreSQL;
selecting a persistence provider does not make that scheduler portable. Driver
or compiler acceptance alone is not end-to-end backend acceptance.
