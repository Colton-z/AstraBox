/** A desktop main-process bridge; credentials never enter renderer state. */
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, readFile, writeFile, rename, watch } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DeepSeekHarness } from '@deepseek-ai/dsh-sdk-client';

const reference = 'DESKTOP_MODEL_ACCESS_TOKEN';

export class DesktopSession {
  constructor({ home, cwd, modelBaseURL, model }) {
    this.home = resolve(home);
    this.cwd = resolve(cwd);
    this.modelBaseURL = modelBaseURL;
    this.model = model;
    this.sessionId = randomUUID();
  }

  async start(accessToken) {
    // Use an application-owned private directory, never an existing DSH home.
    await mkdir(this.home, { mode: 0o700 });
    await this.writeCredential(accessToken);
    const patch = join(this.home, 'desktop.patch.json');
    await writeFile(patch, JSON.stringify([
      { id: 'session-persistence-jsonl', config: { root: join(this.home, 'sessions'), compression: 'none' } },
      { id: 'session-log-deepseek', config: { enabled: false } },
      { id: 'plugin-package-inventory-deepseek', config: { enabled: false } },
      { id: 'llm-deepseek', config: {
        baseURL: this.modelBaseURL, apiKeyEnv: reference,
      } },
      { insert: [{ id: 'desktop-credential-ack',
        name: resolve(dirname(fileURLToPath(import.meta.url)), 'credential-ack.mjs'),
        config: { reference, receipt: join(this.home, 'credential-receipt.json') } }] },
    ]), { mode: 0o600 });
    // In the pinned DSH release inherited credentials are read-only and win
    // over the managed store. Do not launch this reference through env.
    const env = { ...process.env };
    delete env[reference];
    this.harness = new DeepSeekHarness({
      profile: 'sdk', patches: [patch], dshHome: this.home, env,
      cwd: this.cwd, provider: 'deepseek-official', model: this.model,
      maxTokens: 1024,
    });
    await this.harness.start();
    await this.waitForCredential(accessToken);
  }

  async writeCredential(accessToken) {
    if (typeof accessToken !== 'string' || !accessToken) throw new Error('Access Token is required');
    const target = join(this.home, '.credentials.yaml');
    // JSON is valid YAML. This example owns this whole new private home.
    await writeFile(`${target}.tmp`, JSON.stringify({ version: 1, refs: { [reference]: accessToken } }), { mode: 0o600 });
    await rename(`${target}.tmp`, target);
  }

  async waitForCredential(accessToken) {
    const expected = createHash('sha256').update(accessToken).digest('hex');
    const abort = AbortSignal.timeout(10_000);
    const events = watch(this.home, { signal: abort });
    const read = async () => {
      try {
        const current = JSON.parse(await readFile(join(this.home, 'credential-receipt.json'), 'utf8'));
        if (current.sha256 !== expected) return false;
        if (this.pid && current.pid !== this.pid) throw new Error('DSH process changed during refresh');
        this.pid = current.pid;
        return true;
      } catch (error) {
        if (error.code === 'ENOENT') return false;
        throw error;
      }
    };
    try {
      if (await read()) return;
      for await (const event of events) {
        if (event.filename === 'credential-receipt.json' && await read()) return;
      }
      throw new Error('DSH did not confirm the credential update');
    } finally {
      await events.return();
    }
  }

  async replaceAccessToken(accessToken) {
    await this.writeCredential(accessToken);
    await this.waitForCredential(accessToken);
  }

  async ask(text) {
    if (!this.pid) throw new Error('DSH has not acknowledged its credential');
    return this.harness.run(text, { sessionId: this.sessionId });
  }

  async close() {
    await this.harness?.close();
  }
}
