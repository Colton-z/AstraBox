# AstraBox — browser e2e (Playwright)

End-to-end specs drive the real console (`frontend/`) against a live backend
and Agent sandbox. Set `ASTRABOX_E2E_BASE_URL` to use an existing deployment;
otherwise the config starts a source-mounted Compose stack and a Vite server.
The happy-path spec verifies a live model turn through the browser.

This suite is a narrower browser diagnostic, not the acceptance campaign in
`tests/e2e-ui/`; see [that suite's README](../tests/e2e-ui/README.md) for
exact-case selection against a full deployment.

## What it covers

| spec | flow |
|---|---|
| `specs/happy-path.spec.ts` | open app → start a Session from an Agent → send the 1+1 prompt → assert the streamed assistant text contains **"2"** and is **not** `(empty reply, exit=0)` → assert running activity and the final **READY** state. |
| `specs/interrupt.spec.ts` | kick a long turn, click Stop/Interrupt mid-stream, assert the run leaves its running state. |
| `specs/delete.spec.ts` | create a Session, archive it from the sidebar, and assert its row leaves the active list. |

## Prerequisites

For an existing deployment, configure its URL and required authentication.
For a temporary stack, the host needs:

1. A reachable Docker daemon and repository virtual environment.
2. Server and Agent images built from the repository root with
   `make build-server-image` and `make build-agent-image`.
3. A gitignored repo-root `.env` containing the model-provider configuration.
   `scripts/serve-backend.sh` passes it through the maintained Compose launcher,
   which uses generated database secret files and a dedicated sandbox callback
   edge. It does not expose the Docker gateway as a general callback service.
4. Installed frontend and browser-suite dependencies, using the Node release
   pinned in `.nvmrc`.

## Run

```bash
python3 scripts/node-toolchain.py npm --prefix frontend ci
python3 scripts/node-toolchain.py npm --prefix e2e ci
python3 scripts/node-toolchain.py npm --prefix e2e run install:browser
python3 scripts/node-toolchain.py npm --prefix e2e run test:happy -- --retries=0
```

Choose an existing deployment by setting `ASTRABOX_E2E_BASE_URL` for the test
command; this disables both managed servers. Without that URL, Playwright starts
the backend on port `8123`, then Vite on `5183`, with `/api` proxied to the
backend. `E2E_BACKEND_PORT` and `E2E_FRONTEND_PORT` override these ports.
`E2E_SKIP_WEBSERVER=1` with `E2E_BASE_URL` can target separately managed servers.

To diagnose another case, run `npm --prefix e2e test -- specs/<file>.spec.ts
--grep '<exact title>' --retries=0` through `scripts/node-toolchain.py`.
`npm --prefix e2e run report` opens the last HTML report. A selected pass does
not prove the rest of the suite.

## Notes

- **One worker, serial.** The config stops at the first failure and uses the
  shared 180-second test budget. Pass `--retries=0` for diagnostics because the
  base config enables one retry under `CI`.
- **No fake green.** The happy-path spec fails if the assistant text is empty or is
  the `(empty reply, exit=0)` signature — exactly the failure this gate exists to catch.
- Selectors are `data-testid` hooks on the real components (`frontend/src/...`);
  `specs/helpers.ts` centralizes them and the create-run flow.
- Cleanup depends on the spec. The happy-path and interrupt cases leave their
  Sessions in an existing deployment. After reading failure evidence, delete
  only the exact test-owned Sessions through the API. The temporary launcher
  removes its own Compose project and test volumes; a deployment-wide container
  label is not a safe test-ownership boundary.
