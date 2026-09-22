/**
 * E2E: agent conversations provision correctly with and without skills.
 *
 * A no-skills conversation must reach READY without a skills link or stale
 * manifest. The opt-in with-skills case must link `${config_dir}/skills` to the
 * runtime cache, expose the declared manifest, and make it readable to the
 * conversation's Linux user.
 *
 * Conversations are started from the agent card and must render a reply. The
 * filesystem assertions use root `docker exec` because the console has no
 * filesystem surface; `runuser` separately verifies readability as the runtime
 * user. The with-skills case requires real cloneable skill descriptors supplied
 * through ASTRABOX_E2E_LIFECYCLE_WITH_SKILLS_DESCRIPTOR.
 */
import { test, expect } from '@playwright/test';

import { AstraApi, type AdminSessionRecord } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import {
  requireSandboxHandle,
  sandboxExec,
  type SandboxHandle,
} from '../fixtures/sandboxOps';

const RUN_ID = new Date().toISOString().replace(/[:.]/g, '-');
const READY_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 180_000);
// Two agent creations, up to two conversations provisioning their own sandbox and
// up to two turns sit above the 240s suite default; same lifecycle-sized,
// env-tunable budget the sibling per-session specs carry.
// One short, tool-free reply rendered into the transcript.
const REPLY_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_SKILLS_REPLY_TIMEOUT_MS', 240_000);

// The sandbox-local runtime skill cache the bootstrap symlinks `${config_dir}/skills`
// at (conversation_identity.AGENT_RUNTIME_SKILL_CACHE_DIR). Verified against source.
const SKILL_CACHE_DIR = '/opt/conversation-runtime/claude-skills-cache';
const MANIFEST_FILENAME = '.skill-manifest';
const PROBE_MARKER = 'ASTRABOX_SKILLS_PROBE:';

// The with-skills path accepts comma-separated, sandbox-cloneable git skill
// descriptors (`<repo>@<ref>#<path>`). When unset, only the no-skills path runs
// and the opt-in case is annotated as skipped.
const WITH_SKILLS_DESCRIPTORS = String(process.env.ASTRABOX_E2E_LIFECYCLE_WITH_SKILLS_DESCRIPTOR || '')
  .split(',')
  .map((item) => item.trim())
  .filter(Boolean);

interface SkillsProbe {
  target_kind: 'absent' | 'symlink' | 'directory' | 'other' | string;
  target_exists: boolean;
  link_target: string;
  cache_dir: string;
  manifest_present: boolean;
  cache_manifest_present: boolean;
  manifest_items: string[];
}

/**
 * The `${config_dir}/skills` view inside a conversation's sandbox — inspected
 * through the runtime-aware sandbox handle. It reads the link/manifest AS ROOT
 * and checks the manifest is readable
 * AS THE CONVERSATION USER via `runuser -u <user> -- test -r` (which a root exec
 * can do because the per-session terminal already runs as that user).
 */
