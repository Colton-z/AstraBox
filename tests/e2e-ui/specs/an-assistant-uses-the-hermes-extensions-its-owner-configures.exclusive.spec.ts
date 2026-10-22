/**
 * E2E: an Assistant's owner extends Hermes with a Skill, a Hermes plugin and a
 * native configuration key on the Assistant's record page, and a conversation
 * uses all three; removed there, a new conversation has none of them.
 *
 * The sources are public and pinned: Anthropic's `brand-guidelines` Skill,
 * whose accent colour the model can only quote by loading it, and Nous
 * Research's reference `plugin-tool-example`, which registers a `text_stats`
 * tool. A second plugin, `gonogo` at the commit Nous Research's reviewed
 * plugin catalog pins, declares a Python dependency (`gonogo-eval`) that Hermes
 * does not ship; its `gonogo_decide` tool answers with a verdict only when
 * Hermes' installer fetched that dependency from PyPI, through the Assistant
 * RuntimeProfile's limited network. The configuration is a Hermes personality the owner defines and
 * selects, whose text Hermes puts in each new session's system prompt. The
 * Assistant's own timezone, which Hermes names in the "Conversation started"
 * line of every system prompt, is checked alongside, and so is the Skill
 * AstraBox ships in the image, `astrabox-proactive`. The evidence is the tool
 * calls the turn streams, the answer they produce, and Hermes' own profile and
 * managed scope in the box. Each save is read back through the API.
 *
 * This journey provisions a workspace and restarts its backend, so it stays in
 * the suite contract's one-worker serial group.
 */
