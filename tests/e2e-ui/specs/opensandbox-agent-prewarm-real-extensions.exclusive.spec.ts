/**
 * Product E2E for AstraBox Agent preparation on OpenSandbox.
 *
 * OpenSandbox's Redis-coordinated client pool supplies complete base boxes;
 * AstraBox's preparer installs the Agent version's real Git Skill and runtime
 * plugin. This spec proves both tenancy modes through the public product API
 * and the browser, including durable shared-session placement across restart.
 *
 * It deliberately requires task-owned Environments and real extension sources.
 * A skipped extension source would turn it into a wiring test that never
 * exercises preparation.
 */
import { test, expect, type Page } from '@playwright/test';

import { AstraApi, type AdminSessionRecord, type SessionRecord } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { onPassOnly } from '../fixtures/sessionCleanup';
import { sessionDoc, snapshotDoc } from '../fixtures/dbOracle';
import { absoluteBaseUrl, appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { requireSandboxHandle, restartServerContainer, sandboxExec } from '../fixtures/sandboxOps';

const SHARED_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '',
).trim();
const CONVERSATION_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT || '',
).trim();
const RESEARCH_AGENT = String(
  process.env.ASTRABOX_E2E_RESEARCH_AGENT || '',
).trim();

const SKILL_DESCRIPTOR = String(
  process.env.ASTRABOX_E2E_REAL_SKILL_DESCRIPTOR
    || 'https://github.com/anthropics/skills.git@main#skills/skill-creator',
).trim();
const PLUGIN_REPOSITORY = String(
  process.env.ASTRABOX_E2E_REAL_PLUGIN_REPOSITORY
    || 'https://github.com/anthropics/claude-plugins-official.git',
).trim();
const PLUGIN_PATH = String(
  process.env.ASTRABOX_E2E_REAL_PLUGIN_PATH || 'plugins/frontend-design',
).trim();

// Leave enough of the fixed 180s test budget to report the failing phase and
// retain its scene. A healthy prepared box reaches each boundary well inside
// these limits; the live testbed currently prepares one in about 15 seconds.
const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 60_000);
const POOL_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_POOL_TIMEOUT_MS', 60_000);
const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 90_000);

interface Placement {
  sandboxId: string;
  isolatedSessionId: string;
  terminalIsolatedSessionId: string;
  homeDir: string;
  uid: number;
}

interface PreparedRuntimeStatus {
  clientPoolName: string | null;
  preparedCount: number;
  runtimeGeneration: string;
  sandboxId: string;
}

function requiredConfiguration(): void {
  expect(
    SHARED_ENVIRONMENT,
    'set ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT to an OpenSandbox Agent-tenancy Environment',
  ).not.toEqual('');
  expect(
    CONVERSATION_ENVIRONMENT,
    'set ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT to an OpenSandbox conversation-tenancy Environment',
  ).not.toEqual('');
  expect(
    RESEARCH_AGENT,
    'set ASTRABOX_E2E_RESEARCH_AGENT to the deployed Claude Agent whose model route was proven',
  ).not.toEqual('');
}

async function concreteModel(api: AstraApi, environmentName: string): Promise<string> {
  return api.configuredAgentModel(RESEARCH_AGENT, environmentName);
}

function extensionAgentConfig(name: string, model: string, environmentName: string) {
  return {
    name,
    model,
    environment_name: environmentName,
    prewarm_enabled: true,
    skills: [SKILL_DESCRIPTOR],
    plugin_repos: [
      {
        url: PLUGIN_REPOSITORY,
        protocol: 'https',
        branch: 'main',
        depth: 1,
        plugin_paths: [PLUGIN_PATH],
      },
    ],
  };
}

function extensionAgentCreateConfig(name: string, model: string, environmentName: string) {
  return {
    ...extensionAgentConfig(name, model, environmentName),
    // createAgent must carry this policy through the dedicated second-phase
    // /access write. The strict authoring POST refuses all three fields.
    visibility: 'allowlist' as const,
    admins: ['p144-secondary-manager'],
    allowed_user_ids: ['p144-allowed-viewer'],
  };
}

