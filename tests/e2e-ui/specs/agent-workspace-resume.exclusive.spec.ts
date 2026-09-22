/** A prepared replacement serves the original conversation's persistent files. */
import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { expect, test, type Page, type Response } from '@playwright/test';

import { AstraApi, messageText, visibleMessages } from '../fixtures/astraApi';
import { engineCases, engineProfileFor, type EngineProfile } from '../fixtures/engineProfile';
import { apiPath, appPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { sendPrompt } from '../fixtures/sessionPage';
import { requireSandboxHandle, sandboxExec, sandboxRunning, waitForSandboxStopped } from '../fixtures/sandboxOps';
import { WorkspaceResumeNodes } from '../fixtures/workspaceResumeNodes';

interface Prepared {
  ready: boolean;
  prepared_count: number;
  sandbox_id: string;
  client_pool_name: string;
  runtime_generation: string;
}

let crossHostNodes: WorkspaceResumeNodes | undefined;
// Restore scheduling on failure as well as success; failed workloads stay available for diagnosis.
test.afterEach(() => { crossHostNodes?.restore(); });
const sessions = trackSessions();
let agentId = '';
let evidence: Record<string, unknown> = {};

test.beforeEach(() => { agentId = ''; evidence = {}; crossHostNodes = undefined; });
test.afterEach(async ({}, info) => {
  await info.attach('agent-workspace-resume', {
    body: JSON.stringify(evidence), contentType: 'application/json',
  });
});
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function browserResponse(page: Page, path: string): Promise<Response> {
  return page.waitForResponse((response) => response.request().method() === 'GET'
    && !response.request().isNavigationRequest()
    && new URL(response.url()).pathname === apiPath(path));
}

async function preparedCapacity(page: Page, id: string): Promise<Prepared> {
  const path = `/agents/${id}/prepared-runtime`;
  const read = browserResponse(page, path);
  await page.goto(appPath(`/manage/agents/${id}`));
  const parse = async (response: Response): Promise<Prepared> => {
    expect(response.status()).toBe(200);
    const envelope = await response.json();
    expect(envelope.code).toBe('OK');
    evidence.lastPreparedStatus = envelope.data;
    return envelope.data as Prepared;
  };
  let status = await parse(await read);
  await expect.poll(async () => {
    if (!status.ready || status.prepared_count < 1) {
      const refreshed = browserResponse(page, path);
      await page.getByTestId('agent-prewarm-status')
        .getByRole('button', { name: 'Refresh', exact: true }).click();
      status = await parse(await refreshed);
    }
    return status.ready && status.prepared_count > 0;
  }, { timeout: 60_000, intervals: [1_000, 2_000] }).toBe(true);
  await expect(page.getByTestId('prewarm-state')).toHaveText('Ready');
  await expect(page.getByTestId('prewarm-count')).toHaveText(`Available: ${status.prepared_count}`);
  expect(status.sandbox_id).toBeTruthy();
  expect(status.client_pool_name).toBeTruthy();
  expect(status.runtime_generation).toBeTruthy();
  return status;
}

async function createFixture(api: AstraApi, profile: EngineProfile): Promise<string> {
  const environments = await api.data<Array<Record<string, unknown>>>('GET', '/admin/environments');
  const environment = environments.find((item) => item.enabled === true
    && item.sandbox_tenancy === 'conversation' && item.engine_kind === profile.engine_kind
    && item.runtime_template_name === profile.image);
  expect(environment, `the deployment must supply the exact ${profile.engine_kind} conversation image`).toBeTruthy();
  const base = await api.getAgent(profile.agent_id);
  const agent = await api.createAgent({
    name: `workspace-resume-${profile.engine_kind}-${randomUUID()}`,
    model: base.model, engine_options: base.engine_options,
    environment_name: environment!.name, prewarm_enabled: true,
  });
  agentId = agent.agent_id;
  evidence.agentId = agentId;
  return agent.name;
}

async function browserTurn(api: AstraApi, page: Page, sessionId: string, prompt: string) {
  const previous = (await api.getSession(sessionId)).last_turn_id;
  await sendPrompt(page, sessionId, prompt);
  const completed = await api.waitForSession(sessionId, (session) => session.last_turn_id !== previous
    && !session.current_turn_id && ['COMPLETED', 'FAILED', 'INTERRUPTED'].includes(String(session.last_turn_status)), 90_000);
  expect(completed.last_turn_status, String(completed.last_error || '')).toBe('COMPLETED');
  expect(completed.state).toBe('READY');
  expect(completed.last_error || null).toBeNull();
  const history = visibleMessages(await api.getMessages(sessionId, 100));
  const turn = history.filter((row) => row.turn_id === completed.last_turn_id);
  const blocks = turn.flatMap((row) => row.blocks || []);
  const tools = blocks.filter((block) => block.type === 'tool_use');
  expect(tools, 'the real Agent must operate on the file, not merely describe the command').not.toHaveLength(0);
  const outputs = blocks.filter((block) => block.type === 'tool_result');
  for (const tool of tools) {
    expect(outputs.some((output) => output.tool_use_id === tool.id && output.is_error !== true),
      `the native tool ${String(tool.id)} must have a successful paired result`).toBe(true);
  }
  const reply = turn.filter((row) => row.role === 'assistant').map(messageText).join('\n');
  expect(reply.trim()).not.toBe('');
  await expect(page.getByTestId('user-message').filter({ hasText: prompt })).toHaveCount(1);
  await expect(page.getByTestId('assistant-message').last().getByTestId('assistant-text')).not.toBeEmpty();
  await expect(page.getByTestId('assistant-message').last()).not.toHaveAttribute('data-streaming', 'true');
  return { completed, reply, turn };
}

const xattrProbe = `
attribute = 'user.astrabox_e2e_workspace_resume'
value = b'ordinary-user-xattr'
try:
    os.setxattr(marker, attribute, value, flags=os.XATTR_CREATE)
except OSError as exc:
    if exc.errno != errno.EOPNOTSUPP:
        raise
    xattr = {'errno': exc.errno, 'operation': 'set'}
else:
    assert os.getxattr(marker, attribute) == value
    os.removexattr(marker, attribute)
    xattr = {'errno': 0, 'operation': 'set/get/remove'}
`;

function kubeText(args: string[]): string {
  return execFileSync('kubectl', args, { encoding: 'utf8', timeout: 30_000,
    stdio: ['ignore', 'pipe', 'pipe'], maxBuffer: 16 * 1024 * 1024 });
}

function backingMountEvidence(
  namespace: string, podName: string, workspaceDir: string, filename: string, uid: number, gid: number,
) {
  const resources = JSON.parse(kubeText(['get', 'pods,pvc', '-n', namespace, '-o', 'json'])).items;
  const read = (kind: string, name: string) => {
    const found = resources.filter((item: any) => item.kind === kind && item.metadata.name === name);
    expect(found, `live ${kind}/${name} must exist in ${namespace}`).toHaveLength(1);
    return found[0];
  };
  const pod = read('Pod', podName);
  const workload = pod.spec.containers.find((item: any) => item.name === 'sandbox');
  const mounts = workload.volumeMounts.filter((item: any) => item.mountPath === workspaceDir);
  expect(mounts).toHaveLength(1);
  const viewClaim = pod.spec.volumes.find((item: any) => item.name === mounts[0].name)
    .persistentVolumeClaim.claimName;
  const viewPvc = read('PersistentVolumeClaim', viewClaim);
  const helper = read('Pod', viewClaim);
  expect(helper.metadata.labels['astrabox.storage-managed']).toBe('mergerfs');
  expect(helper.spec.nodeName).toBe(pod.spec.nodeName);
  const dataClaim = helper.spec.volumes.find((item: any) => item.name === 'data')
    .persistentVolumeClaim.claimName;
  const dataPvc = read('PersistentVolumeClaim', dataClaim);
  const volumes = JSON.parse(kubeText(['get', 'pv', viewPvc.spec.volumeName,
    dataPvc.spec.volumeName, '-o', 'json'])).items;
  expect(volumes).toHaveLength(2);
  const viewPv = volumes.find((item: any) => item.metadata.name === viewPvc.spec.volumeName);
  const dataPv = volumes.find((item: any) => item.metadata.name === dataPvc.spec.volumeName);
  expect(helper.spec.volumes.find((item: any) => item.name === 'views').hostPath.path)
    .toBe(viewPv.spec.hostPath.path);
  const input = JSON.stringify(JSON.stringify({ workspaceDir, filename, uid, gid }));
  const native = JSON.parse(kubeText(['exec', '-n', namespace, helper.metadata.name,
    '-c', 'workspace-mounter', '--', 'python3', '-c', `
import errno
import hashlib
import json
import os
import pathlib
import subprocess
config = json.loads(${input})
assert os.geteuid() == 0
state = json.loads(pathlib.Path('/views/.control/state.json').read_text())
assert state['phase'] == 'READY' and state['data'] == '/data'
mounts = [item for item in state['mounts'] if item[0] == config['workspaceDir']]
assert len(mounts) == 1
marker = str(pathlib.Path(state['data']) / mounts[0][1] / config['filename'])
filesystems = json.loads(subprocess.check_output(
    ['findmnt', '--json', '--target', marker, '--output', 'TARGET,SOURCE,FSTYPE'],
    text=True, timeout=10))['filesystems']
assert len(filesystems) == 1
os.setgroups([])
os.setgid(config['gid'])
os.setuid(config['uid'])
${xattrProbe}
print(json.dumps({'mount': filesystems[0], 'path': marker, 'uid': os.geteuid(),
                  'sha256': hashlib.sha256(pathlib.Path(marker).read_bytes()).hexdigest(),
                  'xattr': xattr}))
`]));
  const csi = dataPv.spec.csi;
  let filesystemId: string | null = null;
  if (csi) {
    expect(csi.driver, 'this EFS acceptance must use the official CSI driver').toBe('efs.csi.aws.com');
    filesystemId = String(csi.volumeHandle).split(':')[0];
    expect(filesystemId).toMatch(/^fs-[0-9a-f]+$/);
    expect(native.mount.fstype).toBe('nfs4');
    expect(native.xattr).toEqual({ errno: 95, operation: 'set' });
  } else {
    expect(dataPv.spec.hostPath?.path, 'the local baseline must have an actual hostPath backing').toBeTruthy();
    expect(native.mount.fstype).toBe('ext4');
    expect(native.xattr).toEqual({ errno: 0, operation: 'set/get/remove' });
  }
  return { helper: helper.metadata.name, node: pod.spec.nodeName, helperNode: helper.spec.nodeName,
    namespace, viewClaim, dataClaim, dataPvcUid: dataPvc.metadata.uid, dataPv: dataPv.metadata.name,
    csi: csi ? { driver: csi.driver, volumeHandle: csi.volumeHandle, filesystemId } : null,
    hostPath: dataPv.spec.hostPath?.path ?? null, native };
}

async function workspaceMountEvidence(
  api: AstraApi, sandboxId: string, identity: Record<string, unknown> | null | undefined, filename: string,
) {
  const workspaceDir = String(identity?.workspace_dir || '');
  const linuxUser = String(identity?.linux_user || '');
  expect(workspaceDir).toMatch(/^\//);
  expect(linuxUser).not.toBe('');
  const input = JSON.stringify(JSON.stringify({ workspaceDir, linuxUser, filename }));
  const handle = await requireSandboxHandle(api, sandboxId);
  const routed = JSON.parse(sandboxExec(handle, `python3 - <<'PY'
import errno
import hashlib
import json
import os
import pathlib
import pwd

config = json.loads(${input})
assert os.geteuid() == 0, 'the control-entry probe must start as sandbox root'
paths = [os.path.join(config['workspaceDir'], '.mergerfs'),
         os.path.join(config['workspaceDir'], '..', '.mergerfs')]
observed = []
def require_absent():
    for path in paths:
        try:
            os.lstat(path)
        except OSError as exc:
            assert exc.errno == errno.ENOENT, (path, exc.errno)
            observed.append({'path': path, 'uid': os.geteuid(), 'errno': exc.errno,
                             'code': errno.errorcode[exc.errno]})
        else:
            raise AssertionError('mergerfs control entry is reachable: ' + path)

require_absent()
account = pwd.getpwnam(config['linuxUser'])
assert account.pw_uid != 0, 'ordinary workspace access must use a non-root account'
os.initgroups(account.pw_name, account.pw_gid)
os.setgid(account.pw_gid)
os.setuid(account.pw_uid)
require_absent()
marker = os.path.join(config['workspaceDir'], config['filename'])
${xattrProbe}
print(json.dumps({'control_entries': observed, 'xattr': xattr,
                  'sha256': hashlib.sha256(pathlib.Path(marker).read_bytes()).hexdigest(),
                  'ordinary_uid': os.geteuid(), 'ordinary_gid': os.getegid()}))
PY`));
  if (handle.runtime === 'docker') {
    expect(routed.xattr).toEqual({ errno: 0, operation: 'set/get/remove' });
    return routed;
  }
  const backing = backingMountEvidence(handle.namespace, handle.pod, workspaceDir, filename,
    routed.ordinary_uid, routed.ordinary_gid);
  expect(routed.xattr, 'the routed workspace must preserve the measured backing filesystem semantics')
    .toEqual(backing.native.xattr);
  expect(routed.sha256, 'both probes must read the same actual workspace file').toBe(backing.native.sha256);
  expect(routed.ordinary_uid).toBe(backing.native.uid);
  return { ...routed, backing };
}

function workspaceResumeScenario(engineKind: string, crossHost: boolean) {
  // Selection receipts identify cases by literal title; Claude uses the unprefixed title.
  const title = 'the same Agent conversation reads its persisted workspace after sandbox rebuild';
  const engineTitle = engineKind === 'claude_code' ? title : `${engineKind}: ${title}`;
  test(crossHost ? `cross-host: ${engineTitle}` : engineTitle, async ({ page, context, request }) => {
    const profile = engineProfileFor(engineKind);
    const api = new AstraApi(request);
    const observer = await context.newPage();
    evidence.engine = profile.engine_kind;
    const appended = `APPENDED-${randomUUID()}`;
    const project = `RESEARCH-${randomUUID()}`;
    const filename = 'e2e-marker.txt';
    if (crossHost) {
      crossHostNodes = new WorkspaceResumeNodes();
      evidence.scheduling = crossHostNodes.evidence;
      crossHostNodes.prepareSource();
    }
    const name = await createFixture(api, profile);
    const firstPrepared = await preparedCapacity(observer, agentId);
    evidence.firstPrepared = firstPrepared;
    crossHostNodes?.prepareReplacement();

    await page.goto(appPath('/agents'));
    const card = page.getByTestId('agent-option').and(page.locator(`[data-agent-name="${name}"]`));
    await card.getByRole('button', { name: 'Start conversation', exact: true }).click();
    await page.waitForURL(/\/sessions\/[^/]+$/);
    const sessionId = new URL(page.url()).pathname.split('/').pop()!;
    sessions.push(sessionId);
    evidence.sessionId = sessionId;
    const initial = await api.waitForSessionReady(sessionId);
    const oldSandbox = String(initial.sandbox_id || '');
    expect(oldSandbox, 'the first conversation must claim the observed waiting box').toBe(firstPrepared.sandbox_id);
    if (profile.modes.unattended) await api.setPermissionMode(sessionId, profile.modes.unattended);
    const original = await api.adminSessionDetail(sessionId);
    const workspaceId = String(original.workspace_id || '');
    expect(workspaceId, 'the conversation must own a durable workspace identity').not.toBe('');
    evidence.original = { sandboxId: oldSandbox, workspaceId, workspaceDir: original.runtime_identity?.workspace_dir };

    // The Agent writes bytes it never reads into history, so resume cannot reconstruct
    // a lost file from the previous prompt. The project label separately checks history.
    const writePrompt = `Our research project label is ${project}; keep that label in this conversation, not in a file. `
      + `Use your shell tool in your current working directory to execute exactly: cat /proc/sys/kernel/random/uuid > ${filename}. `
      + 'Do not read or print the file contents. After success, confirm the project label briefly. Do not ask questions.';
    const written = await browserTurn(api, page, sessionId, writePrompt);
    evidence.write = written;
    expect(written.reply).toContain(project);
    const originalBytes = await api.downloadFileText(sessionId, filename);
    expect(originalBytes).toMatch(/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\n$/);
    const marker = originalBytes.trim();
    expect(JSON.stringify(written.turn), 'the original file value must not be available in native turn history').not.toContain(marker);
    evidence.originalFile = originalBytes;
    const originalMount = await workspaceMountEvidence(api, oldSandbox, original.runtime_identity, filename);
    evidence.originalMount = originalMount;
    if (crossHostNodes) {
      expect(originalMount.backing?.node).toBe(crossHostNodes.name('source'));
      expect(originalMount.backing?.helperNode).toBe(crossHostNodes.name('source'));
      expect(originalMount.backing?.csi?.filesystemId, 'cross-host recovery requires actual EFS backing').toMatch(/^fs-[0-9a-f]+$/);
    }

    // Observe replenishment before destroying the old box. A cold replacement cannot pass.
    const waiting = await preparedCapacity(observer, agentId);
    evidence.replacementPrepared = waiting;
    expect(waiting.sandbox_id).not.toBe(oldSandbox);
    expect(waiting.client_pool_name).toBe(firstPrepared.client_pool_name);
    const handle = await requireSandboxHandle(api, oldSandbox);
    expect(sandboxRunning(handle)).toBe(true);
    const reclaimed = await api.terminateSandbox(sessionId);
    evidence.reclaimed = reclaimed;
    expect(reclaimed.session_id).toBe(sessionId);
    expect(reclaimed.sandbox_id).toBe(oldSandbox);
    expect(reclaimed.killed).toBe(true);
    await waitForSandboxStopped(handle, 30_000);
    expect(sandboxRunning(handle)).toBe(false);

    // Only the browser's next input resumes the same Session; it must read before writing.
    const readPrompt = `Use your shell tool in your current working directory to execute: `
      + `cat ${filename} && printf '%s\\n' '${appended}' >> ${filename}. `
      + 'Do not recreate or overwrite the file. Reply with the original file contents and the research project label '
      + 'from our earlier conversation. Do not store the project label in a file or ask questions.';
    const readBack = await browserTurn(api, page, sessionId, readPrompt);
    evidence.read = readBack;
    expect(readBack.reply).toContain(marker);
    expect(readBack.reply).toContain(project);
    await expect(page.getByTestId('assistant-message').last().getByTestId('assistant-text')).toContainText(marker);
    const rebuilt = await api.adminSessionDetail(sessionId);
    evidence.rebuilt = { sandboxId: rebuilt.sandbox_id, workspaceId: rebuilt.workspace_id,
      workspaceDir: rebuilt.runtime_identity?.workspace_dir };
    expect(rebuilt.session_id).toBe(sessionId);
    expect(rebuilt.sandbox_id, 'resume must claim the exact pre-existing waiting box').toBe(waiting.sandbox_id);
    expect(rebuilt.workspace_id, 'bind the old workspace, not the pool member temporary workspace').toBe(workspaceId);
    expect(rebuilt.runtime_identity?.workspace_dir).toBe(original.runtime_identity?.workspace_dir);
    expect(await api.downloadFileText(sessionId, filename)).toBe(`${originalBytes}${appended}\n`);
    const rebuiltMount = await workspaceMountEvidence(api, waiting.sandbox_id, rebuilt.runtime_identity, filename);
    evidence.rebuiltMount = rebuiltMount;
    if (crossHostNodes) {
      expect(rebuiltMount.backing?.node).toBe(crossHostNodes.name('target'));
      expect(rebuiltMount.backing?.helperNode).toBe(crossHostNodes.name('target'));
      expect(rebuiltMount.backing?.node).not.toBe(originalMount.backing?.node);
      expect(rebuiltMount.backing?.csi).toEqual(originalMount.backing?.csi);
      expect(rebuiltMount.backing?.namespace).toBe(originalMount.backing?.namespace);
      expect(rebuiltMount.backing?.dataClaim).toBe(originalMount.backing?.dataClaim);
      expect(rebuiltMount.backing?.dataPvcUid).toBe(originalMount.backing?.dataPvcUid);
      expect(rebuiltMount.backing?.dataPv).toBe(originalMount.backing?.dataPv);
    }
    const history = visibleMessages(await api.getMessages(sessionId, 100));
    expect(history.filter((row) => row.role === 'user').map(messageText)).toEqual([writePrompt, readPrompt]);
    evidence.history = history;
    await observer.close();
  });
}

for (const profile of engineCases()) {
  workspaceResumeScenario(profile.engine_kind, false);
  workspaceResumeScenario(profile.engine_kind, true);
}
