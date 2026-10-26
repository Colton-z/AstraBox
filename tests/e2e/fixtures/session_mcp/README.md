# Session MCP activation fixture

This isolated provider supplies a private identity file and an environment
pointer using the public runtime preparation seam. Its private token map binds
two synthetic platform users through the public identity seam. It contains no
external service endpoints or customer account configuration.

Bake the read-only stdio service and the configured Plugin's package cache with
this directory's Dockerfile over the candidate Claude image. Supply
`PLUGIN_SHA`, `PLUGIN_CACHE_TARGET` and `PLUGIN_CACHE_HASH` from the platform's
existing package checkout/hash helpers. The Plugin commit uses the fixture
files, author `Fixture <fixture@example.invalid>`, message `Read-only identity
fixture`, and author/committer date `2026-10-26T00:00:00Z`. Configure the canonical
Agent with that exact package descriptor; the selected case copies it through
the ordinary Agent package API. Each isolated account then receives the
platform's normal private Plugin link. No build-user home is shared.

Install this Python package only
in the isolated candidate server. Configure the server's web identity as
`fixture`, with `/data/session-mcp-fixture/identities.private.json` containing
task-owned token, user_id and roles records. The test toggles its own blocked
user list in that same private state directory.

Select only
`tests/e2e/session_mcp_activation_test.py::test_user_bound_mcp_activates_in_the_prepared_process_and_recovers`
with the normal 180-second budget. Supply both token files, the explicit
server/PostgreSQL handles and a Claude agent-tenancy profile on the candidate
fixture image, with no persistent workspace volume. No business model calls
belong in fixture setup. Preserve source-bound native process, SessionStore,
connection and model receipts before retiring task-owned resources.
