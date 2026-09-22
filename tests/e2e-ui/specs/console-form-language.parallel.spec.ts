/** Language changes update an open form, not just its surrounding shell. */
import { randomUUID } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly } from '../fixtures/sessionCleanup';

test.use({ locale: 'en-US' });
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    if (!sessionStorage.getItem('e2e-form-language-initialized')) {
      localStorage.setItem('astrabox-lang', 'en');
      sessionStorage.setItem('e2e-form-language-initialized', 'true');
    }
  });
});

let assistantId = '';
let deploymentOwner = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
  if (deploymentId) await new PlatformApi(request).deleteDeployment(deploymentOwner, deploymentId);
  assistantId = '';
  deploymentId = '';
});

async function switchLanguage(page: Page, language: 'en' | 'zh') {
  await page.locator('[data-slot="sidebar-footer"] [data-slot="dropdown-menu-trigger"]').click();
  const menu = page.locator('[data-slot="dropdown-menu-content"]');
  await expect(menu).toBeVisible();
  await menu.getByRole('combobox', { name: /^(Language|语言)$/ }).selectOption(language);
  await expect(page.locator('html')).toHaveAttribute('lang', language);
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
}

const copy = {
  agent: {
    heading: ['Basic information', '基本信息'],
    help: ['One line on what this agent does', '用一句话说明这个 Agent 的用途'],
    placeholder: ['Search project documents, compare solutions, and summarize findings.', '检索项目资料、比较方案，并整理有出处的结论。'],
  },
  environment: {
    heading: ['Basics', '基本信息'],
    help: ['One line on what this environment is good for', '一句话说明这个环境适合什么场景'],
  },
  assistant: {
    heading: ['Basics', '基本信息'],
    help: ["Optional. Shown on the user's assistant card.", '可选。会显示在用户的 Assistant 卡片上。'],
    placeholder: ['One line on what this assistant does', '一句话说明这个 Assistant 的用途'],
  },
} as const;

type AuthoringField = { key: string; label?: string };
type AuthoringEnvironment = {
  name: string;
  enabled?: boolean;
  engine_available?: boolean;
  supported_session_kinds?: string[];
  engine_options_schema?: AuthoringField[];
};

