# AstraBox Community — developer entrypoints.
#
# These targets are the human-facing twins of what CI runs:
#   * `make test`             == the .github/workflows/ci.yml unit/lint/typecheck job
#                                (ruff + mypy + non-e2e pytest, + frontend
#                                 tsc, build & vitest) — green with NO Docker and NO secret.
#   * `make e2e`              == the .github/workflows/e2e.yml job (build the agent
#                                image, then the curl smoke + the Playwright happy
#                                path) — needs Docker + the operator LLM key (.env).
#   * `make test-postgresql`  — production PostgreSQL conformance suite.
#   * `make test-mongo`       — opt-in mongo conformance suite (`-m mongo`, deselected
#                                from `make test`) — needs a reachable mongod + `[mongo]`.
#   * `make dev`             — boot the local backend+frontend stack (scripts/dev.sh).
#   * `make build-agent-image`— build the in-sandbox astrabox/sandbox-claude-code:latest image.
#   * `make k8s-testbed-up`   — throwaway k3s + the OpenSandbox controller, so the
#                               sandbox server's kubernetes runtime can be exercised
#                               rather than assumed — needs passwordless sudo.
#
# Override the interpreter / image knobs on the command line, e.g.
#   make build-agent-image CLAUDE_CODE_VERSION=2.1.220
#   make test PY=python3.12

SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

# ── tools / knobs ────────────────────────────────────────────────────────────
VENV            ?= .venv
PY              ?= $(VENV)/bin/python
NODE_TOOLCHAIN   ?= python3 scripts/node-toolchain.py
RUFF            ?= $(PY) -m ruff
MYPY            ?= $(PY) -m mypy
PYTEST          ?= $(PY) -m pytest
PKG             ?= astrabox

AGENT_IMAGE         ?= astrabox/sandbox-claude-code:latest
ASSISTANT_IMAGE     ?= astrabox/sandbox-hermes:latest
DSH_IMAGE           ?= astrabox/sandbox-deepseek-harness:latest
CODEX_IMAGE         ?= astrabox/sandbox-codex:latest
PI_IMAGE            ?= astrabox/sandbox-pi:latest
WORKSPACE_MOUNTER_IMAGE ?= astrabox/workspace-mounter:latest
SERVER_IMAGE        ?= astrabox/server:latest
# Kubernetes nodes using IfNotPresent need a new tag for each image build;
# reusing `:latest` can leave a node serving cached content. The commit-derived
# tag also makes the deployed source identifiable from `docker images`.
# Source copies without `.git` use a timestamp so separate builds remain
# distinguishable.
IMAGE_TAG           ?= $(shell git rev-parse --short=12 HEAD 2>/dev/null && { git diff --quiet 2>/dev/null || echo -dirty; } || date -u +nogit-%Y%m%d%H%M%S)
REGISTRY            ?=
CLAUDE_CODE_VERSION ?= 2.1.266
SANDBOX_BASE_IMAGE  ?= ghcr.io/agent-infra/sandbox:1.11.0
NPM_REGISTRY        ?= https://registry.npmjs.org/

.PHONY: help install install-py install-web install-channel-gateway dev test test-py test-postgresql test-mongo test-opensandbox lint typecheck fmt-check check-e2e-collection \
        check-comments check-comments-report check-i18n check-upstream check-api-client audit-interaction audit-ui audit-console \
        test-web test-channel-gateway test-layout utilisation build-web build-dist e2e e2e-smoke e2e-browser e2e-live \
        build-agent-image build-assistant-image build-dsh-image build-codex-image build-pi-image build-workspace-mounter-image clean \
        k8s-testbed-up k8s-testbed-status k8s-testbed-down k8s-testbed-purge

help: ## Show this help.
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── install ──────────────────────────────────────────────────────────────────
install: install-py install-web install-channel-gateway ## Install Python, web, and channel-gateway dependencies.

# [sandbox-server] is NOT optional for a developer checkout, even though it is an
# optional extra of the package: every launch path (`make dev`, `make e2e-smoke`,
# scripts/dev.sh, scripts/e2e_smoke.sh) runs `python -m astrabox.deploy.onebox`,
# which starts the bundled OpenSandbox lifecycle server in-process and fails loud
# without it. The CI unit lane deliberately installs `.[dev]` alone — it never
# launches the stack — and that is what keeps `import astrabox` free of the extra.
install-py: ## Create .venv (if missing) and install the package editable with [dev,sandbox-server].
	@test -x "$(PY)" || python3 -m venv "$(VENV)"
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e '.[dev,sandbox-server]'

