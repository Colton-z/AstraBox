import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

import { expect, test } from '@playwright/test';

import { loginViaOidc } from '../fixtures/oidcLogin';
import { AstraApi, messageText } from '../fixtures/astraApi';


test.use({ trace: 'off', video: 'off' });

test('an independent public OIDC application infers as its employee with native governance and live DSH refresh', async ({ browser, request }, testInfo) => {
  // Match Playwright's requireOrImport native-ESM path: its CommonJS hook
  // cannot execute import.meta in the unmodified desktop example.
  const loadExample = (name: string) => eval(`import(${JSON.stringify(pathToFileURL(path.resolve(__dirname, '../../../examples/oidc-dsh', name)).href)})`);
  const { signIn, refresh } = await loadExample('login.mjs');
  const { DesktopSession } = await loadExample('desktop-session.mjs');
  const fixture = JSON.parse(await readFile(String(process.env.ASTRABOX_E2E_MODEL_OIDC_FIXTURE_FILE), 'utf8'));
  const control = (action: string, payload: unknown = {}): Promise<any> => new Promise((resolve, reject) => {
    const child = spawn('python3', [path.resolve(__dirname, '../fixtures/oidcModelGateway.py'), fixture.control_file, action]);
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.on('error', reject);
    child.on('exit', code => code === 0 ? resolve(JSON.parse(stdout)) : reject(new Error(`Fixture ${action} failed: ${stderr}`)));
    child.stdin.end(JSON.stringify(payload));
  });
  const context = await browser.newContext();
  const page = await context.newPage();
  let session: InstanceType<typeof DesktopSession> | undefined;
  const usedTokens = new Set<string>();
  const evidence: Record<string, unknown> = { fixture: fixture.identity, protocols: [], failed: true };
  try {
    const { config, tokens } = await test.step('public-client browser PKCE without a desktop secret', async () => signIn({
      issuer: fixture.issuer, clientId: fixture.client_id, redirectURI: fixture.redirect_uri,
      openBrowser: async (url: string) => {
        expect(new URL(url).searchParams.get('code_challenge_method')).toBe('S256');
        expect(new URL(url).searchParams.has('client_secret')).toBe(false);
        await page.goto(url);
        await page.locator('input[name="username"], input[placeholder*="username" i], input[type="text"]').first().fill(fixture.username);
        await page.locator('input[type="password"]').first().fill((await readFile(fixture.password_file, 'utf8')).trim());
        await page.getByRole('button', { name: 'Sign In', exact: true }).click();
        await page.waitForURL(fixture.redirect_uri + '*');
      },
    }));
    usedTokens.add(tokens.access_token);
    const bearer = (token: string) => ({ Authorization: `Bearer ${token}` });
    const models = (token: string) => request.get(`${fixture.gateway}/v1/models`, { headers: bearer(token) });
    const chat = (token: string, extra: Record<string, unknown> = {}) => request.post(`${fixture.gateway}/v1/chat/completions`, {
      headers: bearer(token), data: { model: fixture.model, messages: [{ role: 'user', content: 'Reply with the single word maple.' }], max_tokens: 1024, ...extra },
    });
    const employee = 'oidc:' + createHash('sha256').update(JSON.stringify([fixture.issuer, fixture.subject])).digest('hex');
    expect((await models(tokens.access_token)).status()).toBe(200);
    const list = await (await models(tokens.access_token)).json();
    expect(list.data.map((item: { id: string }) => item.id)).toEqual([fixture.model]);
    const ordinary = await chat(tokens.access_token);
    expect(ordinary.status(), 'a direct employee Access Token must reach actual inference').toBe(200);
    const ordinaryBody = await ordinary.json();
    evidence.ordinary = ordinaryBody;
    expect(ordinaryBody.choices[0].finish_reason).not.toBe('length');
    expect(ordinaryBody.choices[0].message.content).toContain('maple');
    const stream = await chat(tokens.access_token, { stream: true, stream_options: { include_usage: true } });
    expect(stream.status()).toBe(200);
    expect(stream.headers()['content-type']).toContain('text/event-stream');
    const streamedBody = await stream.text();
    expect(streamedBody).toContain('[DONE]');
    expect(streamedBody).toContain('maple');
    evidence.protocols = ['chat/completions', 'chat/completions SSE'];

    await test.step('one DSH process and native session continue after a real token refresh', async () => {
      session = new DesktopSession({ home: fixture.dsh_home, cwd: fixture.workspace, modelBaseURL: fixture.gateway, model: fixture.model });
      await session.start(tokens.access_token);
      const first = await session.ask('Remember the exact label amber-pine-73. Reply only OK.');
      evidence.first_dsh_result = first;
      expect(first.finalResponse.trim()).not.toBe('');
      const pid = session.pid;
      const updated = await refresh(config, tokens.refresh_token, session);
      expect(updated.access_token).not.toBe(tokens.access_token);
      usedTokens.add(updated.access_token);
      const second = await session.ask('What exact label did I ask you to remember? Reply only with that label.');
      expect(second.finalResponse).toContain('amber-pine-73');
      expect(session.pid).toBe(pid);
      expect(second.sessionId).toBe(first.sessionId);
      const expectedWireHashes = [tokens.access_token, updated.access_token].map(token =>
        createHash('sha256').update(token).digest('hex'));
      const native = await control('native', { home: fixture.dsh_home, session_id: first.sessionId, credential_header: 'x-api-key', credential_sha256: expectedWireHashes });
      expect([...new Set(native.model_message_turns)]).toEqual([1, 2]);
      const expectedRequests = native.model_message_turns.map((turn: number) => expectedWireHashes[turn - 1]);
      // A native tool call creates another model step in the same turn.
      // Compare all requests against independent committed native messages.
      expect(native.model_requests.map((entry: { credential_sha256: string }) => entry.credential_sha256)).toEqual(expectedRequests);
      expect(native.model_requests.every((entry: { status: number }) => entry.status === 200)).toBe(true);
      expect(native.contents).toContain('amber-pine-73');
      expect(native.contents).toContain(second.finalResponse);
      evidence.dsh = { pid, session_id: first.sessionId, native, first: first.events, second: second.events };
      // Revoke the actual old Access Token record, not a simulated auth reply.
      await control('revoke', { token: tokens.access_token });
      expect((await models(tokens.access_token)).status()).toBe(401);
      expect((await models(updated.access_token)).status()).toBe(200);
      tokens.access_token = updated.access_token;
      tokens.refresh_token = updated.refresh_token ?? tokens.refresh_token;
    });

    await test.step('untrusted credentials and caller parameters cannot expand the grant', async () => {
      const variants = await control('signed_variants', { subject: fixture.subject });
      for (const [name, variant] of Object.entries(variants) as Array<[string, { token: string; status: number }]>) {
        expect((await models(variant.token)).status(), name).toBe(variant.status);
      }
      const overrides: Array<[Record<string, unknown>, number]> = [
        [{ model: fixture.denied_model }, 403], [{ model: fixture.denied_alias }, 403],
        // Native LiteLLM rejects upstream routing before Custom Auth runs.
        [{ api_base: fixture.gateway }, 401], [{ api_key: 'caller-credential' }, 403],
        [{ fallbacks: [fixture.denied_model] }, 403], [{ user: 'another-employee' }, 403],
      ];
      for (const [extra, status] of overrides) {
        expect((await chat(tokens.access_token, extra)).status(), Object.keys(extra).join(',')).toBe(status);
      }
      expect((await request.get(`${fixture.gateway}/v1/models?team_id=other`, { headers: bearer(tokens.access_token) })).status()).toBe(403);
      await control('user_admin', { admin: true });
      const adminTokens = await refresh(config, tokens.refresh_token, session);
      usedTokens.add(adminTokens.access_token);
      tokens.access_token = adminTokens.access_token;
      tokens.refresh_token = adminTokens.refresh_token ?? tokens.refresh_token;
      for (const route of ['/key/generate', '/model/new']) {
        expect((await request.post(fixture.gateway + route, { headers: bearer(tokens.access_token), data: {} })).status()).toBe(403);
      }
      expect((await request.get(`${fixture.gateway}/api/v1/agents`, { headers: bearer(tokens.access_token) })).status()).toBe(403);
      await control('user_admin', { admin: false });
      await control('user_disabled', { disabled: true });
      expect((await models(tokens.access_token)).status()).toBe(401);
      await control('user_disabled', { disabled: false });
      await control('application', { enabled: false });
      expect((await models(tokens.access_token)).status()).toBe(403);
      await control('application', { enabled: true });
      await control('application', { removed: true });
      expect((await models(tokens.access_token)).status()).toBe(403);
      await control('application', { restore: true });
      await control('provider_down');
      expect((await models(tokens.access_token)).status()).toBe(503);
      await control('provider_up');
    });

    await test.step('native accounting, user/application/team rates and actual spend enforce policy', async () => {
      const audit = await control('audit', { employee, access_tokens: [...usedTokens] });
      expect(audit.employee).toBe(employee);
      expect(audit.application).toBe(fixture.app_user_id);
      expect(audit.team).toBe(fixture.team_id);
      expect(audit.spend).toBeGreaterThan(0);
      expect(audit.actual_requests).toBeGreaterThanOrEqual(4);
      expect(audit.raw_access_token_retained).toBe(false);
      evidence.audit = audit;
      for (const dimension of ['employee', 'application', 'team']) {
        await control('rate', { dimension, limited: true });
        expect((await chat(tokens.access_token)).status(), dimension).toBe(429);
        await control('rate', { dimension, limited: false });
      }
      await control('budget', { employee, below_actual_spend: true });
      const limited = await chat(tokens.access_token);
      expect(limited.ok()).toBe(false);
      expect(JSON.stringify(await limited.json())).toMatch(/budget/i);
      await control('budget', { employee, restore: true });
    });

    await session?.close();

    await test.step('existing Virtual Key, platform login/API/admin UI and sandbox model access remain usable', async () => {
      const old = await control('existing_key');
      expect(old.virtual_key_inference).toBe(true);
      const platform = await loginViaOidc(browser);
      try {
        const agents = await platform.context.request.get(`${fixture.console}/api/v1/agents`);
        expect(agents.status()).toBe(200);
        const management = await platform.context.request.get(`${fixture.console}/litellm/ui/`);
        expect(management.status()).toBe(200);
        expect(await management.text()).toMatch(/LiteLLM|litellm/);
        const api = new AstraApi(platform.context.request);
        const created = await api.startConversation(fixture.agent_id);
        await api.waitForSessionReady(created.session_id);
        const turn = await api.sendTurn(created.session_id, 'Reply with only the word maple.');
        expect(turn.errorText).toBeNull();
        expect(turn.text).toContain('maple');
        const reply = await api.waitForReplyTo(created.session_id, turn.inputId);
        expect(messageText(reply)).toContain('maple');
        evidence.existing = { virtual_key_inference: true, platform_api: true, management_login: true,
          sandbox_session_id: created.session_id, sandbox_answer: turn.text };
      } finally {
        await platform.context.close();
      }
    });
    const revocation = await control('revoke', { token: tokens.access_token });
    expect(revocation.revoked).toBe(true);
    expect((await models(tokens.access_token)).status()).toBe(401);
    evidence.revocation = revocation;
    evidence.failed = false;
  } finally {
    await session?.close();
    await context.close();
    await writeFile(testInfo.outputPath('oidc-model-evidence.json'), JSON.stringify(evidence, null, 2), { mode: 0o600 });
    // The dispatcher retains task services, accounts, databases and native
    // stores on failure. Retirement is a separate post-review action.
  }
});
