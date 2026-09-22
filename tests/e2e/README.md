# Python live E2E execution contract

These tests drive a real deployment: a live backend, a real sandbox, a real
model. They carry the `e2e` mark and are deselected by default, so a plain
`pytest` run never starts one.

`scripts/e2e_live.sh` is the entry point. Point it at a deployment that is
already running and let it select the lane:

```bash
ASTRABOX_E2E_BASE_URL=http://127.0.0.1:8088 ./scripts/e2e_live.sh
```

It stops at the first failed report and cleans up nothing, so the failing
scene — the sandbox, its logs, the deployment state — is there when you look.
A selected pass is not lane evidence; the lane is what the two phases below
finish.

The runner finishes two phases in order:

The canonical PostgreSQL + Agent lane excludes the deployment-specific marker
`assistant_live`, then runs:

1. `e2e and not backend_restart` across xdist workers with `loadgroup`.
2. `e2e and backend_restart` only after every parallel worker has exited, without
   xdist.

An Assistant/Hermes contract carries `assistant_live` and runs against its
actual deployment. It must not skip inside the Agent lane or be counted as one
of that lane's passes.

The frozen node inventory, engine matrix, and storage partitions live in
[suite-contract.json](../../tests/e2e-contract/suite-contract.json).
`main` runs ordinary and restart tests for each configured Agent program;
`assistant` is a separate serial lane. `E2E_LANE` selects one of them for a
direct diagnostic:

```bash
E2E_LANE=main ./scripts/e2e_live.sh
E2E_LANE=assistant \
  ASTRABOX_E2E_ASSISTANT_ENVIRONMENT=astrabox-e2e-assistant \
  ASTRABOX_E2E_ASSISTANT_IMAGE=<exact-hermes-image> \
  ASTRABOX_E2E_ASSISTANT_MODEL=<configured-openai-chat-model> \
  ./scripts/e2e_live.sh
```

For a direct diagnostic after a fix, select exact nodes instead of rerunning
the complete lane:

```bash
./scripts/e2e_live.sh \
  --parallel-node tests/e2e/test_delete_and_archive.py::test_delete_makes_session_unreadable
```

Use `--restart-node` for a backend-restart test. Repeated node arguments retain
the ordinary/restart separation. Preserve failed reports and sandbox state
before another invocation; a targeted pass is not complete-lane evidence.

Any test that stops or restarts the shared backend, bundled sandbox server, or
database must carry both `backend_restart` and
`xdist_group("backend-restart")`. The first marker moves it into the exclusive
runner phase. The second keeps restart tests together in targeted xdist runs;
the top-level conftest rejects any other scheduler whenever a live E2E
invocation uses workers, so callers cannot silently select one that ignores the
group. The canonical runner supplies `--dist loadgroup` itself.

`loadgroup` alone is not an exclusive lock: other workers may still run
unmarked lifecycle operations. Therefore a bare full-suite
`pytest -m e2e -n ...` invocation does not satisfy this contract even though
the group itself is serialized. Use the runner for the full lane, or select
only the `backend_restart` phase and run it without `-n`.

Markers are strict repository-wide. A misspelled or unregistered isolation
marker is a collection error rather than a silently parallel test.