# scripts/api-codegen is its own npm package because openapi-typescript emits
# through the TypeScript compiler API and the console is on typescript@7, the Go
# port, which ships none — see scripts/check_api_client.py.
install-web: ## npm ci for every web/test package (lockfile-exact).
	$(NODE_TOOLCHAIN) npm --prefix frontend ci
	$(NODE_TOOLCHAIN) npm --prefix tests/e2e-ui ci
	$(NODE_TOOLCHAIN) npm --prefix e2e ci
	$(NODE_TOOLCHAIN) npm --prefix scripts/api-codegen ci

install-channel-gateway: ## npm ci for the bundled concrete channel adapters.
	$(NODE_TOOLCHAIN) npm --prefix channel-gateway ci --ignore-scripts

# ── dev ──────────────────────────────────────────────────────────────────────
dev: ## Boot the local dev stack: backend (:8000) + frontend (:5173). Ctrl-C stops.
	./scripts/dev.sh

# ── test (the CI unit/lint/typecheck job — no Docker, no secret) ─────────────
test: check-comments check-i18n check-errors check-credential-seam check-api-client lint typecheck test-py test-web ## Full unit gate: static checks + ruff + mypy + pytest + web build/vitest.

test-py: ## Fast unit suite (database-backed and live markers are deselected).
	$(NODE_TOOLCHAIN) $(PYTEST)

test-postgresql: ## Production PostgreSQL contract (explicit URL or maintained local Compose credentials).
	$(NODE_TOOLCHAIN) $(PYTEST) -m postgresql tests/postgresql_collection_conformance_test.py tests/postgresql_schema_migrations_test.py tests/postgresql_transcript_sequence_test.py

test-mongo: ## Opt-in: the mongo conformance suite. Needs a reachable mongod (ASTRABOX_DB_URL=mongodb://…) + `pip install -e '.[mongo]'`.
	$(NODE_TOOLCHAIN) $(PYTEST) -m mongo

test-opensandbox: ## Opt-in: the open_sandbox LIVE conformance lane. Needs a reachable opensandbox-server (ASTRABOX_OPENSANDBOX_LIVE_BASE_URL=http://…) that can pull the agent image; skips when unset.
	$(NODE_TOOLCHAIN) $(PYTEST) -m opensandbox

lint: ## ruff check (E402/F401/F811/F821/F841 grandfathered tree-wide — see pyproject).
	$(RUFF) check $(PKG) scripts tests

typecheck: ## mypy over the package (lenient config — see [tool.mypy]).
	$(MYPY) $(PKG)

fmt-check: ## Advisory ruff format check; not part of the repository gate.
	-$(RUFF) format --check $(PKG)

screenshots: ## Recapture assets/screenshots/ from a live deployment. Needs ASTRABOX_E2E_BASE_URL, and ASTRABOX_E2E_SCREENSHOT_MODEL naming a model the deployment holds a credential for.
	$(NODE_TOOLCHAIN) npm --prefix tests/e2e-ui run e2e -- console-screenshots

audit-ui: ## Check the visual grammar on every app + console route, both themes, two widths. Needs ASTRABOX_E2E_BASE_URL, and ASTRABOX_E2E_STORAGE_STATE where the deployment has a login.
	$(NODE_TOOLCHAIN) npm --prefix tests/e2e-ui run e2e -- visual-grammar

audit-interaction: ## What the console does when touched: every control must do something, every hover and ring must paint inside its own edges, and every ring must be findable on the ground it lands on. Same env as audit-ui.
	$(NODE_TOOLCHAIN) npm --prefix tests/e2e-ui run e2e -- console-interaction

audit-console: ## Every console audit at once: grammar, interaction, focus order, axe, rail counts, list scale, mobile shell, clipping. Same env as audit-ui.
	$(NODE_TOOLCHAIN) npm --prefix tests/e2e-ui run e2e -- audit.spec.ts

check-comments: ## Enforce the CONTRIBUTING.md comment rules against the reviewed zero baseline.
	$(PY) scripts/check_comment_style.py

check-comments-report: ## Print every current comment-style violation, grouped by class.
	$(PY) scripts/check_comment_style.py --report

