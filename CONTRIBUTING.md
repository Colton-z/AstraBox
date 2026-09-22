# Contributing to AstraBox

AstraBox is a self-hosted platform for running installed Agent programs in
isolated sandboxes. It provides a web console, APIs, saved Session history,
live output, and approval controls for teams. Thanks for helping improve it.

## Development setup

You need **Python 3.12+** and a working **Docker** daemon (only required to run a
real agent turn or the end-to-end suite; the unit gate needs neither). The
repository manages its exact Node.js version from `.nvmrc`; an existing matching
NVM install is reused, otherwise `make` downloads the checksum-pinned official
archive into `.astrabox/toolchains/node`.

Create a virtualenv and install the package editable with the dev tools:

```bash
python -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e '.[dev,sandbox-server]'
```

The `[dev]` extra pulls in `pytest`, its async/timeout/xdist support, `ruff`, and
`mypy`; it is enough for the Python gate (`make test-py`), lint, and typecheck.
`[sandbox-server]` adds the bundled
OpenSandbox service used by the default dev and container launch paths. A
deployment configured with an external lifecycle service connects to it instead.
If a required dependency is missing, startup exits with an explicit error.
Use the Makefile target to install
everything required by the complete `make test` gate, including the frontend,
channel gateway, API code generator, and both Playwright projects:

```bash
make install
```

### Run the local dev stack

`scripts/dev.sh` (also `make dev`) boots the two processes you iterate against:

- **Backend** — uvicorn (`astrabox.api.app:create_app`) on the host at **:8000** with
  `--reload`, started behind `astrabox.deploy.onebox` so the bundled OpenSandbox
  lifecycle server comes up beside it with access to `/var/run/docker.sock`.
- **Frontend** — the Vite dev server (`frontend/`) at **:5173**, whose `/api` proxies
  to the backend on `:8000`.

```bash
make dev                # == scripts/dev.sh  (Ctrl-C stops both)
```

Then open <http://localhost:5173>. The backend boots without credentials — the UI and
session list work — but a real agent turn additionally needs the agent image
(`make build-agent-image`) and your LLM key. The dev stack and the e2e gate both read
your Anthropic-compatible endpoint and token from a gitignored repo-root `.env`
(`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` **or** `ANTHROPIC_API_KEY`, optional
`ANTHROPIC_MODEL`). [`e2e/README.md`](e2e/README.md) covers the browser
happy-path run end to end.

You can run the backend on its own with `ASTRABOX_DEV_BACKEND_ONLY=1 scripts/dev.sh`,
or change the backend port with `ASTRABOX_DEV_BACKEND_PORT=9000 scripts/dev.sh`.

## Which loop your change needs

Use local static checks while editing, then choose verification by the behavior
changed. A live check needs a deployment you control: Docker, the agent image,
and a model key.

| Changed | Check with |
|---|---|
| Documentation or comments | Comment and link checks; for website changes, bilingual build and website browser checks |
| Frontend or backend behavior | Static checks, then the affected real E2E; the repository gate for a coherent release candidate |
| Backend code on an isolated development deployment | `make run-mounted` mounts this tree over a compatible image's package |
| Sandbox control service, Agent image, or deployment configuration | Build the affected images and verify the changed path against a live deployment |

Dependency or bundled frontend changes also require a new server image
(`make build-server-image`), because `make run-mounted` swaps code, not the
environment: it mounts this tree over the package the image installed.

Two things that are deliberate, and cost an afternoon when they surprise you:

- `make run-mounted` does not use `--reload`. A reloader restarting mid-turn
  drops the host end of a live sandbox connection, which surfaces as
  `runtime reconnect failed` and sends you hunting a sandbox bug that is your
  own dev server.
- Never push a changed `:latest` to a shared cluster. A Kubernetes node pulls
  `IfNotPresent`, so a new `:latest` leaves it serving the OLD image while your
  source says otherwise. `make push-images REGISTRY=<host:port>` tags by tree
  state precisely so a node cannot do that, and prints the
  `ASTRABOX_AGENT_IMAGE` line that points a deployment at what you pushed.

## Running tests

### Unit gate (no Docker, no secret)

Use a Git checkout at the candidate commit, including its Git metadata. Release
archive tests run `git archive HEAD` and inspect committed files; an extracted
deployment tarball alone is not sufficient for the complete Python suite.

This is what CI runs on every push and PR. A plain `pytest` run deselects the live-turn
suite by default (`pyproject.toml` sets `addopts = -m 'not e2e'`), so it never touches
Docker or an LLM key:

```bash
make test-py            # unit suite only (the `e2e` mark is deselected)
```