async function waitForPoolReady(
  platform: PlatformApi,
  agentId: string,
  options: {
    differentRuntimeGenerationFrom?: string;
    differentSandboxFrom?: string;
  } = {},
): Promise<PreparedRuntimeStatus> {
  const deadline = Date.now() + POOL_TIMEOUT_MS;
  let last: Record<string, unknown> = {};
  while (Date.now() < deadline) {
    last = await platform.preparedRuntime(agentId);
    const clientPoolName = String(last.client_pool_name || '').trim() || null;
    const runtimeGeneration = String(last.runtime_generation || '').trim();
    const sandboxId = String(last.sandbox_id || '').trim();
    const preparedCount = Number(last.prepared_count || 0);
    if (
      last.ready === true
      && preparedCount > 0
      && runtimeGeneration
      && sandboxId
      && (
        !options.differentRuntimeGenerationFrom
        || runtimeGeneration !== options.differentRuntimeGenerationFrom
      )
      && (!options.differentSandboxFrom || sandboxId !== options.differentSandboxFrom)
    ) {
      return { clientPoolName, preparedCount, runtimeGeneration, sandboxId };
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  throw new Error(
    `Agent ${agentId} did not prepare a runtime within ${POOL_TIMEOUT_MS}ms; `
      + `last=${JSON.stringify(last)}`,
  );
}

async function startConversationFromCard(
  page: Page,
  agentName: string,
): Promise<string> {
  await page.goto(appPath('/agents'));
  const card = page.locator(
    `[data-testid="agent-option"][data-agent-name="${agentName}"]`,
  );
  await expect(card, `${agentName} must appear in the Agent picker`).toBeVisible({
    timeout: 45_000,
  });
  await card.getByRole('button').click();
  await page.waitForURL((url) => /\/sessions\/[^/]+$/.test(url.pathname), {
    timeout: 120_000,
  });
  const sessionId = new URL(page.url()).pathname.split('/').filter(Boolean).pop() || '';
  expect(sessionId, 'the Agent card must open a concrete conversation').not.toEqual('');
  return sessionId;
}

function placement(session: AdminSessionRecord, shared: boolean): Placement {
  const identity = (session.runtime_identity || {}) as Record<string, unknown>;
  const resolved: Placement = {
    sandboxId: String(session.sandbox_id || '').trim(),
    isolatedSessionId: String(identity.isolated_session_id || '').trim(),
    terminalIsolatedSessionId: String(
      identity.terminal_isolated_session_id || '',
    ).trim(),
    homeDir: String(identity.home_dir || '').trim(),
    uid: Number(identity.uid || 0),
  };
  expect(resolved.sandboxId, 'READY session must name its sandbox').not.toEqual('');
  expect(resolved.homeDir, 'READY session must expose its workload HOME').not.toEqual('');
  expect(resolved.uid, 'READY session must expose its workload UID').toBeGreaterThan(0);
  if (shared) {
    expect(
      resolved.isolatedSessionId,
      'shared tenancy must persist the OpenSandbox isolated-session id',
    ).not.toEqual('');
    expect(
      resolved.terminalIsolatedSessionId,
      'shared tenancy must persist its disposable terminal-session id',
    ).not.toEqual('');
    expect(
      resolved.terminalIsolatedSessionId,
      'the terminal must not share the Agent runner isolation session',
    ).not.toBe(resolved.isolatedSessionId);
  } else {
    expect(
      resolved.isolatedSessionId,
      'conversation tenancy owns the whole box and must not masquerade as a shared placement',
    ).toEqual('');
    expect(
      resolved.terminalIsolatedSessionId,
      'conversation tenancy uses the box terminal and must not persist a shared terminal session',
    ).toEqual('');
  }
  return resolved;
}

async function waitForIsolatedTerminalBinding(sessionId: string): Promise<string> {
  const deadline = Date.now() + 30_000;
  let last = '';
  while (Date.now() < deadline) {
    last = String(snapshotDoc(sessionId)?.active_terminal_execution_id || '').trim();
    if (last.startsWith('isolated-run:')) return last;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(
    `shared terminal did not publish its isolated execution binding within 30000ms; last=${last || '<none>'}`,
  );
}

function sseFrames(raw: string): Array<Record<string, unknown>> {
  const frames: Array<Record<string, unknown>> = [];
  for (const line of raw.split('\n')) {
    if (!line.startsWith('data:')) continue;
    const payload = line.slice(5).trim();
    if (!payload || payload === '[DONE]') continue;
    try {
      frames.push(JSON.parse(payload) as Record<string, unknown>);
    } catch {
      // Non-JSON SSE comments are transport keepalives, not product frames.
    }
  }
  return frames;
}

interface CommandDetail {
  name: string;
  description: string;
}

function commandName(value: unknown): string {
  if (value && typeof value === 'object') {
    const item = value as Record<string, unknown>;
    return String(item.name ?? item.command ?? '').trim().replace(/^\/+/, '');
  }
  return String(value ?? '').trim().replace(/^\/+/, '');
}

function normalizedText(value: unknown): string {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}

function expectExtensionCommandMetadata(session: SessionRecord): CommandDetail[] {
  expect(session.state, 'READY must already carry extension command metadata').toBe('READY');
  expect(Array.isArray(session.slash_command_details)).toBe(true);
  expect(Array.isArray(session.slash_commands)).toBe(true);
  const details = (session.slash_command_details as Array<Record<string, unknown>>)
    .map((item) => ({ name: commandName(item), description: normalizedText(item.description) }));
  const names = details.map((item) => item.name);
  expect(names.length, 'READY command metadata must not be empty').toBeGreaterThan(0);
  expect(new Set(names).size, 'READY command names must be unique').toBe(names.length);
  expect((session.slash_commands as unknown[]).map(commandName).sort()).toEqual([...names].sort());
  for (const detail of details) {
    expect(detail.name, 'command metadata must name every entry').not.toBe('');
    expect(detail.description, `${detail.name} must have a description`).not.toBe('');
  }
  for (const name of ['compact', 'skill-creator', 'frontend-design:frontend-design']) {
    expect(names, `${name} must be available before the first model input`).toContain(name);
  }
  return details;
}

async function installedPluginDescription(
  api: AstraApi,
  session: AdminSessionRecord,
): Promise<string[]> {
  const identity = session.runtime_identity as Record<string, unknown>;
  const plan = identity.capability_plan as Record<string, unknown>;
  const repos = plan.plugin_repo_plan as Array<Record<string, unknown>>;
  const selected = repos.filter((repo) => repo.url === PLUGIN_REPOSITORY);
  expect(selected, 'the source probe must address the selected prepared repository').toHaveLength(1);
  const checkout = String(selected[0].target_path || '');
  expect(checkout).toMatch(/^\/.+/);
  const sandbox = await requireSandboxHandle(api, String(session.sandbox_id));
  const source = JSON.parse(sandboxExec(sandbox, [
    "python3 - <<'PY'",
    'import hashlib, json, pathlib, subprocess',
    `checkout = pathlib.Path(${JSON.stringify(checkout)})`,
    `plugin = checkout / ${JSON.stringify(PLUGIN_PATH)}`,
    'skill = plugin / "skills/frontend-design/SKILL.md"',
    'content = skill.read_bytes()',
    'revision = subprocess.run(["git", "-c", "safe.directory=" + str(checkout),',
    '    "-C", str(checkout), "rev-parse", "HEAD"], check=True,',
    '    capture_output=True, text=True, timeout=10).stdout.strip()',
    'manifest = json.loads((plugin / ".claude-plugin/plugin.json").read_text())',
    'print(json.dumps({"revision": revision, "sha256": hashlib.sha256(content).hexdigest(),',
    '    "plugin_name": manifest["name"], "content": content.decode("utf-8")}))',
    'PY',
  ].join('\n'))) as { revision: string; sha256: string; plugin_name: string; content: string };
  expect(source.revision).toMatch(/^[0-9a-f]{40}$/);
  expect(source.sha256).toMatch(/^[0-9a-f]{64}$/);
  expect(source.plugin_name).toBe('frontend-design');
  // This selected public plugin uses plain, single-line YAML name/description.
  // Refuse a changed source shape instead of treating an unparsed value as metadata.
  const frontmatter = source.content.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/)?.[1];
  expect(frontmatter, 'the installed plugin must contain its own metadata').toBeTruthy();
  expect(frontmatter!.match(/^name: (.+)$/m)?.[1].trim()).toBe('frontend-design');
  const description = normalizedText(frontmatter!.match(/^description: (.+)$/m)?.[1]);
  expect(description).not.toMatch(/^[>|'"]/);
  const words = description.split(' ');
  expect(words.length, 'the source description must provide two meaningful short fragments').toBeGreaterThanOrEqual(8);
  const fragments = [words.slice(0, 4).join(' '), words.slice(4, 8).join(' ')];
  await test.info().attach(`plugin-command-source-${session.session_id}`, {
    contentType: 'application/json',
    body: Buffer.from(JSON.stringify({
      repository: PLUGIN_REPOSITORY, pluginPath: PLUGIN_PATH,
      revision: source.revision, sha256: source.sha256, description, fragments,
    })),
  });
  return fragments;
}

function expectPluginDescription(details: CommandDetail[], fragments: string[]): void {
  const description = details.find((item) => item.name === 'frontend-design:frontend-design')!.description;
  for (const fragment of fragments) expect(description).toContain(fragment);
}

async function expectExtensionCommandMenu(page: Page, fragments: string[]): Promise<void> {
  const composer = page.getByTestId('composer-prompt');
  await expect(composer).toBeEnabled();
  await composer.fill('/');
  const menu = page.getByTestId('slash-command-menu');
  await expect(menu).toBeVisible();
  for (const name of ['compact', 'skill-creator', 'frontend-design:frontend-design']) {
    const option = menu.locator(`[data-testid="slash-command-option"][data-command-name="/${name}"]`);
    await option.scrollIntoViewIfNeeded();
    const label = option.getByTestId('slash-command-name');
    await expect(label).toHaveText(`/${name}`);
    await expect(label).toBeVisible();
    const description = option.getByTestId('slash-command-description');
    await expect(description).toBeVisible();
    expect(normalizedText(await description.innerText())).not.toBe('');
    if (name === 'frontend-design:frontend-design') {
      for (const fragment of fragments) await expect(description).toContainText(fragment);
    }
    expect(await label.evaluate((element) => {
      const box = element.getBoundingClientRect();
      const painted = document.elementFromPoint(box.left + Math.min(8, box.width / 2), box.top + box.height / 2);
      return box.width > 0 && box.height > 0 && painted !== null && element.contains(painted);
    }), `${name} must be painted, not merely present in the menu DOM`).toBe(true);
  }
  await composer.fill('');
  await expect(menu).toHaveCount(0);
}

async function useRealSkillAndPlugin(
  api: AstraApi,
  sessionId: string,
  marker: string,
): Promise<void> {
  const prompt = [
    'Use the Skill tool to invoke skill-creator.',
    'Then use the Skill tool to invoke frontend-design:frontend-design.',
    `After both calls succeed, use Bash to print exactly ${marker}.`,
    `End with exactly ${marker}_OK.`,
  ].join(' ');
  const raw = await api.streamPrompt(
    sessionId,
    prompt,
    'bypassPermissions',
    TURN_TIMEOUT_MS,
  );
  const frames = sseFrames(raw);
  const skillInputs = frames
    .filter((frame) => frame.type === 'tool-input-available' && frame.toolName === 'Skill')
    .map((frame) => String((frame.input as Record<string, unknown> | undefined)?.skill || ''));
  const outputs = frames
    .filter((frame) => frame.type === 'tool-output-available')
    .map((frame) => String(frame.output || ''));

  expect(skillInputs, 'the real Git Skill must be callable by name').toContain('skill-creator');
  expect(skillInputs, 'the real runtime Plugin must contribute its Skill command').toContain(
    'frontend-design:frontend-design',
  );
  expect(
    outputs.some((output) => output.includes('Launching skill: skill-creator')),
    'Skill loading must return a successful tool result',
  ).toBe(true);
  expect(
    outputs.some((output) => output.includes('Launching skill: frontend-design:frontend-design')),
    'Plugin Skill loading must return a successful tool result',
  ).toBe(true);
  expect(
    outputs.some((output) => output.includes(marker)),
    'the Agent must continue into a real sandbox tool after loading both extensions',
  ).toBe(true);
  expect(frames.some((frame) => frame.type === 'error'), 'extension turn must not emit an error frame').toBe(false);
  expect(frames.some((frame) => frame.type === 'finish'), 'extension turn must settle').toBe(true);
}

async function expectModelTurn(api: AstraApi, sessionId: string, marker: string): Promise<void> {
  const raw = await api.streamPrompt(
    sessionId,
    `Reply with exactly ${marker}. Do not use tools.`,
    'bypassPermissions',
    TURN_TIMEOUT_MS,
  );
  const frames = sseFrames(raw);
  expect(frames.some((frame) => frame.type === 'error'), 'resumed model turn must not fail').toBe(false);
  expect(frames.some((frame) => frame.type === 'finish'), 'resumed model turn must settle').toBe(true);
  expect(raw, 'resumed model turn must reach the configured model').toContain(marker);
}

async function expectToolCards(page: Page, sessionId: string): Promise<void> {
  await page.goto(appPath(`/sessions/${sessionId}`));
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });
  // A settled response's tool work arrives on a cold page as one header, and
  // the cards behind it are not in the document until it is opened, so the
  // transcript is waited for and then opened the way a reader opens it.
  await expect(page.getByTestId('assistant-message').first()).toBeVisible({ timeout: 60_000 });
  await revealAssistantProcess(page);
  await expect
    .poll(
      () => page.getByTestId('assistant-message').getByRole('button', { name: /^Skill\b/ }).count(),
      { timeout: 60_000 },
    )
    .toBeGreaterThanOrEqual(2);
}

async function cleanup(
  api: AstraApi,
  sessionIds: Set<string>,
  agentId: string,
  additionalAgentIds: string[],
): Promise<void> {
  const failures: string[] = [];
  for (const sessionId of [...sessionIds].reverse()) {
    try {
      await api.deleteSession(sessionId);
      sessionIds.delete(sessionId);
    } catch (error) {
      failures.push(`session ${sessionId}: ${(error as Error).message}`);
    }
  }
  for (const id of [agentId, ...additionalAgentIds].filter(Boolean)) {
    try {
      await api.deleteAgent(id);
    } catch (error) {
      failures.push(`Agent ${id}: ${(error as Error).message}`);
    }
  }
  if (failures.length > 0) {
    throw new Error(`P1-44 cleanup failed:\n${failures.join('\n')}`);
  }
}

test.describe.serial('OpenSandbox Agent prepared runtimes', () => {
  // The live tests in this file are serial and each resets these at its start; the
  // hook below is the only reader. A failing preparation run keeps its boxes so
  // the runtime and extension state remain available for diagnosis.
  let sessions = new Set<string>();
  let agentId = '';
  let additionalAgentIds: string[] = [];
  onPassOnly(async ({ request }) => {
    await cleanup(new AstraApi(request), sessions, agentId, additionalAgentIds);
  });

  test('shared tenancy keeps isolated conversations safe across restart and deletion', async ({
    page,
    request,
  }) => {
    requiredConfiguration();
    const api = new AstraApi(request);
    const platform = new PlatformApi(request);
    const runId = Date.now();
    const agentName = `__e2e_p144_shared_${runId}`;
    const model = await concreteModel(api, SHARED_ENVIRONMENT);
    sessions = new Set<string>();
    agentId = '';
    additionalAgentIds = [];

    try {
      const agent = await api.createAgent(
        extensionAgentCreateConfig(agentName, model, SHARED_ENVIRONMENT),
      );
      agentId = String(agent.agent_id || '');
      expect(agentId).not.toEqual('');

      const pool = await waitForPoolReady(platform, agentId);
      expect(
        pool.clientPoolName,
        'shared tenancy must use the OpenSandbox SDK client pool',
      ).not.toBeNull();
      test.info().annotations.push({
        type: 'client_pool',
        description: String(pool.clientPoolName),
      });

      const firstId = await startConversationFromCard(page, agentName);
      sessions.add(firstId);
      const firstReady = await api.waitForSessionReady(firstId, READY_TIMEOUT_MS);
      const initialCommands = expectExtensionCommandMetadata(firstReady);
      const firstDetail = await api.adminSessionDetail(firstId);
      const first = placement(firstDetail, true);
      expect(
        first.sandboxId,
        'the first shared conversation must claim the Agent box prepared from the SDK client pool',
      ).toBe(pool.sandboxId);
      const descriptionFragments = await installedPluginDescription(api, firstDetail);
      expectPluginDescription(initialCommands, descriptionFragments);
      expect((await api.getMessages(firstId)).messages).toHaveLength(0);
      await expectExtensionCommandMenu(page, descriptionFragments);

      const refilled = await waitForPoolReady(platform, agentId);
      expect(
        refilled.sandboxId,
        'Agent-tenancy refill must prepare another isolated runtime in the Agent-owned box',
      ).toBe(first.sandboxId);
      expect(refilled.runtimeGeneration).toBe(pool.runtimeGeneration);
      expect(refilled.clientPoolName).toBe(pool.clientPoolName);

      const secondCreated = await api.startConversation(agentId);
      const secondId = secondCreated.session_id;
      sessions.add(secondId);
      await api.waitForSessionReady(secondId, READY_TIMEOUT_MS);
      const second = placement(await api.adminSessionDetail(secondId), true);

      expect(second.sandboxId, 'shared conversations must use the Agent-owned box').toBe(
        first.sandboxId,
      );
      expect(second.isolatedSessionId).not.toBe(first.isolatedSessionId);
      expect(second.homeDir).not.toBe(first.homeDir);
      expect(second.uid).not.toBe(first.uid);

      const firstProof = `P144_SHARED_FIRST_${runId}`;
      const firstTerminal = await api.runTerminalCommand(
        firstId,
        `test "$PWD" = /workspace && printf '%s' '${firstProof}' > /workspace/shared-proof.txt && cat /workspace/shared-proof.txt`,
      );
      expect(firstTerminal).toContain(firstProof);

      const crossRead = await api.runTerminalCommand(
        secondId,
        [
          'set +e',
          `out=$(cat '${first.homeDir}/workspace/shared-proof.txt' 2>&1)`,
          'rc=$?',
          'printf "CROSS_READ_RC=%s\\n%s\\n" "$rc" "$out"',
          'test "$rc" -ne 0',
          'test "$PWD" = /workspace',
          `printf '%s' 'P144_SHARED_SECOND_${runId}' > /workspace/shared-proof.txt`,
          'cat /workspace/shared-proof.txt',
        ].join('; '),
      );
      expect(crossRead).toMatch(/CROSS_READ_RC=[1-9][0-9]*/);
      expect(crossRead).toContain('Permission denied');
      expect(crossRead).toContain(`P144_SHARED_SECOND_${runId}`);

      const interruptMarker = `P144_SHARED_INTERRUPT_${runId}`;
      const longTerminal = api.runTerminalCommand(
        firstId,
        `printf '%s\\n' '${interruptMarker}'; sleep 600`,
        undefined,
        120_000,
      );
      const isolatedExecutionId = await waitForIsolatedTerminalBinding(firstId);
      expect(isolatedExecutionId).toContain(firstId);
      const interruptResult = await api.interruptSession(firstId);
      expect(String((interruptResult as Record<string, unknown>).status || '')).toBe('completed');
      const interruptedTerminal = await longTerminal;
      expect(interruptedTerminal).toContain(interruptMarker);
      expect(interruptedTerminal).toContain('"exit_code": 130');
      const firstAfterInterrupt = placement(await api.adminSessionDetail(firstId), true);
      expect(firstAfterInterrupt.sandboxId).toBe(first.sandboxId);
      expect(firstAfterInterrupt.isolatedSessionId).toBe(first.isolatedSessionId);
      expect(firstAfterInterrupt.terminalIsolatedSessionId).not.toBe(
        first.terminalIsolatedSessionId,
      );
      const afterInterrupt = await api.runTerminalCommand(
        firstId,
        `printf '%s' 'P144_SHARED_AFTER_INTERRUPT_${runId}'`,
      );
      expect(afterInterrupt).toContain(`P144_SHARED_AFTER_INTERRUPT_${runId}`);

      await useRealSkillAndPlugin(api, firstId, `P144_SHARED_EXTENSIONS_${runId}`);
      await expectToolCards(page, firstId);
      expectPluginDescription(
        expectExtensionCommandMetadata(await api.getSession(firstId)), descriptionFragments,
      );
      await expectExtensionCommandMenu(page, descriptionFragments);

      const firstBeforeRestart = placement(await api.adminSessionDetail(firstId), true);
      const secondBeforeRestart = placement(await api.adminSessionDetail(secondId), true);
      await restartServerContainer(absoluteBaseUrl());

      const firstAfterRestart = placement(await api.adminSessionDetail(firstId), true);
      expect(firstAfterRestart).toEqual(firstBeforeRestart);
      const restored = await api.runTerminalCommand(
        firstId,
        'id -u; printf "HOME=%s\\nPWD=%s\\n" "$HOME" "$PWD"; test "$PWD" = /workspace; cat /workspace/shared-proof.txt',
      );
      expect(restored).toContain(String(first.uid));
      expect(restored).toContain(first.homeDir);
      expect(restored).toContain(firstProof);
      await expectModelTurn(api, firstId, `P144_SHARED_AFTER_RESTART_${runId}`);

      // Evict both process-local runtimes again, then delete without attaching
      // first. This exercises teardown from the persisted isolated-session id.
      await restartServerContainer(absoluteBaseUrl());
      await api.deleteSession(firstId);
      sessions.delete(firstId);
      const deletedRow = sessionDoc(firstId);
      expect(deletedRow?.deleted).toBe(true);
      expect(
        deletedRow?.undestroyed_sandbox_ids ?? [],
        'a successfully released child session is not a leaked sandbox',
      ).toEqual([]);

      const surviving = placement(await api.adminSessionDetail(secondId), true);
      expect(surviving).toEqual(secondBeforeRestart);
      const agentAfterDelete = await api.getAgent(agentId);
      expect(String(agentAfterDelete.sandbox_id || '')).toBe(first.sandboxId);
      const siblingProof = await api.runTerminalCommand(
        secondId,
        'id -u; printf "HOME=%s\\nPWD=%s\\n" "$HOME" "$PWD"; test "$PWD" = /workspace; cat /workspace/shared-proof.txt',
      );
      expect(siblingProof).toContain(String(second.uid));
      expect(siblingProof).toContain(second.homeDir);
      expect(siblingProof).toContain(`P144_SHARED_SECOND_${runId}`);
    } finally {
      // `cleanup()` runs from an afterEach on the passing path — see
      // fixtures/sessionCleanup.ts. It deletes the sessions newest-first and
      // then the agent, reporting every failure; that order and that reporting
      // are why the function is called rather than re-expressed.
    }
  });

  test('conversation tenancy acquires prepared boxes and rotates on preparation changes', async ({
    page,
    request,
  }) => {
    requiredConfiguration();
    const api = new AstraApi(request);
    const platform = new PlatformApi(request);
    const runId = Date.now();
    const agentName = `__e2e_p144_conversation_${runId}`;
    const model = await concreteModel(api, CONVERSATION_ENVIRONMENT);
    sessions = new Set<string>();
    agentId = '';
    additionalAgentIds = [];

    try {
      const created = await api.createAgent(
        extensionAgentCreateConfig(agentName, model, CONVERSATION_ENVIRONMENT),
      );
      agentId = String(created.agent_id || '');
      expect(agentId).not.toEqual('');
      const versionOne = await waitForPoolReady(platform, agentId);

      const firstId = await startConversationFromCard(page, agentName);
      sessions.add(firstId);
      const firstReady = await api.waitForSessionReady(firstId, READY_TIMEOUT_MS);
      const initialCommands = expectExtensionCommandMetadata(firstReady);
      const firstDetail = await api.adminSessionDetail(firstId);
      const first = placement(firstDetail, false);
      expect(
        first.sandboxId,
        'the first conversation must claim the exact prepared whole box',
      ).toBe(versionOne.sandboxId);
      const descriptionFragments = await installedPluginDescription(api, firstDetail);
      expectPluginDescription(initialCommands, descriptionFragments);
      expect((await api.getMessages(firstId)).messages).toHaveLength(0);
      await expectExtensionCommandMenu(page, descriptionFragments);
      await useRealSkillAndPlugin(api, firstId, `P144_CONVERSATION_V1_${runId}`);
      await expectToolCards(page, firstId);
      expectPluginDescription(
        expectExtensionCommandMetadata(await api.getSession(firstId)), descriptionFragments,
      );
      await expectExtensionCommandMenu(page, descriptionFragments);

      // Plan and claim B between A's two claims. Its different preparation
      // recipe must not become A's expected generation or acquisition source.
      const otherConfig = extensionAgentCreateConfig(
        `__e2e_p144_interleaved_${runId}`, model, CONVERSATION_ENVIRONMENT,
      );
      otherConfig.plugin_repos[0].depth = 2;
      const otherAgent = await api.createAgent(otherConfig);
      const otherAgentId = String(otherAgent.agent_id || '');
      expect(otherAgentId).not.toBe('');
      expect(otherAgentId).not.toBe(agentId);
      additionalAgentIds.push(otherAgentId);
      test.info().annotations.push({ type: 'interleaved_agent', description: otherAgentId });
      const otherPool = await waitForPoolReady(platform, otherAgentId);
      expect(versionOne.clientPoolName).not.toBeNull();
      expect(otherPool.clientPoolName).not.toBeNull();
      expect(otherPool.clientPoolName).not.toBe(versionOne.clientPoolName);
      expect(otherPool.runtimeGeneration).not.toBe(versionOne.runtimeGeneration);
      expect(otherPool.sandboxId).not.toBe(first.sandboxId);
      test.info().annotations.push({
        type: 'interleaved_prepared_pool', description: JSON.stringify(otherPool),
      });
      const otherCreated = await api.startConversation(otherAgentId);
      const otherId = otherCreated.session_id;
      sessions.add(otherId);
      test.info().annotations.push({ type: 'interleaved_session', description: otherId });
      const otherReady = await api.waitForSessionReady(otherId, READY_TIMEOUT_MS);
      expect(otherReady.agent_id).toBe(otherAgentId);
      expectExtensionCommandMetadata(otherReady);
      const other = placement(await api.adminSessionDetail(otherId), false);
      expect(other.sandboxId, 'B must claim its own previously prepared box').toBe(otherPool.sandboxId);
      expect((await api.getMessages(otherId)).messages).toHaveLength(0);

      // Wait for AstraBox to replace the box consumed above; the second Session
      // must acquire another prepared box, not cold-create.
      const refilledVersionOne = await waitForPoolReady(platform, agentId, {
        differentSandboxFrom: first.sandboxId,
      });
      expect(refilledVersionOne.runtimeGeneration).toBe(versionOne.runtimeGeneration);
      expect(refilledVersionOne.clientPoolName, 'B planning must not replace A\'s pool').toBe(
        versionOne.clientPoolName,
      );
      expect(refilledVersionOne.sandboxId).not.toBe(other.sandboxId);
      const secondCreated = await api.startConversation(agentId);
      const secondId = secondCreated.session_id;
      sessions.add(secondId);
      await api.waitForSessionReady(secondId, READY_TIMEOUT_MS);
      const second = placement(await api.adminSessionDetail(secondId), false);
      expect(
        second.sandboxId,
        'the second conversation must claim the replacement prepared whole box',
      ).toBe(refilledVersionOne.sandboxId);
      expect((await api.getSession(secondId)).agent_id).toBe(agentId);
      const otherAfterA = await platform.preparedRuntime(otherAgentId);
      expect(otherAfterA.runtime_generation, 'returning to A must retain B\'s generation').toBe(
        otherPool.runtimeGeneration,
      );
      expect(otherAfterA.client_pool_name).toBe(otherPool.clientPoolName);
      expect(placement(await api.adminSessionDetail(otherId), false)).toEqual(other);
      await test.info().attach('interleaved-prepared-pool-claims', {
        contentType: 'application/json',
        body: Buffer.from(JSON.stringify([
          { agentId, sessionId: firstId, prepared: versionOne, claimed: first },
          { agentId: otherAgentId, sessionId: otherId, prepared: otherPool, claimed: other },
          { agentId, sessionId: secondId, prepared: refilledVersionOne, claimed: second },
        ])),
      });

      const latest = await api.getAgent(agentId);
      // PUT replaces the authoring payload (the console submits the same full
      // draft), so keep every required Agent field while deepening the
      // plugin clone. Clone depth is a preparation input, so it rotates the
      // prepared runtime generation. A pure-configuration edit (system prompt,
      // managed MCP selection) keeps that generation by design.
      const preparationChanged = extensionAgentConfig(
        agentName, model, CONVERSATION_ENVIRONMENT,
      );
      preparationChanged.plugin_repos[0].depth = 2;
      const rotated = await api.updateAgent(agentId, {
        ...preparationChanged,
        version: Number(latest.version || 1),
      });
      expect(Number(rotated.version || 0)).toBeGreaterThan(Number(latest.version || 0));

      const versionTwo = await waitForPoolReady(platform, agentId, {
        differentRuntimeGenerationFrom: versionOne.runtimeGeneration,
      });
      expect(versionTwo.runtimeGeneration).not.toBe(versionOne.runtimeGeneration);

      const firstAfterRotation = placement(await api.adminSessionDetail(firstId), false);
      expect(firstAfterRotation.sandboxId).toBe(first.sandboxId);
      const oldSessionProof = await api.runTerminalCommand(
        firstId,
        `printf '%s' 'P144_OLD_SESSION_STABLE_${runId}'`,
      );
      expect(oldSessionProof).toContain(`P144_OLD_SESSION_STABLE_${runId}`);

      const thirdCreated = await api.startConversation(agentId);
      const thirdId = thirdCreated.session_id;
      sessions.add(thirdId);
      await api.waitForSessionReady(thirdId, READY_TIMEOUT_MS);
      const third = placement(await api.adminSessionDetail(thirdId), false);
      expect(
        third.sandboxId,
        'the first conversation after rotation must claim that generation\'s prepared whole box',
      ).toBe(versionTwo.sandboxId);
      expect(third.sandboxId).not.toBe(first.sandboxId);
      expect(third.sandboxId).not.toBe(second.sandboxId);
      await useRealSkillAndPlugin(api, thirdId, `P144_CONVERSATION_V2_${runId}`);
    } finally {
      // `cleanup()` runs from an afterEach on the passing path — see
      // fixtures/sessionCleanup.ts. It deletes the sessions newest-first and
      // then the agent, reporting every failure; that order and that reporting
      // are why the function is called rather than re-expressed.
    }
  });
});
