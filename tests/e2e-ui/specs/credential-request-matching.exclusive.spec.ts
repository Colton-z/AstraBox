/**
 * Real OpenSandbox Credential Vault request matching.
 *
 * The probe returns 204 only when it receives the test credential on GET
 * /allowed. AstraBox gives the sandbox a placeholder, and OpenSandbox may
 * replace it only for that method and path. POST /allowed and GET /denied must
 * reach the same host without receiving the credential and therefore return
 * 401. Both an ordinary cold-created box and an Agent-prepared box run the
 * same terminal and real Agent-tool journey.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';

const COLD_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT || '',
).trim();
const PREWARM_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_CREDENTIAL_PREWARM_ENVIRONMENT || '',
).trim();
const PROBE_URL = String(
  process.env.ASTRABOX_E2E_CREDENTIAL_PROBE_URL || '',
).trim().replace(/\/$/, '');
const MODEL_GATEWAY_URL = String(
  process.env.ASTRABOX_E2E_HTTPS_MODEL_GATEWAY_URL || '',
).trim().replace(/\/$/, '');
const RESEARCH_AGENT = String(
  process.env.ASTRABOX_E2E_RESEARCH_AGENT || '',
).trim();
const PROBE_SECRET = 'astrabox-e2e-request-match-secret';
const MODEL_PLACEHOLDER = 'astrabox-credential-held-by-egress-sidecar';
const COLD_ENVIRONMENT_SETUP = [
  'set ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT to a dedicated cold Environment name.',
  'Create it in Console > Manage > Environments with enabled=true, engine_kind=claude_code,',
  'sandbox_backend=open_sandbox, and sandbox_tenancy=conversation. Match the prepared',
  "Environment's runtime_template_name, endpoint_provider/provider_access, and networking,",
  'and keep Agent prewarming disabled in the cold test case. Keep and reuse this deployment',
  'fixture: AstraBox exposes no Environment DELETE API, so the spec cannot create and remove',
  'a disposable Environment.',
].join(' ');

const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 300_000);
const POOL_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_POOL_TIMEOUT_MS', 720_000);
const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 300_000);

interface HarnessResources {
  vaultId: string;
  agentId: string;
  sessionId: string;
}

function requiredConfiguration(): void {
  expect(
    COLD_ENVIRONMENT,
    COLD_ENVIRONMENT_SETUP,
  ).not.toEqual('');
  expect(
    PREWARM_ENVIRONMENT,
    'set ASTRABOX_E2E_CREDENTIAL_PREWARM_ENVIRONMENT to the OpenSandbox Agent-tenancy Environment',
  ).not.toEqual('');
  expect(
    PROBE_URL,
    'set ASTRABOX_E2E_CREDENTIAL_PROBE_URL to the test-only HTTP authorization probe',
  ).toMatch(/^http:\/\/[A-Za-z0-9.-]+$/);
  expect(
    MODEL_GATEWAY_URL,
    'set ASTRABOX_E2E_HTTPS_MODEL_GATEWAY_URL to the sandbox-facing team model gateway',
  ).toMatch(/^https:\/\/[A-Za-z0-9.-]+(?::443)?$/);
  const gateway = new URL(MODEL_GATEWAY_URL);
  expect(gateway.hostname, 'the HTTPS gateway must use an FQDN, not an IP or single-label host')
    .toMatch(/^(?=.{1,253}$)(?!\d+(?:\.\d+){3}$)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$/);
  expect(gateway.port, 'the HTTPS gateway must use the standard TLS port').toMatch(/^(?:|443)$/);
  expect(
    RESEARCH_AGENT,
    'set ASTRABOX_E2E_RESEARCH_AGENT to the deployed Claude Agent whose model route was proven',
  ).not.toEqual('');
}

async function concreteModel(api: AstraApi, environmentName: string): Promise<string> {
  return api.configuredAgentModel(RESEARCH_AGENT, environmentName);
}

function agentConfig(
  name: string,
  model: string,
  environmentName: string,
  prewarmEnabled: boolean,
) {
  return {
    name,
    model,
    environment_name: environmentName,
    prewarm_enabled: prewarmEnabled,
    system: 'Run the exact shell command requested by the test and report its output.',
  };
}

async function waitForAgentPool(platform: PlatformApi, agentId: string): Promise<string> {
  const deadline = Date.now() + POOL_TIMEOUT_MS;
  let last: Record<string, unknown> = {};
  while (Date.now() < deadline) {
    last = await platform.preparedRuntime(agentId);
    const sandboxId = String(last.sandbox_id || '').trim();
    const runtimeGeneration = String(last.runtime_generation || '').trim();
    const clientPoolName = String(last.client_pool_name || '').trim();
    if (
      last.ready === true
      && Number(last.prepared_count || 0) > 0
      && sandboxId
      && runtimeGeneration
      && clientPoolName
    ) {
      return sandboxId;
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  throw new Error(
    `Agent ${agentId} did not prepare a runtime within ${POOL_TIMEOUT_MS}ms; `
      + `last=${JSON.stringify(last)}`,
  );
}

function probeCommand(secretName: string): string {
  const allowed = `${PROBE_URL}/allowed`;
  const denied = `${PROBE_URL}/denied`;
  return [
    `value="\${${secretName}}"`,
    'case "$value" in ASTRABOX-VAULT-CRED::*) ;; *) exit 91 ;; esac',
    `allowed=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $value" '${allowed}')`,
    `method_denied=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $value" '${allowed}')`,
    `path_denied=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $value" '${denied}')`,
    'printf "ALLOWED=%s METHOD_DENIED=%s PATH_DENIED=%s\\n" "$allowed" "$method_denied" "$path_denied"',
    'test "$allowed" = 204',
    'test "$method_denied" = 401',
    'test "$path_denied" = 401',
  ].join('; ');
}

function shellLiteral(value: string): string {
  return `'${value.replace(/'/g, `'"'"'`)}'`;
}

/**
 * This runs as a Bash tool child of the real Agent, so it sees the same model
 * endpoint and credential that the Agent CLI sees. The endpoint must be the
 * configured HTTPS origin while the credential must still be the inert value
 * OpenSandbox replaces at egress. GET /v1/models then proves the TLS request
 * reached the live gateway and the sidecar supplied the real credential.
 */
