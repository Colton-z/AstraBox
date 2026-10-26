# Session MCP activation fixture

This isolated provider supplies a private identity file and an environment
pointer using the public runtime preparation seam. Its private token map binds
two synthetic platform users through the public identity seam. It contains no
external service endpoints or customer account configuration.

Bake the read-only stdio service and installed Plugin with this directory's
Dockerfile over the candidate Claude image. Install this Python package only
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