async function checkCompleteAgentCreateForm(page: Page, api: AstraApi) {
  const [schema, environments] = await Promise.all([
    api.data<{ fields: AuthoringField[] }>('GET', '/agent-configuration/schema'),
    api.data<AuthoringEnvironment[]>('GET', '/agent-configuration/environments'),
  ]);
  const selectable = environments.filter((environment) => environment.enabled !== false
    && environment.engine_available !== false
    && environment.supported_session_kinds?.includes('agent_chat') === true);
  expect(selectable.length, 'the live deployment must provide an Agent environment').toBeGreaterThan(0);
  expect(schema.fields.map((field) => field.key)).toEqual(expect.arrayContaining(['terminal_panel', 'diff_panel']));

  await page.goto(appPath('/manage/agents/new'), { waitUntil: 'domcontentloaded' });
  const form = page.locator('[data-slot="create-page"]');
  await expect(form).toBeVisible();
  await page.getByRole('button', { name: /^Show advanced settings/ }).click();
  await page.locator('#agent-new-visibility').selectOption('allowlist');

  for (const environment of selectable) {
    await test.step(`Complete Agent form for ${environment.name}`, async () => {
      await page.locator('#agent-new-environment_name').selectOption(environment.name);
      const nativeFields = environment.engine_options_schema ?? [];
      const fieldKeys = schema.fields.flatMap((field) => field.key === 'engine_options'
        ? nativeFields.map((block) => `engine_options.${block.key}`) : [field.key]);
      const expectedIds = [...fieldKeys, 'visibility', 'admins', 'allowed_user_ids']
        .map((key) => `agent-new-${key}-label`).sort();
      const labels = form.locator('[id^="agent-new-"][id$="-label"]');
      await expect.poll(async () => labels.evaluateAll((nodes) => nodes.map((node) => node.id).sort()),
        { message: 'every live schema field, native JSON block and access field must be rendered' }).toEqual(expectedIds);

      let englishLabels: Record<string, string> = {};
      for (const language of ['en', 'zh'] as const) {
        await switchLanguage(page, language);
        const terminalLabel = language === 'en' ? 'Terminal panel' : '终端面板';
        const diffLabel = language === 'en' ? 'Diff panel' : '文件差异面板';
        await expect(form.getByRole('switch', { name: terminalLabel, exact: true })).toBeVisible();
        await expect(form.getByRole('switch', { name: diffLabel, exact: true })).toBeVisible();
        const rendered = await labels.evaluateAll((nodes) => Object.fromEntries(
          nodes.map((node) => [node.id, node.textContent?.trim() ?? '']),
        ));
        for (const [id, label] of Object.entries(rendered)) {
          expect(label, `${environment.name}/${language}/${id} must have readable copy`).not.toEqual('');
          expect(label, `${environment.name}/${language}/${id} must not show an unresolved translation`)
            .not.toMatch(/(?:\w+:)?(?:agent_form|env_form|assistant_form)\.|^(?:misc|manage|common|chat):/);
        }
        if (language === 'en') englishLabels = rendered;
        for (const field of schema.fields.filter((field) => field.key !== 'engine_options' && !field.label)) {
          const id = `agent-new-${field.key}-label`;
          expect(rendered[id], `${field.key} must not use the wire key as its label`).not.toEqual(field.key);
          if (language === 'zh') {
            expect(rendered[id], `${field.key} must not fall back to English`).not.toEqual(englishLabels[id]);
          }
        }
        // Native JSON block names belong to the runtime, not the console catalogue.
        for (const block of nativeFields) {
          expect(rendered[`agent-new-engine_options.${block.key}-label`]).toBe(block.label ?? block.key);
        }
        await expect(page.locator('#agent-new-terminal_panel-help')).toContainText(language === 'en'
          ? 'does not restrict command execution or API access' : '不会限制命令执行或 API 访问');
        await expect(page.locator('#agent-new-diff_panel-help')).toContainText(language === 'en'
          ? "not the Agent's ability to edit files" : '不限制 Agent 编辑文件的能力');
        const headings = await form.getByRole('heading').allTextContents();
        expect(headings.length).toBeGreaterThan(0);
        expect(headings.join('\n')).not.toMatch(/agent_form\.groups\./);
      }
      await switchLanguage(page, 'en');
    });
  }
}

async function checkOpenForm(page: Page, path: string, prefix: string, kind: keyof typeof copy) {
  await page.goto(appPath(path), { waitUntil: 'domcontentloaded' });
  const field = page.locator(`[id="${prefix}-description"]`);
  const label = page.locator(`label[for="${prefix}-description"]`);
  const help = page.locator(`[id="${prefix}-description-help"]`);
  const expected = copy[kind];
  await expect(field).toBeVisible();
  await expect(label).toContainText('Description');
  await expect(page.getByRole('heading', { name: expected.heading[0], exact: true })).toBeVisible();
  const draft = `Unsaved 草稿 ${randomUUID()}\nKeep this exact text through both language changes.`;
  await field.fill(draft);
  const documentId = await page.evaluate(() => performance.timeOrigin);

  for (const [index, language] of [[1, 'zh'], [0, 'en']] as const) {
    await switchLanguage(page, language);
    await expect(label, 'field copy must follow the language without editing or navigating').toContainText(index ? '描述' : 'Description');
    await expect(page.getByRole('heading', { name: expected.heading[index], exact: true })).toBeVisible();
    await expect(help).toContainText(expected.help[index]);
    if ('placeholder' in expected) await expect(field).toHaveAttribute('placeholder', expected.placeholder[index]);
    await expect(field, 'switching language must not discard an unsaved draft').toHaveValue(draft);
    expect(await page.evaluate(() => performance.timeOrigin), 'language changes must not reload the document').toBe(documentId);
  }

  await switchLanguage(page, 'zh');
  await expect(label).toContainText('描述');
  await expect(field).toHaveValue(draft);
  // Explicit reload checks the persisted language, not persistence of an unsaved form.
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh');
  await expect(label).toContainText('描述');
  await expect(page.getByRole('heading', { name: expected.heading[1], exact: true })).toBeVisible();
  await switchLanguage(page, 'en');
  await expect(label).toContainText('Description');
}