async function probeSkillsTarget(
  sandbox: SandboxHandle,
  configDir: string,
  linuxUser: string,
): Promise<SkillsProbe> {
  const script = [
    `config='${configDir}'`,
    `user='${linuxUser}'`,
    'target="$config/skills"',
    `cache='${SKILL_CACHE_DIR}'`,
    `manifest="$target/${MANIFEST_FILENAME}"`,
    'if [ -L "$target" ]; then kind=symlink; link="$(readlink "$target" 2>/dev/null || true)";',
    'elif [ -d "$target" ]; then kind=directory; link="";',
    'elif [ -e "$target" ]; then kind=other; link="";',
    'else kind=absent; link=""; fi',
    'if [ -e "$target" ] || [ -L "$target" ]; then exists=true; else exists=false; fi',
    // manifest_present == exists as a file AND is readable AS the conversation user
    // (root can always stat it, so `runuser -u <user> -- test -r` is the load-bearing
    // check — directly exercising the "readable as user" property).
    'if [ -f "$manifest" ] && runuser -u "$user" -- test -r "$manifest" 2>/dev/null; then mpresent=true; mb64="$(base64 -w0 < "$manifest" 2>/dev/null || base64 < "$manifest" | tr -d \'\\n\')"; else mpresent=false; mb64=""; fi',
    `if [ -f "$cache/${MANIFEST_FILENAME}" ]; then cpresent=true; else cpresent=false; fi`,
    `printf '${PROBE_MARKER}{"target_kind":"%s","target_exists":%s,"link_target":"%s","cache_dir":"%s","manifest_present":%s,"manifest_b64":"%s","cache_manifest_present":%s}\\n' "$kind" "$exists" "$link" "$cache" "$mpresent" "$mb64" "$cpresent"`,
  ].join('\n');

  let lastRaw = '';
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    let stdout = '';
    try {
      stdout = sandboxExec(sandbox, script);
    } catch (error) {
      const err = error as { stdout?: string; stderr?: string; message?: string };
      // A non-zero exit still carries the marker on stdout; keep whatever came back.
      // An error with NEITHER stream is not an exec failure — it is the helper
      // saying it could not reach the box at all, and swallowing its message
      // leaves `raw tail=` empty, which reads as "the box answered nothing".
      stdout = String(err.stdout ?? '');
      const streams = `${stdout}\n${String(err.stderr ?? '')}`.trim();
      lastRaw = streams || String(err.message ?? 'probe failed with no output');
    }
    if (!lastRaw) lastRaw = stdout;
    const markerAt = stdout.indexOf(PROBE_MARKER);
    if (markerAt !== -1) {
      const jsonText = stdout.slice(markerAt + PROBE_MARKER.length).split('\n')[0].trim();
      const parsed = JSON.parse(jsonText) as SkillsProbe & { manifest_b64?: string };
      const manifestText = parsed.manifest_b64
        ? Buffer.from(String(parsed.manifest_b64), 'base64').toString('utf-8')
        : '';
      const manifest_items = manifestText
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean);
      return { ...parsed, manifest_items };
    }
    if (attempt < 2) await new Promise((r) => setTimeout(r, 2_000));
  }
  throw new Error(
    `skills probe emitted no ${PROBE_MARKER} sentinel for sandbox ${sandbox.sandboxId}; ` +
      `raw tail=${lastRaw.slice(-600)}`,
  );
}

/** Assert and return the Linux identity used by the conversation sandbox. */
function expectRuntimeIdentity(detail: AdminSessionRecord, label: string): { linuxUser: string; configDir: string } {
  const identity = (detail.runtime_identity || {}) as Record<string, unknown>;
  expect(detail.runtime_identity, `${label} should expose runtime_identity`).toBeTruthy();
  const linuxUser = String(identity.linux_user || '').trim();
  const configDir = String(identity.config_dir || '').trim();
  expect(linuxUser, `${label} should expose runtime_identity.linux_user`).not.toEqual('');
  expect(configDir, `${label} should expose runtime_identity.config_dir`).not.toEqual('');
  return { linuxUser, configDir };
}

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: afterEach hooks run in the
// order they are registered.
//
// `sessionIds` IS the tracker's array, so every existing push into it now
// registers the session for outcome-driven cleanup unchanged.
const sessionIds = trackSessions();
const agentIds: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const agentId of agentIds.splice(0, agentIds.length)) await api.deleteAgent(agentId);
});