function modelGatewayCommand(): string {
  return [
    'model_base="${ANTHROPIC_BASE_URL%/}"',
    `test "$model_base" = ${shellLiteral(MODEL_GATEWAY_URL)}`,
    'model_credential="${ANTHROPIC_AUTH_TOKEN:-${ANTHROPIC_API_KEY:-}}"',
    `test "$model_credential" = ${shellLiteral(MODEL_PLACEHOLDER)}`,
    'if [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then model_header="Authorization: Bearer $model_credential"; else model_header="x-api-key: $model_credential"; fi',
    'model_status=$(curl -sS -o /dev/null -w \'%{http_code}\' -H "$model_header" "$model_base/v1/models")',
    'printf "MODEL_GATEWAY_SCHEME=https MODEL_CREDENTIAL=placeholder MODEL_STATUS=%s\\n" "$model_status"',
    'test "$model_status" = 200',
  ].join('; ');
}

function streamFrames(raw: string): Array<Record<string, unknown>> {
  const frames: Array<Record<string, unknown>> = [];
  for (const line of raw.split('\n')) {
    if (!line.startsWith('data:')) continue;
    const payload = line.slice(5).trim();
    if (!payload || payload === '[DONE]') continue;
    try {
      frames.push(JSON.parse(payload) as Record<string, unknown>);
    } catch {
      // SSE keepalives are not product frames.
    }
  }
  return frames;
}

