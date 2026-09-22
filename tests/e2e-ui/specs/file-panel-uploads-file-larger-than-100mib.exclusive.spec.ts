/**
 * E2E: the file panel uploads a file larger than 100 MiB without truncation.
 *
 * Selecting the 101 MiB fixture must send one successful upload, refresh the file
 * tree, and show the new entry. The files API must report the exact byte count
 * from the sandbox filesystem. This scenario does not download the file because
 * the current download transport caps buffered responses at 64 MiB; upload uses a
 * separate multipart path without that response limit.
 *
 * The upload is expensive and mutates one session filesystem, so the spec runs
 * exclusively. It does not require a model turn.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';

// 101 MiB + a prime tail so the size is unambiguously over the 100MiB threshold
// and exact: a transfer truncated at any buffer or chunk boundary cannot land on
// this byte count by accident.
const LARGE_FILE_UPLOAD_BYTES = 101 * 1024 * 1024 + 17;

// A 101 MiB browser upload staged over the docker socket sits well above the
// suite default; all budgets are env-tunable.
const UPLOAD_RESPONSE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_LARGE_FILE_UPLOAD_RESPONSE_TIMEOUT_MS', 600_000);
const FILE_ENTRY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_LARGE_FILE_UPLOAD_LIST_TIMEOUT_MS', 180_000);
const PANEL_READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_LARGE_FILE_UPLOAD_PANEL_TIMEOUT_MS', 60_000);

interface LargeUploadFixture {
  dir: string;
  filePath: string;
  fileName: string;
  size: number;
}

// A directory-listing entry projected from OpenSandbox execd's Filesystem API.
interface SessionFileListEntry {
  name?: string;
  path?: string;
  kind?: string;
  size?: number;
}
interface SessionFileListPayload {
  root_path?: string;
  current_path?: string;
  entries?: SessionFileListEntry[];
}

// Materialize a >100MiB fixture on the runner's disk in 1 MiB chunks (never a
// single 101 MiB Buffer). The bytes are opaque filler — the scenario proves the
// full length lands, not the content (reading it back is not covered; see header).
function createLargeUploadFixture(): LargeUploadFixture {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'astrabox-e2e-large-upload-'));
  const fileName = `e2e-large-upload-${Date.now()}.bin`;
  const filePath = path.join(dir, fileName);
  const chunk = Buffer.alloc(1024 * 1024, 0x61);
  const fd = fs.openSync(filePath, 'w');
  try {
    let written = 0;
    while (written < LARGE_FILE_UPLOAD_BYTES) {
      const size = Math.min(chunk.length, LARGE_FILE_UPLOAD_BYTES - written);
      fs.writeSync(fd, chunk, 0, size, written);
      written += size;
    }
  } finally {
    fs.closeSync(fd);
  }
  return { dir, filePath, fileName, size: LARGE_FILE_UPLOAD_BYTES };
}

function removeLargeUploadFixture(fixture: LargeUploadFixture | null): void {
  if (!fixture) return;
  fs.rmSync(fixture.dir, { recursive: true, force: true });
}

// Poll POST /sessions/{id}/files/list until the uploaded file appears, then
// assert its durable, sandbox-stat'd metadata. AstraApi exposes no
// listSessionFiles helper and a spec does not edit the shared fixture, so the
// listing call is inlined here through the generic data() surface.
async function waitForSessionFileEntry(
  api: AstraApi,
  sessionId: string,
  fileName: string,
  expectedSize: number,
  timeoutMs: number,
): Promise<SessionFileListEntry> {
  const deadline = Date.now() + timeoutMs;
  let last: SessionFileListPayload | null = null;
  while (Date.now() < deadline) {
    last = await api.data<SessionFileListPayload>(
      'POST',
      `/sessions/${sessionId}/files/list`,
      {},
      120_000,
    );
    const entry = (last.entries || []).find((item) => item.name === fileName);
    if (entry) {
      expect(entry.kind, `uploaded entry kind for ${fileName}`).toBe('file');
      expect(Number(entry.size || 0), `uploaded entry size for ${fileName}`).toBe(expectedSize);
      expect(String(entry.path || '').trim(), `uploaded entry path for ${fileName}`).not.toEqual('');
      return entry;
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  throw new Error(`Uploaded file ${fileName} did not appear in session files; last=${JSON.stringify(last)}`);
}

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: afterEach hooks run in the
// order they are registered, so the session goes before the agent it ran on.
// A kept session pointing at a deleted agent is half a scene.
let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('file panel uploads a file larger than 100MiB', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');

  let sessionId = '';
  let fixture: LargeUploadFixture | null = null;
  try {
    // ── Isolated agent (pure metadata) + one conversation. Source a concrete
    //    model + environment from the seeded default agent — the create validator
    //    (agent_schema) requires a non-blank model — skipping litellm wildcard
    //    routing keys, same as the per-session / reclaimed sibling specs. ──────
    const base = await api.defaultAgent();
    const environmentName = String(base.environment_name || '').trim();
    expect(environmentName, 'seeded default agent must name an environment').not.toEqual('');
    const models = await api.listEnvironmentModels(environmentName);
    const model = models.find((m) => m && !m.includes('*')) || 'deepseek-chat';

    const agent = await api.createAgent({
      name: `__e2e_large_upload_${runId}`,
      model,
      environment_name: environmentName,
    });
    agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');

    const created = await api.startConversation(agentId);
    sessionId = created.session_id;
    sessions.push(sessionId);
    test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
    const ready = await api.waitForSessionReady(sessionId);
    const sandboxId = String(ready.sandbox_id || '').trim();
    expect(sandboxId, 'fresh conversation should hold a sandbox before the file panel opens').not.toEqual('');
    test.info().annotations.push({ type: 'e2e_sandbox_id', description: sandboxId });

    // ── Large-file fixture ──────────────────────────────────────────────────
    fixture = createLargeUploadFixture();
    expect(fixture.size, 'large upload fixture must be larger than 100MiB').toBeGreaterThan(100 * 1024 * 1024);
    test.info().annotations.push(
      { type: 'e2e_large_upload_file_name', description: fixture.fileName },
      { type: 'e2e_large_upload_size', description: String(fixture.size) },
    );

    // ── Files panel ─────────────────────────────────────────────────────────
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 30_000 });
    // 'files' is the default right-panel tab; clicking is idempotent and keeps
    // the anchor explicit. (Default browser locale is en-US → English console.)
    await page.getByRole('tab', { name: 'Files' }).click();
    await expect(
      page.getByText('Current directory', { exact: true }),
      'files panel toolbar should mount',
    ).toBeVisible({ timeout: PANEL_READY_TIMEOUT_MS });
    // The header Upload button enables only once the panel is runtime-ready AND
    // the initial listing resolved an active directory — the correct gate before
    // driving the hidden file input.
    const uploadButton = page.getByRole('button', { name: 'Upload', exact: true });
    await expect(uploadButton, 'Upload should enable when the panel is ready').toBeEnabled({
      timeout: PANEL_READY_TIMEOUT_MS,
    });

    // ── Browser upload path ─────────────────────────────────────────────────
    // Through the Upload button's own file chooser — the user's actual path.
    // Driving `input[type="file"]` by position broke silently when the
    // composer grew its OWN attachment input earlier in the DOM: `.first()`
    // fed the fixture to the attachment flow, no /files/upload POST ever
    // fired (trace network log: zero entries), and the spec burned its whole
    // response budget waiting on a request that was never sent.
    const uploadResponsePromise = page.waitForResponse(
      (response) => (
        response.url().includes(apiPath(`/sessions/${sessionId}/files/upload`))
        && response.request().method() === 'POST'
      ),
      { timeout: UPLOAD_RESPONSE_TIMEOUT_MS },
    );
    const chooserPromise = page.waitForEvent('filechooser');
    await uploadButton.click();
    const chooser = await chooserPromise;
    await chooser.setFiles(fixture.filePath);
    const uploadResponse = await uploadResponsePromise;
    // Only the status is read off the Response: the body would come from
    // Chromium's inspector network cache, which evicts entries under the
    // memory pressure this very upload creates ("Request content was evicted
    // from inspector cache"). Success is proven below by the stronger
    // channels instead — the file tree entry and the full-size sandbox stat.
    expect(uploadResponse.status(), `large file upload status for ${sessionId}`).toBe(200);

    // ── File-tree visibility ────────────────────────────────────────────────
    await expect(
      page.getByText(fixture.fileName, { exact: true }),
      'uploaded file should appear in the file tree',
    ).toBeVisible({ timeout: FILE_ENTRY_TIMEOUT_MS });

    // ── Sandbox file size ───────────────────────────────────────────────────
    // POST /files/list stats the file from disk, proving every byte landed.
    await waitForSessionFileEntry(api, sessionId, fixture.fileName, fixture.size, FILE_ENTRY_TIMEOUT_MS);
  } finally {
    // The session and the agent are not released here. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why a `finally` cannot tell it is unwinding
    // from a failure, and on why the unit is the whole block.
    removeLargeUploadFixture(fixture);
    await page.close().catch(() => {});
  }
});
