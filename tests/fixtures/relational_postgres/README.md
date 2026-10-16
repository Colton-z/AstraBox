# Live relational contract binding

This external entry point uses the real built-in PostgreSQL implementation. It
proves external selection and the shared input/reader/follower journey on
PostgreSQL, not support for other databases or every repository.

After the owner supplies a disposable PostgreSQL server and releases execution:

```bash
.venv/bin/pip install --no-deps tests/fixtures/relational_postgres
# Set ASTRABOX_TEST_POSTGRES_URL privately to that server's administrative role.
.venv/bin/python -m pytest -m e2e -q -rA -s \
  tests/relational_persistence_live_test.py::TestSuppliedRelationalPersistence::test_transactional_wakeup_and_reader_handoff
```

The role needs database/role creation, grants, connection termination and owned
database/role deletion. The fixture creates random database and login-role names;
it never uses the candidate database. Each process has its own role/connections.
The independent observer uses psycopg, while the selected provider uses asyncpg.
On failure, connections close and only that test's database/roles are retained;
its private credential receipt is printed by path, not by contents. Success drops
only the resources this fixture created.

The gap sets **only the replica's newly created role** NOLOGIN, terminates its
single `astrabox-feed` connection inside the owned database, commits through an
existing SQL connection, then restores LOGIN in `finally`. NOLOGIN controls new
sessions; it does not disconnect existing ones. Administrative termination is
restricted by database, role and observed backend PID. See PostgreSQL's
[role attributes](https://www.postgresql.org/docs/17/role-attributes.html) and
[backend signaling](https://www.postgresql.org/docs/17/functions-admin.html#FUNCTIONS-ADMIN-SIGNAL).

Downstream binds the same `RelationalPersistenceContractSuite` with a
`relational_store` fixture and implements `NativePersistenceObservation` against
its actual database/coordination. Native snapshots, subscription readiness, lock
contention/loss and a held listener gap are required, not mocked callbacks or
optional skips. A missing backend or observation cannot pass this journey.
There is no private SDK requirement. A MySQL provider may use SQLAlchemy's
maintained asyncmy/aiomysql dialect, but must also supply its real coordination
and the shared SQL/schema support before running this same positive journey.