async function proveThroughTerminalAndAgent(
  api: AstraApi,
  page: Page,
  sessionId: string,
  secretName: string,
): Promise<void> {
  const requestCommand = probeCommand(secretName);
  const terminal = await api.runTerminalCommand(sessionId, requestCommand);
  expect(terminal).toContain('ALLOWED=204 METHOD_DENIED=401 PATH_DENIED=401');
  expect(terminal).not.toContain(PROBE_SECRET);

  const command = `${requestCommand}; ${modelGatewayCommand()}`;
  const raw = await api.streamPrompt(
    sessionId,
    `Use the Bash tool to run exactly this command, without changing it. ` +
      `After it succeeds, repeat both output lines verbatim in your final answer: ${command}`,
    'bypassPermissions',
    TURN_TIMEOUT_MS,
  );
  const frames = streamFrames(raw);
  const outputs = frames
    .filter((frame) => frame.type === 'tool-output-available')
    .map((frame) => String(frame.output || ''));
  expect(
    outputs.some((output) => output.includes('ALLOWED=204 METHOD_DENIED=401 PATH_DENIED=401')),
    'the real Agent tool process must use the same request-limited placeholder',
  ).toBe(true);
  expect(
    outputs.some((output) => output.includes(
      'MODEL_GATEWAY_SCHEME=https MODEL_CREDENTIAL=placeholder MODEL_STATUS=200',
    )),
    'the Agent tool must reach the real HTTPS gateway while seeing only the model placeholder',
  ).toBe(true);
  expect(raw).not.toContain(PROBE_SECRET);
  expect(frames.some((frame) => frame.type === 'error')).toBe(false);
  expect(frames.some((frame) => frame.type === 'finish')).toBe(true);

  await page.goto(appPath(`/sessions/${sessionId}`));
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });
  const renderedAnswer = page.getByTestId('assistant-message').last();
  await expect(renderedAnswer).toContainText(
    /(?:ALLOWED\s*=\s*204|GET\s*\/allowed[\s\S]{0,120}204)/i,
    { timeout: 60_000 },
  );
  await expect(renderedAnswer).toContainText(
    /(?:METHOD[_ ]DENIED\s*=\s*401|POST\s*\/allowed[\s\S]{0,120}401)/i,
  );
  await expect(renderedAnswer).toContainText(
    /(?:PATH[_ ]DENIED\s*=\s*401|GET\s*\/denied[\s\S]{0,120}401)/i,
  );
  await expect(renderedAnswer).toContainText(
    'MODEL_GATEWAY_SCHEME=https MODEL_CREDENTIAL=placeholder MODEL_STATUS=200',
  );
  await expect(renderedAnswer).not.toContainText(PROBE_SECRET);
}

async function proveSecurityPosture(
  platform: PlatformApi,
  sandboxId: string,
  credentialId: string,
): Promise<void> {
  const posture = await platform.sandboxSecurity(sandboxId);
  expect(posture.available, 'the sandbox itself must report a reachable egress sidecar').toBe(true);
  expect(String(posture.default_action || '').toLowerCase()).toBe('deny');

  const targets = (posture.egress_rules as Array<Record<string, unknown>> || [])
    .filter((rule) => String(rule.action || '').toLowerCase() === 'allow')
    .map((rule) => String(rule.target || '').toLowerCase());
  expect(targets).toContain(new URL(MODEL_GATEWAY_URL).hostname.toLowerCase());
  expect(
    targets,
    'the platform must admit an assigned Vault binding without changing the Environment allowlist',
  ).toContain(new URL(PROBE_URL).hostname.toLowerCase());

  const credentialNames = (posture.credential_names as unknown[] || []).map(String);
  const bindingNames = (posture.binding_names as unknown[] || []).map(String);
  const requestCredentialName = `astrabox-vault-${credentialId}`;
  expect(credentialNames).toContain('astrabox-model-gateway');
  expect(bindingNames).toContain('astrabox-model-gateway');
  expect(credentialNames).toContain(requestCredentialName);
  expect(bindingNames).toContain(`${requestCredentialName}-http`);
}

async function runJourney(
  api: AstraApi,
  platform: PlatformApi,
  page: Page,
  environmentName: string,
  prewarmed: boolean,
  resources: HarnessResources,
): Promise<void> {
  const runId = `${Date.now()}_${Math.random().toString(16).slice(2)}`;
  const secretName = `P002_REQUEST_TOKEN_${runId.toUpperCase()}`;
  const probeHost = new URL(PROBE_URL).hostname;
  const model = await concreteModel(api, environmentName);
  const vault = await api.data<Record<string, unknown>>(
    'POST',
    '/admin/vaults',
    { display_name: `__e2e_p002_request_match_${runId}` },
  );
  resources.vaultId = String(vault.vault_id || '');
  expect(resources.vaultId).not.toEqual('');

  const credential = await api.data<Record<string, unknown>>(
    'POST',
    `/admin/vaults/${resources.vaultId}/credentials`,
    {
      display_name: 'Request-limited probe token',
      auth: {
        type: 'environment_variable',
        secret_name: secretName,
        secret_value: PROBE_SECRET,
        networking: { type: 'limited', allowed_hosts: [`${probeHost}:80`] },
        injection_location: { header: true, body: false },
        allow_insecure_http: true,
        allowed_requests: { methods: ['GET'], paths: ['/allowed'] },
      },
    },
  );
  expect(credential).not.toHaveProperty('secret_value');
  expect(JSON.stringify(credential)).not.toContain(PROBE_SECRET);
  expect((credential.auth as Record<string, unknown>).allowed_requests).toEqual({
    methods: ['GET'],
    paths: ['/allowed'],
  });
  const credentialId = String(credential.credential_id || '');
  expect(credentialId).not.toEqual('');

  const name = `__e2e_p002_${prewarmed ? 'prewarm' : 'cold'}_${runId}`;
  const created = await api.createAgent(agentConfig(name, model, environmentName, false));
  resources.agentId = String(created.agent_id || '');
  expect(resources.agentId).not.toEqual('');

  await api.data(
    'PUT',
    `/admin/agents/${resources.agentId}/credential-vaults`,
    { vault_ids: [resources.vaultId] },
  );

  let preparedSandboxId = '';
  if (prewarmed) {
    const latest = await api.getAgent(resources.agentId);
    await api.updateAgent(resources.agentId, {
      ...agentConfig(name, model, environmentName, true),
      version: Number(latest.version || 1),
    });
    preparedSandboxId = await waitForAgentPool(platform, resources.agentId);
  }

  const session = await api.startConversation(resources.agentId);
  resources.sessionId = String(session.session_id || '');
  expect(resources.sessionId).not.toEqual('');
  const ready = await api.waitForSessionReady(resources.sessionId, READY_TIMEOUT_MS);
  const sandboxId = String(ready.sandbox_id || '');
  expect(sandboxId).not.toEqual('');
  if (prewarmed) {
    expect(
      sandboxId,
      'the credential journey must run in the exact Agent box published as prepared',
    ).toBe(preparedSandboxId);
    expect(
      await waitForAgentPool(platform, resources.agentId),
      'refilling the shared Agent box must not replace the claimed workload credential',
    ).toBe(sandboxId);
  }
  await proveSecurityPosture(platform, sandboxId, credentialId);
  await proveThroughTerminalAndAgent(api, page, resources.sessionId, secretName);
}