check-i18n: ## Check console locale JSON, EN/ZH key parity, plurals, and placeholders.
	$(PY) scripts/check_i18n.py

check-errors: ## Every raised error code has a registry row, against the shrinking baseline.
	$(PY) scripts/check_error_registry.py

check-credential-seam: ## Credential machinery stays behind the egress seam, against the shrinking baseline.
	$(PY) scripts/check_credential_seam.py

check-api-client: ## frontend/src/api/schema.d.ts is byte-identical to what the committed OpenAPI snapshot generates. Needs `npm --prefix scripts/api-codegen ci` (make install-web).
	$(PY) scripts/check_api_client.py

check-upstream: ## Every vendored ui/ and ai-elements/ file still matches its registry. Needs the network; NOT in `make test` for that reason — run it when re-vendoring, and when upstream moves.
	$(PY) scripts/check_upstream.py

test-web: test-channel-gateway build-web typecheck-e2e ## Channel gateway + frontend typecheck (tsc --noEmit) + prod build + interaction-state, palette and failure-voice checks + vitest unit run + e2e spec typecheck.
	$(PY) scripts/check_interaction_states.py
	$(PY) scripts/check_palette.py
	$(PY) scripts/check_failure_voice.py
	$(NODE_TOOLCHAIN) npm --prefix frontend test

test-channel-gateway: ## Node tests for provider schemas and the bundled adapter runtime.
	$(NODE_TOOLCHAIN) npm --prefix channel-gateway test

typecheck-e2e: ## tsc --noEmit over both Playwright suites, plus the lane inventories. Playwright transpiles specs without typechecking them, so a wrong-arity API call is otherwise found on the testbed.
	$(NODE_TOOLCHAIN) npm --prefix tests/e2e-ui run typecheck
	$(NODE_TOOLCHAIN) npm --prefix e2e run typecheck
	$(PY) scripts/check_e2e_collection.py

check-e2e-collection: ## Ask Playwright what each e2e lane holds and compare to the frozen contract. Needs the e2e-ui dependency tree (make install-web).
	$(PY) scripts/check_e2e_collection.py

test-layout: ## Console layout gate across 4 viewports (stubbed /api — no backend, no Docker, no key). Needs chromium: `npm --prefix e2e run install:browser`.
	$(NODE_TOOLCHAIN) npm --prefix e2e run test:layout

utilisation: ## Report how much of the window each page uses, at 1280/1440/1920/2560. Asserts nothing — read it when adding or widening a page.
	$(NODE_TOOLCHAIN) npm --prefix e2e run utilisation

build-web: ## tsc --noEmit && vite build for the console.
	$(NODE_TOOLCHAIN) npm --prefix frontend run build

# ── e2e (needs Docker + the operator LLM key in .env) ────────────────────────
e2e: build-server-image build-agent-image e2e-smoke e2e-browser ## Full live e2e: build images, curl smoke, Playwright happy-path.

e2e-smoke: ## Headless curl-level live-turn proof (scripts/e2e_smoke.sh; asserts a real text-delta).
	./scripts/e2e_smoke.sh

e2e-browser: ## Browser happy-path through the real UI (e2e/ Playwright; boots its own stack).
	$(NODE_TOOLCHAIN) npm --prefix e2e ci
	$(NODE_TOOLCHAIN) npm --prefix e2e run install:browser
	$(NODE_TOOLCHAIN) npm --prefix e2e run test:happy

# The live pytest suite against a deployment that is ALREADY running — set
# ASTRABOX_E2E_BASE_URL to it. Five workers because these tests wait on a real
# model and a real cluster, not on CPU.
#
# scripts/e2e_live.sh, not a bare pytest, because `maxfail` only stops pytest
# handing out NEW tests: the workers already running keep going, and a live test
# can sit until its fixed 180 s timeout. The script kills the run at the first failed
# report and cleans up nothing, so the scene is there when you look.
E2E_WORKERS ?= 5
e2e-live: ## Main PostgreSQL + Agent live pytest lane (5 workers; first-red; keeps its sandbox).
	E2E_WORKERS=$(E2E_WORKERS) ./scripts/e2e_live.sh