test('agent conversation creation works with and without agent skills', async ({ page, request }) => {
  const api = new AstraApi(request);

  // Clone the seeded agent's environment + model so a throwaway agent provisions
  // on the same stack; only the `skills` field varies between the two halves.
  const base = await api.defaultAgent();
  const environmentName = String(base.environment_name || '').trim();
  expect(environmentName, 'seeded agent should expose environment_name to clone').not.toEqual('');
  // The seeded agents carry model:"" (resolved from the gateway at run time), but
  // the create validator requires a NON-BLANK model, so source a CONCRETE id from
  // the environment catalogue — skipping litellm wildcard routing keys ("claude-*",
  // "gpt-*", "gemini-*"), which are not real model ids. Falls back to the documented
  // stack model (deepseek-chat) only if the catalogue offers no concrete id.
  let model = String(base.model || '').trim();
  if (!model || model.includes('*')) {
    const models = await api.listEnvironmentModels(environmentName);
    model = models.find((m) => m && !m.includes('*')) || 'deepseek-chat';
  }
  expect(model, 'a model id is required to mint a throwaway agent').not.toEqual('');

  const withSkillsConfigured = WITH_SKILLS_DESCRIPTORS.length > 0;
  if (!withSkillsConfigured) {
    test.info().annotations.push({
      type: 'skip',
      description:
        'with-skills path skipped: set ASTRABOX_E2E_LIFECYCLE_WITH_SKILLS_DESCRIPTOR to a real, ' +
        'sandbox-cloneable git skill descriptor (<repo>@<ref>#<path>) to exercise it. The no-skills ' +
        'path always runs.',
    });
  }

  /**
   * Start a conversation the way the console offers it: the agent's card on the
   * home page, and its one button. The fresh navigation is not a convenience —
   * AgentHome fetches the agent list once on mount, so re-opening the page IS how
   * a user meets an agent that was just arranged for.
   */
  const startConversationFromCard = async (agentName: string): Promise<string> => {
    await page.goto(appPath('/agents'));
    const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);
    await expect(card, `the picker must offer ${agentName}`).toBeVisible({ timeout: 30_000 });
    // One button per card — the badge, the title and the model line are all spans.
    await card.getByRole('button').click();
    // The console navigates to the conversation it just created, and that route
    // IS the user-visible identity of the conversation.
    await page.waitForURL((url) => /\/sessions\/[^/]+$/.test(url.pathname), { timeout: 120_000 });
    const sessionId = new URL(page.url()).pathname.split('/').filter(Boolean).pop() || '';
    expect(sessionId, 'starting a conversation must open its own /sessions/<id> route').not.toEqual('');
    // Recorded before any further assertion can throw, so the box is torn down
    // even if the conversation goes wrong from here.
    sessionIds.push(sessionId);
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
    return sessionId;
  };

  /** The run view's own header pill — the sidebar renders one per conversation row. */
  const runPill = () => page.getByTestId('run-view').getByTestId('status-pill').first();

  /** Send from the composer and wait for one more rendered assistant bubble. */
  const sendAndReadReply = async (prompt: string, budgetMs: number) => {
    const before = await page.getByTestId('assistant-message').count();
    await page.getByTestId('composer-prompt').fill(prompt);
    await page.getByTestId('composer-submit').click();
    await expect(page.getByTestId('user-message').last()).toContainText(prompt.slice(0, 8), {
      timeout: 30_000,
    });
    // Count, not text: the model's wording is its own business, and a spec that
    // pins wording fails on a model that is behaving correctly.
    await expect
      .poll(() => page.getByTestId('assistant-message').count(), { timeout: budgetMs })
      .toBeGreaterThan(before);
    const reply = page.getByTestId('assistant-message').last();
    await expect(reply).not.toBeEmpty();
    // A bubble that grew the count is not yet a reply: a FAILED turn renders its
    // error INTO the transcript as an assistant message, so "one more non-empty
    // bubble" is satisfied by exactly the outcome under test — a conversation
    // whose config directory the skills wiring left unusable.
    await expect(reply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
    // And the header settles — a turn that delivers text but leaves the screen
    // looking live is still a broken screen.
    await expect(runPill()).toHaveAttribute('data-pulse', 'false', { timeout: 60_000 });
    return reply;
  };

  try {
    await test.step('create agent conversation from a no-skills agent', async () => {
      const agentName = `e2e skills no-skills ${RUN_ID}`;
      const agent = await api.createAgent({
        name: agentName,
        model,
        environment_name: environmentName,
        skills: [],
      });
      agentIds.push(agent.agent_id);

      // The user starts the conversation from the picker; the page is what
      // created it, and the URL it landed on is what names it.
      const sessionId = await startConversationFromCard(agentName);
      // On-screen READY: the conversation acquired its own runtime. Startup
      // settles state=READY together with sandbox_id, so this is the
      // user-visible half of "creation reached READY with a per-session
      // sandbox" — half, because the pill's own fallback prints READY for a
      // detail with no state (see the header note).
      await expect(
        runPill(),
        'no-skills agent conversation should read READY in its own header',
      ).toHaveAttribute('data-state', 'READY', { timeout: READY_BUDGET_MS });

      // API for the facts the page cannot carry: the state as the projection
      // records it (the pill would paint READY for a stateless detail), the
      // sandbox_id, and the runtime identity the probe addresses the box by.
      const ready = await api.waitForSessionReady(sessionId, READY_BUDGET_MS);
      expect(ready.state, 'no-skills agent conversation should become READY').toBe('READY');
      const sandboxId = String(ready.sandbox_id || '').trim();
      expect(sandboxId, 'no-skills conversation should own a per-session sandbox_id').not.toEqual('');
      const sandbox = await requireSandboxHandle(api, sandboxId);

      const adminDetail = await api.adminSessionDetail(sessionId);
      const { linuxUser, configDir } = expectRuntimeIdentity(adminDetail, 'no-skills agent conversation');
      // Probed BEFORE the turn: "nothing wired a skills target" is a claim about
      // creation, and an agent that has run could create the directory itself.
      const probe = await probeSkillsTarget(sandbox, configDir, linuxUser);
      const probeText = JSON.stringify(probe);
      // A no-skills conversation must not wire the shared agent skill cache in.
      expect(probe.target_kind, `no-skills conversation must not create a skills target: ${probeText}`).toBe('absent');
      expect(probe.target_exists, `no-skills conversation skills target should stay absent: ${probeText}`).toBe(false);
      expect(probe.manifest_present, `no-skills conversation should not expose a stale skills manifest: ${probeText}`).toBe(false);

      // …and the conversation the user opened genuinely serves. READY is not
      // conversable; this is the difference, read in the transcript.
      await sendAndReadReply('请简短回复一句话，不要使用工具。', REPLY_BUDGET_MS);
    });

    // With-skills half only when a real skill-declaring descriptor is configured;
    // otherwise the sandbox clone would fail and the conversation never reaches READY.
    if (withSkillsConfigured) {
      await test.step('create agent conversation from a with-skills agent', async () => {
        const agentName = `e2e skills with-skills ${RUN_ID}`;
        const agent = await api.createAgent({
          name: agentName,
          model,
          environment_name: environmentName,
          skills: WITH_SKILLS_DESCRIPTORS,
        });
        agentIds.push(agent.agent_id);

        const sessionId = await startConversationFromCard(agentName);
        // READY on screen is also the evidence the declared skills CLONED: a
        // descriptor the sandbox cannot fetch leaves the conversation stuck short
        // of READY, which the pill is where a user would watch for.
        await expect(
          runPill(),
          'with-skills agent conversation should read READY in its own header',
        ).toHaveAttribute('data-state', 'READY', { timeout: READY_BUDGET_MS });

        const ready = await api.waitForSessionReady(sessionId, READY_BUDGET_MS);
        expect(ready.state, 'with-skills agent conversation should become READY').toBe('READY');
        const sandboxId = String(ready.sandbox_id || '').trim();
        expect(sandboxId, 'with-skills conversation should own a per-session sandbox_id').not.toEqual('');
        const sandbox = await requireSandboxHandle(api, sandboxId);

        const adminDetail = await api.adminSessionDetail(sessionId);
        const { linuxUser, configDir } = expectRuntimeIdentity(adminDetail, 'with-skills agent conversation');
        const probe = await probeSkillsTarget(sandbox, configDir, linuxUser);
        const probeText = JSON.stringify(probe);
        expect(probe.target_kind, `with-skills conversation should expose skills as a symlink: ${probeText}`).toBe('symlink');
        expect(probe.link_target, `with-skills symlink should point at the agent skill cache: ${probeText}`).toBe(SKILL_CACHE_DIR);
        expect(probe.link_target, `with-skills symlink target should equal the reported cache_dir: ${probeText}`).toBe(probe.cache_dir);
        expect(probe.cache_manifest_present, `agent skill cache should carry a manifest: ${probeText}`).toBe(true);
        expect(probe.manifest_present, `conversation user should read the skills manifest through the symlink: ${probeText}`).toBe(true);
        expect(probe.manifest_items, `conversation skills manifest should match the declared skills: ${probeText}`).toEqual(WITH_SKILLS_DESCRIPTORS);

        // The wiring is only worth having if the conversation still works with it
        // — a config directory the skill cache broke fails here, one step past
        // the READY the pill showed.
        await sendAndReadReply('请简短回复一句话，不要使用工具。', REPLY_BUDGET_MS);
      });
    }
  } finally {
    // Nothing here is released on a failing run. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why the unit is the whole block.
  }
});