test('Agent create and edit forms change language in place without losing drafts', async ({ page, request }) => {
  await checkCompleteAgentCreateForm(page, new AstraApi(request));
  await checkOpenForm(page, '/manage/agents/new', 'agent-new', 'agent');
  const agent = await new AstraApi(request).defaultAgent();
  await checkOpenForm(page, `/manage/agents/${agent.agent_id}`, 'agent', 'agent');
});

test('Environment create and edit forms change language in place without losing drafts', async ({ page, request }) => {
  await checkOpenForm(page, '/manage/environments/new', 'environment-new', 'environment');
  const agent = await new AstraApi(request).defaultAgent();
  await checkOpenForm(page, `/manage/environments/${encodeURIComponent(String(agent.environment_name))}`, 'environment', 'environment');
});

test('Assistant create and edit forms change language in place without losing drafts', async ({ page, request }) => {
  await checkOpenForm(page, '/manage/assistants/new', 'assistant-new', 'assistant');
  const api = new AstraApi(request);
  const assistant = await api.createAssistant({
    display_name: `Language acceptance ${randomUUID()}`,
    environment_name: await api.assistantEnvironmentName(),
  });
  assistantId = assistant.assistant_id;
  await checkOpenForm(page, `/manage/assistants/${assistantId}`, 'assistant', 'assistant');
});

test('Deployment language changes preserve unsaved schedule configuration without reloading it', async ({ page, request }) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  deploymentOwner = (await api.defaultAgent()).agent_id;
  const deployment = await platform.createDeployment(deploymentOwner, {
    scene: 'schedule', name: `Language acceptance ${randomUUID()}`,
    prompt_prefix: 'Saved baseline', schedule: { cron: '0 0 1 1 *', timezone: 'UTC' },
  });
  deploymentId = deployment.deployment_id;
  const disabled = await platform.updateDeployment(deploymentOwner, deploymentId, { enabled: false });
  expect(disabled.enabled, 'the authoring test must not trigger an agent run').toBe(false);
  await page.goto(appPath(`/manage/deployments/${deploymentId}`), { waitUntil: 'domcontentloaded' });
  const prompt = page.locator('#deployment-schedule-prompt_prefix');
  await expect(prompt).toHaveValue('Saved baseline');
  const draft = `Unsaved schedule 草稿 ${randomUUID()}`;
  await prompt.fill(draft);
  await page.locator('#deployment-schedule-cron').fill('0 1 1 1 *');
  const reads: string[] = [];
  page.on('request', (req) => {
    if (req.method() === 'GET' && new URL(req.url()).pathname === apiPath('/admin/deployments')) reads.push(req.url());
  });
  const documentId = await page.evaluate(() => performance.timeOrigin);
  for (const language of ['zh', 'en'] as const) {
    await switchLanguage(page, language);
    await expect(page.locator('label[for="deployment-schedule-prompt_prefix"]')).toContainText(language === 'zh' ? '提示词' : 'Prompt');
    expect(reads, 'changing presentation language must not reload the saved configuration over the draft').toEqual([]);
    await expect(prompt).toHaveValue(draft);
    await expect(page.locator('#deployment-schedule-cron')).toHaveValue('0 1 1 1 *');
    expect(await page.evaluate(() => performance.timeOrigin)).toBe(documentId);
  }
  const persisted = (await platform.listDeployments(deploymentOwner)).find((item) => item.deployment_id === deploymentId);
  expect(persisted?.prompt_prefix, 'language switching does not save configuration either').toBe('Saved baseline');
  expect(persisted?.schedule?.cron).toBe('0 0 1 1 *');
});