# ── the Kubernetes testbed (scripts/k8s-testbed.sh) ──────────────────────────
# Stands up a throwaway k3s + the OpenSandbox CONTROLLER, so the sandbox server's
# kubernetes runtime can be exercised rather than assumed. The server itself is
# NOT installed into the cluster — AstraBox runs it, exactly as on one host.
# Needs passwordless sudo (it installs k3s). See docs/providers/opensandbox.md.

k8s-testbed-up: ## Bring up k3s + the OpenSandbox controller + the sandbox namespace, then print the env to set.
	./scripts/k8s-testbed.sh up

k8s-testbed-status: ## What the testbed is actually running (cluster, controller, CRDs, live sandboxes).
	./scripts/k8s-testbed.sh status

k8s-testbed-down: ## Remove the controller, its CRDs and both namespaces. Refuses while sandbox resources still exist.
	./scripts/k8s-testbed.sh down

k8s-testbed-purge: ## k8s-testbed-down, then uninstall k3s itself.
	./scripts/k8s-testbed.sh purge

# ── images ───────────────────────────────────────────────────────────────────
build-agent-image: ## Build the sandbox agent image; stop with an error if the pinned Claude CLI or runtime service is missing.
	docker build -t $(AGENT_IMAGE) \
	  --build-arg CLAUDE_CODE_VERSION=$(CLAUDE_CODE_VERSION) \
	  --build-arg SANDBOX_BASE_IMAGE=$(SANDBOX_BASE_IMAGE) \
	  --build-arg NPM_REGISTRY=$(NPM_REGISTRY) \
	  -f containers/sandbox-claude-code/Dockerfile \
	  .

build-assistant-image: ## Build the in-sandbox Hermes image the assistant engine runs in.
	# The Hermes pin stays in containers/sandbox-hermes/Dockerfile rather than
	# being passed from here: a second copy of the version is a second thing to
	# forget. The base image IS passed, because the assistant and claude-code
	# boxes must share it.
	docker build -t $(ASSISTANT_IMAGE) \
	  --build-arg SANDBOX_BASE_IMAGE=$(SANDBOX_BASE_IMAGE) \
	  -f containers/sandbox-hermes/Dockerfile \
	  .

build-dsh-image: ## Build the in-sandbox DeepSeek Harness runtime image.
	docker build -t $(DSH_IMAGE) \
	  --build-arg SANDBOX_BASE_IMAGE=$(SANDBOX_BASE_IMAGE) \
	  -f containers/sandbox-deepseek-harness/Dockerfile \
	  .

build-codex-image: ## Build the in-sandbox Codex app-server runtime image.
	docker build -t $(CODEX_IMAGE) \
	  --build-arg SANDBOX_BASE_IMAGE=$(SANDBOX_BASE_IMAGE) \
	  -f containers/sandbox-codex/Dockerfile \
	  .

build-pi-image: ## Build the in-sandbox pi RPC runtime image.
	docker build -t $(PI_IMAGE) \
	  --build-arg SANDBOX_BASE_IMAGE=$(SANDBOX_BASE_IMAGE) \
	  --build-arg NPM_REGISTRY=$(NPM_REGISTRY) \
	  -f containers/sandbox-pi/Dockerfile \
	  .

build-workspace-mounter-image: ## Build the host-side mergerfs workspace helper, outside agent sandboxes.
	docker build -t $(WORKSPACE_MOUNTER_IMAGE) -f containers/workspace-mounter/Dockerfile .

build-server-image: ## Build the deployment image (console + backend). Slow by design: it builds the frontend.
	docker build --build-arg NODE_VERSION=$$(cat .nvmrc) -t $(SERVER_IMAGE) -f containers/server/Dockerfile .

