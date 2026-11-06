# Satori capability fixture

Install this package only in an isolated candidate. It binds opaque delegated
credentials from `/data/satori-fixture.private.json` to execution principals and
stores synthetic personal grants in the candidate's shared PostgreSQL database.
No browser login or enterprise identity service is involved. Both server
replicas mount the same credential map; initialize `METADATA` in the candidate
DB before starting acceptance. The owner-controlled admin credential configures
the fixture. Employee-controlled operations affect only that employee's grant.

Run only `tests/e2e/satori_server_test.py::test_delegated_satori_chat_uses_owned_native_sessions_and_replays_across_replicas`.
Provide a private JSON plan through `ASTRABOX_E2E_SATORI_PLAN` with `agent_id`,
`bases` (two replica URLs), `tokens` (employee A/B), and `principals` (their ids).
Normal E2E environment variables identify the candidate DB/admin credential and
selected Claude profile. This fixture is not shipped as a production provider.