async function cleanup(
  api: AstraApi,
  request: APIRequestContext,
  resources: HarnessResources,
): Promise<void> {
  const failures: string[] = [];
  if (resources.sessionId) {
    try {
      await api.deleteSession(resources.sessionId);
    } catch (error) {
      failures.push(`Session ${resources.sessionId}: ${(error as Error).message}`);
    }
  }
  if (resources.agentId) {
    try {
      await api.data('PUT', `/admin/agents/${resources.agentId}/credential-vaults`, {
        vault_ids: [],
      });
    } catch (error) {
      failures.push(`Agent binding ${resources.agentId}: ${(error as Error).message}`);
    }
    try {
      await api.deleteAgent(resources.agentId);
    } catch (error) {
      failures.push(`Agent ${resources.agentId}: ${(error as Error).message}`);
    }
  }
  if (resources.vaultId) {
    try {
      const response = await request.delete(`/api/v1/admin/vaults/${resources.vaultId}`);
      if (!response.ok()) {
        throw new Error(`DELETE returned ${response.status()}: ${(await response.text()).slice(0, 500)}`);
      }
    } catch (error) {
      failures.push(`Vault ${resources.vaultId}: ${(error as Error).message}`);
    }
  }
  if (failures.length > 0) {
    throw new Error(`P0-02 cleanup failed:\n${failures.join('\n')}`);
  }
}

test.describe.serial('Credential request matching', () => {
  // The harness's own teardown, on the passing path only. A failing run keeps
  // the vault, the agent binding and the session — the three things a
  // credential-matching failure is diagnosed from.
  let resources: HarnessResources = { vaultId: '', agentId: '', sessionId: '' };
  onPassOnly(async ({ request }) => {
    await cleanup(new AstraApi(request), request, resources);
  });

  test.beforeEach(() => {
    requiredConfiguration();
  });

  for (const scenario of [
    { name: 'cold-created sandbox', environment: () => COLD_ENVIRONMENT, prewarmed: false },
    { name: 'Agent-prepared sandbox', environment: () => PREWARM_ENVIRONMENT, prewarmed: true },
  ]) {
    test(`${scenario.name} uses the HTTPS model gateway and limits Vault request matching`, async ({
      page,
      request,
    }) => {
      const api = new AstraApi(request);
      const platform = new PlatformApi(request);
      resources = { vaultId: '', agentId: '', sessionId: '' };
      try {
        await runJourney(
          api,
          platform,
          page,
          scenario.environment(),
          scenario.prewarmed,
          resources,
        );
      } finally {
        // `cleanup()` runs from an afterEach on the passing path — see
        // fixtures/sessionCleanup.ts. It deletes the session, unbinds and
        // deletes the agent, and deletes the vault, in that order and
        // reporting every failure; keeping that whole function is why it is
        // called rather than re-expressed here.
      }
    });
  }
});