The repository gate combines the local targets below. CI runs the corresponding
checks in separate jobs in [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

```bash
make test               # complete repository gate
```

The target includes repository consistency checks, Python lint/typecheck/tests,
the generated API client check, channel-gateway tests, both Playwright typechecks,
and the console typecheck, production build, policy checks, and unit tests. The
Makefile is the current list. During diagnosis, retain the first failure and
prove a fix with the smallest relevant check before rerunning its E2E. Continue
with tests that lack evidence; do not restart an unaffected suite after each
edit. Run the full gate once for a coherent release candidate. Never remove or
weaken an assertion to make a failing change pass.

### End-to-end gate (needs Docker + an LLM key)

The live-turn suite is marked `e2e` and deselected by default. It needs a running
deployment and the appropriate model and credential fixtures. Run only the
selected cases for the behavior being changed; a model reply alone does not
prove file persistence, native resume, child tasks, or approvals.

```bash
ASTRABOX_E2E_BASE_URL=http://127.0.0.1:8088 make e2e-live
```

The development happy-path target builds the images and runs a curl smoke plus
the console browser path. It is not the complete multi-engine campaign:

```bash
make build-agent-image  # bake astrabox/sandbox-claude-code:latest (pins + smoke-tests the claude CLI)
make e2e
```

[`tests/e2e/README.md`](tests/e2e/README.md) describes the Python lane's two
phases and how to select exact nodes; [`tests/e2e-ui/README.md`](tests/e2e-ui/README.md)
does the same for the browser suite and lists the deployment fixtures each
group of specs reads. Core lifecycle and native-state recovery must work
without a persistent workspace volume; file retention is tested separately with
persistent storage.

### Frontend build and tests

The console SPA is TypeScript. Run its typecheck and real production build through
the repository's pinned Node.js toolchain:

```bash
make build-web
```

`make test-web` adds the channel gateway, console policy and unit tests, and both
Playwright suite typechecks.

## Repository map

Where to look when you want to change something:

| Path | What lives there |
|---|---|
| `astrabox/seams/` | Versioned plugin interfaces for sandbox, storage, model endpoint, secret store, repository, and identity providers. Third-party plugins implement these interfaces and register through the `astrabox.providers.*` Python entry-point groups. Preserve backward compatibility when extending them. |
| `astrabox/providers/` | The built-in providers: `open_sandbox/` (the sandbox backend — creates containers through the OpenSandbox lifecycle API; conversation tenancy isolates each Session, while agent tenancy can share one sandbox), `model.py` (the `litellm` model endpoint provider; no bundled direct passthrough), `secret_store.py` (AES-GCM local vault store), `identity.py` (no-auth defaults) + `identity_sso.py` (`trusted_header`/`jwt` resolvers). |
| `astrabox/deploy/` | Deployment startup: `onebox.py` starts AstraBox and supervises the bundled services selected by configuration; `sandbox_server.py` renders the bundled lifecycle server's config from `ASTRABOX_*`. |
| `astrabox/api/` | The HTTP API: `routes/` contains resource routers for Sessions, turns, Agents, Assistants, vaults, admin operations, MCP, and related resources; `app.py` contains the FastAPI factory, lifespan, and SPA serving. Routes stay thin — logic belongs in services. |
| `astrabox/web/` | ASGI middleware for browser/API authentication and trusted-host validation. |
| `astrabox/core/service/orchestrator/` | The platform core: `session_kernel/` (command, journal, projection, and turn/lifecycle workers), `runtime_manager.py` + `runtime/` (sandbox lifecycle and common workspace routing), `engine/` (native Agent-program adapters), `turn_service.py`, and deployment/assistant services. |
| `astrabox/persistence/` | Data access code: `repository/` provides the shared collection API with PostgreSQL, SQLite, and Mongo implementations; `migrations/` upgrades stored data at startup; `models/` defines persisted records. |
| `astrabox/config/` | Typed settings + the env-var registry (`env_registry.py`) — every env var the code reads must be registered; a census test enforces it. |
| `astrabox/testing/` | Reusable compatibility tests for collection, engine, sandbox, storage, and channel plugins, plus E2E fault-injection helpers. Plugin authors run these tests against their implementations. Production modules do not import this package. |
| `astrabox/common/` | Logger + small shared utils. |
| `frontend/` | The React console SPA (Vite; `npm run build` runs `tsc --noEmit` first). |
| `containers/` | The server, sandbox and LiteLLM images + compose. |
| `tests/` | Unit + conformance suites (file naming: `*_test.py`). `tests/e2e/` is the live-turn lane (`-m e2e`). |
| `e2e/` | The Playwright browser suite against the real console. |
| `tests/e2e-contract/` | What every live suite shares: the frozen lane inventory, the per-engine profiles the specs parametrize on, and the one 180-second test budget each Playwright config reads. |

## Coding conventions

- **Python**: target 3.12, `ruff` for lint, `mypy` for typechecking, line length 100.
  Configuration lives in [`pyproject.toml`](pyproject.toml) (`[tool.ruff]`,
  `[tool.mypy]`). Run `make lint` and `make typecheck` before opening a PR — both must
  stay green. Ruff grandfathers a handful of rule codes tree-wide, and mypy runs
  with a documented, grandfathered list of disabled error codes — both with
  rationale in `pyproject.toml`. The remaining enabled rules stay enforced,
  including in new code, so do not introduce new violations.
- **Errors**: use one explicit supported path. Do not silently switch providers,
  weaken security, or discard data when a required operation fails. Return an
  error with enough context to diagnose it.
- **Comments**: follow the rules in [Comments](#comments) below.
  `make check-comments` runs the mechanical half of them — over every tracked
  and untracked source file (`git ls-files`), which includes `.ts`, `.tsx`,
  `.css`, `.sh` and `.yaml`, not only Python. A TypeScript-only or test-only
  change is inside its scope; "this change is not Python" is not a reason to
  skip it, and a violation left behind stays invisible until some unrelated
  change runs the gate.
- **TypeScript**: run `make build-web` for console changes and `make test-web` for
  the complete web gate; both use the Node.js version pinned by the repository.
- **Tests**: name Python test files `*_test.py`. Test observable product behavior
  through real E2E, including native supplier evidence and user-visible results.
  Keep unit tests for small deterministic logic they uniquely prove. When moving
  an implementation-shaped contract to E2E, preserve its intent, obtain passing
  evidence, then remove the replaced contract. Reusable provider checks belong
  in `astrabox/testing/`, not copies per backend.
- Keep changes focused; match the style of the surrounding code.

## Comments

Write comments for contributors who know only this repository. A useful comment
explains behaviour they cannot infer from the code and points to files, symbols
or public sources they can inspect. `make check-comments` enforces the
mechanical half against a strict-zero baseline
(`scripts/comment_style_baseline.json`); `make check-comments-report` prints
every current finding grouped by class.

1. **Docstrings carry API behaviour.** Module, class and function docstrings
   reach API documentation, editor tooltips and `help()`. Put what callers rely
   on there — behaviour, prerequisites, expected errors, limits. An inline
   comment is for a local reason: an ordering requirement, a guard whose purpose
   its name does not carry, a value taken from a measurement.
2. **Describe the code, not how it got this way.** State the current
   requirement and its reason; change history belongs in the commit. Banned in
   comments: `used to`, `no longer`, `previously`, `originally`, `anymore`,
   `the regression`, `BUG (fixed)`, `moved verbatim`. When a guard prevents a
   known failure, describe the unguarded operation and the guard — not the
   guarded code as though it were broken.
3. **Every name a comment uses must resolve for the reader.** A code symbol, a
   repository path or a public URL. Never a work-plan or phase code (`F3-5`,
   `Stage T4`), an issue number in a tracker they cannot open, or a component
   that is not in this repository.
4. **State the failure, not where it was seen.** Keep the failure and its
   mechanism; do not cite private infrastructure a reader cannot inspect.
5. **The subject is something in the code.** Name the component or caller that
   acts. First person hides it — except inside a quoted value or user message.
6. **Explain why, never restate what.** If the code needs a comment to be
   readable, rename something or split the function instead.
7. **Neutral register.** No editorialising (`obviously`), no exasperation
   (`unfortunately`), no shouting. Emphasis is for a severe, silent failure —
   an all-caps `MUST NOT` on a security or data-loss constraint earns its keep.
8. **Section banners are noun phrases**, labelling a region so the file can be
   scanned; they are not headlines.
9. **No TODO, FIXME, HACK or commented-out code.** Track unfinished work in an
   issue with an owner and acceptance criteria. This tree has zero of these.
10. **Test comments explain the scenario's intent** — what situation is being
    built, and what would be wrong without the assertion.

## Pull request flow

1. Branch off `main`.
2. Make your change with focused commits.
3. Record what you ran: the static checks, and the live cases that cover the
   behaviour you changed. Run the complete repository gate when the batch is
   ready for review.
4. Open a pull request against `main`. CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml))
   runs the unit/lint/typecheck gate on your branch and **must pass** before merge. The
   live e2e workflow ([`.github/workflows/e2e.yml`](.github/workflows/e2e.yml)) is
   manually dispatched and gated on an LLM-key repo secret.
5. Describe what changed and why; link any related issue.

## License of contributions

AstraBox is licensed under the **Apache License 2.0** (see
[`LICENSE`](LICENSE)). By submitting a contribution you agree that it is licensed under
the same terms and that you have the right to contribute it.