import { randomUUID } from 'node:crypto';
import { expect, test, type Locator, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { requireSandboxHandle, sandboxExec } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const SKILL = 'https://github.com/anthropics/skills.git@683bc88e56f3e09ba94f7055977f3d3aa499f202#skills/brand-guidelines';
const PLUGIN = {
  kind: 'hermes-plugin',
  url: 'https://github.com/NousResearch/hermes-example-plugins.git',
  protocol: 'https',
  sha: 'fb6726740efccccd56ddb117c25f1d1f3d4edd65',
  paths: ['plugin-tool-example'],
};
const PLUGIN_KEY = 'astrabox/plugin-tool-example';
const DEPENDENT_PLUGIN = {
  kind: 'hermes-plugin',
  url: 'https://github.com/keppy/hermes-plugin-gonogo.git',
  protocol: 'https',
  sha: '3f9945d3235d204d74420544fbd11a08121cd6d2',
};
const VERDICTS = ['AUTOMATE WITH REVIEW', 'AUTOMATE', 'ASSIST ONLY', 'DO NOT AUTOMATE', 'INSUFFICIENT EVIDENCE'];
const ZONE = 'Asia/Tokyo';
const PERSONALITY = 'orchid';

test.use({ locale: 'en-US' });

const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

/** Hermes' profile and managed scope as the box holds them. */
function profileFacts(output: string): Record<string, string> {
  return Object.fromEntries(
    output.split('\n').filter((line) => line.includes('=')).map((line) => {
      const at = line.indexOf('=');
      return [line.slice(0, at).trim(), line.slice(at + 1).trim()];
    }),
  );
}

/** Save one record-page card and require the server to accept it. */
async function saveCard(page: Page, card: Locator, id: string): Promise<void> {
  const saved = page.waitForResponse(
    (response) => response.request().method() === 'PATCH'
      && new URL(response.url()).pathname === apiPath(`/assistants/${id}`),
  );
  await card.getByRole('button', { name: 'Save', exact: true }).click();
  const response = await saved;
  expect(response.status(), `Save must be accepted: ${(await response.text()).slice(0, 300)}`).toBe(200);
}

async function readProfile(api: AstraApi, id: string): Promise<Record<string, string>> {
  const box = String((await api.waitForWorkspaceReady(id, 90_000)).current_sandbox_id || '');
  expect(box).not.toEqual('');
  return profileFacts(sandboxExec(await requireSandboxHandle(api, box), [
    'h=$(find /home/conversations -mindepth 2 -maxdepth 2 -name .hermes -type d | head -n 1)',
    'echo "skill=$(test -f "$h/skills/astrabox/brand-guidelines/SKILL.md" && echo present || echo absent)"',
    'echo "plugin=$(test -f "$h/plugins/astrabox/plugin-tool-example/plugin.yaml" && echo present || echo absent)"',
    'echo "dependent=$(test -f "$h/plugins/astrabox/hermes-plugin-gonogo/plugin.yaml" && echo present || echo absent)"',
    'if test -f "$h/plugins/astrabox/hermes-plugin-gonogo/plugin.yaml"; then',
    'HERMES_HOME="$h" "$HERMES_VENV/bin/python" - <<\'PY\'',
    'import subprocess',
    'from pm.environments import selected_venv, venv_python',
    'from pm.paths import repo_root',
    'python = venv_python(selected_venv(repo_root()))',
    `version = subprocess.check_output([str(python), "-c", "import gonogo; import importlib.metadata; print(importlib.metadata.version('gonogo-eval'))"], text=True).strip()`,
    'print("dependency=" + version)',
    'PY',
    'fi',
    `echo "enabled=$(grep -c '${PLUGIN_KEY}' "$h/config.yaml")"`,
    'echo "managed=$(cat /etc/astrabox/hermes/config.yaml 2>/dev/null | tr "\\n" " ")"',
  ].join('\n')));
}

test('an Assistant uses the Skill, Hermes plugin and configuration its owner adds, and drops them when removed', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const id = randomUUID().slice(0, 8);
  const codename = `ORCHID-${id}`;
  const runtimeProfileName = await api.assistantEnvironmentName();
  const assistant = await api.createAssistant({
    display_name: `__e2e_assistant_extensions_${id}`,
    runtime_profile_name: runtimeProfileName,
    model_config_override: { model_name: await api.assistantModelName(runtimeProfileName) },
    timezone: ZONE,
  });
  assistantId = String(assistant.assistant_id || '');

  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* the en-US navigator locale still applies */
    }
  });
  await page.goto(appPath(`/manage/assistants/${assistantId}`));
  const packages = page.getByRole('textbox', { name: 'Packages', exact: true });
  const card = page.locator('[data-slot="card"]').filter({ has: packages });
  await expect(card, 'Hermes declares packages, so the record page offers them').toBeVisible();
  const skills = card.getByRole('textbox', { name: 'Skills', exact: true });
  const config = card.getByRole('textbox', { name: 'Hermes config.yaml', exact: true });
  await skills.fill(JSON.stringify([SKILL]));
  await skills.blur();
  await packages.fill(JSON.stringify([PLUGIN, DEPENDENT_PLUGIN]));
  await packages.blur();
  const personality = {
    agent: { personalities: { [PERSONALITY]: `Your codename is ${codename}.` } },
    display: { personality: PERSONALITY },
  };
  await config.fill(JSON.stringify(personality));
  await config.blur();
  await saveCard(page, card, assistantId);
  expect(await api.getAssistant(assistantId)).toMatchObject({
    skills: [SKILL],
    packages: [
      expect.objectContaining({ kind: PLUGIN.kind, url: PLUGIN.url, sha: PLUGIN.sha, paths: PLUGIN.paths }),
      expect.objectContaining({ kind: DEPENDENT_PLUGIN.kind, url: DEPENDENT_PLUGIN.url, sha: DEPENDENT_PLUGIN.sha }),
    ],
    engine_options: { config: personality },
    timezone: ZONE,
  });

  const first = String((await api.startAssistantConversation(assistantId)).session_id || '');
  sessions.push(first);
  await api.waitForSessionReady(first);
  const used = await api.sendTurn(first, [
    'Do these six things, then reply with exactly one line in the format',
    'STATS=<number>; ACCENT=<hex>; ZONE=<name>; CODENAME=<codename|NONE>; WEEKLY=<cron>; VERDICT=<verdict|ERROR>',
    'and nothing else.',
    '1. Call the text_stats tool on the text "red green red blue"; STATS is its unique word count.',
    '2. Load the skill brand-guidelines with skill_view; ACCENT is its Orange accent hex code.',
    '3. ZONE is the IANA timezone named in the "Conversation started" line of your system prompt.',
    '4. CODENAME is the codename your system prompt gives you, or NONE if it gives none.',
    '5. Load the skill astrabox-proactive with skill_view; WEEKLY is the schedule its recipes give a weekly review.',
    '6. Call the gonogo_decide tool once with passed 47, total 50 and target 0.95; VERDICT is the verdict it',
    'returns, or ERROR if it returns an error.',
  ].join('\n'), 150_000);
  expect(used.errorText).toBeFalsy();
  expect(used.toolNames).toContain('text_stats');
  expect(used.toolNames).toContain('skill_view');
  expect(used.text).toContain('STATS=3');
  expect(used.text.toLowerCase()).toContain('accent=#d97757');
  expect(used.text).toContain(`ZONE=${ZONE}`);
  expect(used.text).toContain(`CODENAME=${codename}`);
  expect(used.text, "AstraBox's own Skill is there beside the owner's").toContain('WEEKLY=0 17 * * 5');
  expect(used.toolNames).toContain('gonogo_decide');
  expect(
    VERDICTS.some((verdict) => used.text.includes(`VERDICT=${verdict}`)),
    `gonogo_decide answers with a verdict once its PyPI dependency is installed: ${used.text}`,
  ).toBe(true);

  const installed = await readProfile(api, assistantId);
  expect(installed).toMatchObject({
    skill: 'present', plugin: 'present', dependent: 'present', enabled: '1',
  });
  expect(installed.dependency, 'the supplier-selected environment imports the real plugin dependency').toMatch(/^\d+\.\d+/);
  expect(installed.managed).toContain(`personality: ${PERSONALITY}`);

  await skills.fill('[]');
  await skills.blur();
  await packages.fill('[]');
  await packages.blur();
  await config.fill('');
  await config.blur();
  await saveCard(page, card, assistantId);
  expect(await api.getAssistant(assistantId)).toMatchObject({ skills: [], packages: [], engine_options: {} });
  const second = String((await api.startAssistantConversation(assistantId)).session_id || '');
  sessions.push(second);
  await api.waitForSessionReady(second);
  const removed = await api.sendTurn(second, [
    'Without calling any tool, reply with exactly one line in the format',
    'TOOL=<YES|NO>; CODENAME=<codename|NONE> and nothing else.',
    'TOOL is YES only if a tool named text_stats is in your tool list.',
    'CODENAME is the codename your system prompt gives you, or NONE if it gives none.',
  ].join('\n'), 120_000);
  expect(removed.errorText).toBeFalsy();
  expect(removed.toolNames).toEqual([]);
  expect(removed.text).toContain('TOOL=NO');
  expect(removed.text).not.toContain(codename);

  const released = await readProfile(api, assistantId);
  expect(released).toMatchObject({
    skill: 'absent', plugin: 'absent', dependent: 'absent', enabled: '0', managed: '',
  });
});