# Content-addressed publish for anything a CLUSTER pulls. Tagging by tree state
# makes IfNotPresent safe: every changed tree receives a distinct tag, so a node
# cannot serve a stale image for the requested tag.
push-images: ## Tag every cluster-pulled image $(IMAGE_TAG) and push to $$REGISTRY (e.g. REGISTRY=10.0.0.5:5000).
	@test -n "$(REGISTRY)" || { echo "push-images: set REGISTRY=host:port"; exit 2; }
	docker tag $(AGENT_IMAGE)     $(REGISTRY)/astrabox/sandbox-claude-code:$(IMAGE_TAG)
	docker tag $(ASSISTANT_IMAGE) $(REGISTRY)/astrabox/sandbox-hermes:$(IMAGE_TAG)
	docker tag $(DSH_IMAGE)       $(REGISTRY)/astrabox/sandbox-deepseek-harness:$(IMAGE_TAG)
	docker tag $(CODEX_IMAGE)     $(REGISTRY)/astrabox/sandbox-codex:$(IMAGE_TAG)
	docker tag $(PI_IMAGE)        $(REGISTRY)/astrabox/sandbox-pi:$(IMAGE_TAG)
	docker tag $(WORKSPACE_MOUNTER_IMAGE) $(REGISTRY)/astrabox/workspace-mounter:$(IMAGE_TAG)
	docker tag $(SERVER_IMAGE)    $(REGISTRY)/astrabox/server:$(IMAGE_TAG)
	docker push $(REGISTRY)/astrabox/sandbox-claude-code:$(IMAGE_TAG)
	docker push $(REGISTRY)/astrabox/sandbox-hermes:$(IMAGE_TAG)
	docker push $(REGISTRY)/astrabox/sandbox-deepseek-harness:$(IMAGE_TAG)
	docker push $(REGISTRY)/astrabox/sandbox-codex:$(IMAGE_TAG)
	docker push $(REGISTRY)/astrabox/sandbox-pi:$(IMAGE_TAG)
	docker push $(REGISTRY)/astrabox/workspace-mounter:$(IMAGE_TAG)
	docker push $(REGISTRY)/astrabox/server:$(IMAGE_TAG)
	@echo
	@echo "pushed $(IMAGE_TAG) — point the deployment at it:"
	@echo "  ASTRABOX_AGENT_IMAGE=$(REGISTRY)/astrabox/sandbox-claude-code:$(IMAGE_TAG)"
	@echo "  ASTRABOX_WORKSPACE_MOUNTER_IMAGE=$(REGISTRY)/astrabox/workspace-mounter:$(IMAGE_TAG)"
	@echo "  assistant environment runtime_template_name=$(REGISTRY)/astrabox/sandbox-hermes:$(IMAGE_TAG)"
	@echo "  dsh environment runtime_template_name=$(REGISTRY)/astrabox/sandbox-deepseek-harness:$(IMAGE_TAG)"
	@echo "  codex environment runtime_template_name=$(REGISTRY)/astrabox/sandbox-codex:$(IMAGE_TAG)"
	@echo "  pi environment runtime_template_name=$(REGISTRY)/astrabox/sandbox-pi:$(IMAGE_TAG)"

# ── Source-mounted deployment loop ───────────────────────────────────────────
# scripts/dev.sh mounts this checkout over the installed package while keeping
# the Compose database and sandbox-edge topology. Rebuild the server
# image only when dependencies change; Python source changes reload in place.
run-mounted: ## Run the source-mounted backend on :8088 using the maintained Compose topology.
	@test -f .env || { echo "run-mounted: needs .env (see .env.example)"; exit 2; }
	ASTRABOX_DEV_BACKEND_ONLY=1 \
	ASTRABOX_DEV_BACKEND_PORT=8088 \
	ASTRABOX_SERVER_IMAGE=$(SERVER_IMAGE) \
	./scripts/dev.sh

build-dist: build-web ## Release wheel+sdist WITH the console packaged (astrabox/_frontend_dist).
	rm -rf astrabox/_frontend_dist dist
	cp -r frontend/dist astrabox/_frontend_dist
	$(PY) -m build
	@unzip -l dist/*.whl | grep -q "astrabox/_frontend_dist/index.html" \
	  || { echo "build-dist: wheel is MISSING the packaged console (astrabox/_frontend_dist)"; exit 1; }
	@unzip -l dist/*.whl | grep -q "astrabox/py.typed" \
	  || { echo "build-dist: wheel is MISSING the PEP 561 marker (astrabox/py.typed)"; exit 1; }
	@tar tzf dist/*.tar.gz | grep -q "_frontend_dist/index.html" \
	  || { echo "build-dist: sdist is MISSING the packaged console"; exit 1; }
	@echo "build-dist: console packaged OK ($$(unzip -l dist/*.whl | grep -c _frontend_dist) files)"

# ── housekeeping ─────────────────────────────────────────────────────────────
clean: ## Remove build/test caches + web dist dirs (does NOT touch .venv or state).
	rm -rf .pytest_cache .ruff_cache .mypy_cache **/__pycache__ \
	       frontend/dist astrabox/_frontend_dist dist \
	       e2e/test-results e2e/playwright-report
