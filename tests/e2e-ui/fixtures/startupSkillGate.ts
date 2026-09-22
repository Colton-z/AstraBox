/** Serve a tiny real Git Skill while holding exactly its first discovery GET. */
import { execFile } from 'node:child_process';
import { createReadStream } from 'node:fs';
import { mkdir, mkdtemp, realpath, rm, stat, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { networkInterfaces, tmpdir } from 'node:os';
import { join, resolve, sep } from 'node:path';
import { pipeline } from 'node:stream/promises';
import { promisify } from 'node:util';

const runFile = promisify(execFile);

export interface StartupSkillRequest {
  method: string;
  path: string;
  receivedAt: number;
  held: boolean;
  status: number | null;
  completedAt: number | null;
}

export interface StartupSkillGate {
  descriptor: string;
  commit: string;
  gitVersion: string;
  directory: string;
  readonly heldAt: number | null;
  readonly releasedAt: number | null;
  requests: StartupSkillRequest[];
  errors: string[];
  release(): void;
  close(keepFiles?: boolean): Promise<void>;
}

/**
 * Use a full commit so Git's dumb HTTP transport never needs a shallow clone.
 * Only an explicit release opens the first info/refs response. Other requests,
 * including a second clone's info/refs, continue to read the same Git database.
 * Call close in finally; pass true to retain files for a failed scene.
 */
export async function startupSkillGate(): Promise<StartupSkillGate> {
  // The AWS K3s testbed routes this bridge through its Pod network on every
  // node. A host's private IP does not grant peers access to ephemeral ports.
  const addresses = (networkInterfaces().cni0 || []).filter((item) => item.family === 'IPv4');
  if (addresses.length !== 1) throw new Error('startupSkillGate requires one IPv4 address on the K3s cni0 bridge');
  const advertised = new URL(`http://${addresses[0].address}`);
  const directory = await mkdtemp(join(tmpdir(), 'astrabox-startup-skill-'));
  const repository = join(directory, 'repository');
  const gitDatabase = join(repository, '.git');
  const requests: StartupSkillRequest[] = [];
  const errors: string[] = [];
  let heldAt: number | null = null;
  let releasedAt: number | null = null;
  let openGate!: () => void;
  const gate = new Promise<void>((done) => { openGate = done; });
  const release = () => {
    if (releasedAt !== null) return;
    releasedAt = Date.now();
    openGate();
  };

  const server = createServer(async (request, response) => {
    const record: StartupSkillRequest = {
      method: request.method || '', path: request.url || '/', receivedAt: Date.now(),
      held: false, status: null, completedAt: null,
    };
    requests.push(record);
    let reportedDisconnect = false;
    const disconnected = () => {
      if (reportedDisconnect || response.writableFinished) return;
      reportedDisconnect = true;
      errors.push(`connection closed before response finished: ${record.method} ${record.path}`);
    };
    request.on('aborted', disconnected);
    response.on('close', disconnected);
    response.on('finish', () => { record.completedAt = Date.now(); });
    const reply = (status: number, body: string) => {
      record.status = status;
      response.writeHead(status, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' }).end(body);
    };

    try {
      if (record.method !== 'GET' && record.method !== 'HEAD') {
        reply(405, 'Only GET and HEAD are supported.');
        return;
      }
      const url = new URL(record.path, 'http://fixture.invalid');
      const pathname = decodeURIComponent(url.pathname);
      if (!pathname.startsWith('/repo.git/') || pathname.includes('\\') || pathname.includes('\0')) {
        reply(404, 'Not found.');
        return;
      }
      const relative = pathname.slice('/repo.git/'.length);
      if (relative.split('/').some((part) => part === '.' || part === '..')) {
        reply(404, 'Not found.');
        return;
      }
      // Resolve symlinks too: HTTP can expose only this fixture's Git database,
      // never the working tree, parent directory or an external link target.
      const root = await realpath(gitDatabase);
      let filename: string;
      try {
        filename = await realpath(resolve(root, relative));
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
        reply(404, 'Not found.');
        return;
      }
      if (!filename.startsWith(root + sep) || !(await stat(filename)).isFile()) {
        reply(404, 'Not found.');
        return;
      }
      if (record.method === 'GET' && pathname === '/repo.git/info/refs'
        && heldAt === null && releasedAt === null) {
        record.held = true;
        heldAt = Date.now();
        await gate;
      }
      if (response.destroyed) {
        disconnected();
        return;
      }
      record.status = 200;
      response.writeHead(200, {
        'Content-Type': pathname === '/repo.git/info/refs' ? 'text/plain' : 'application/octet-stream',
        'Content-Length': (await stat(filename)).size,
        'Cache-Control': 'no-store',
      });
      if (record.method === 'HEAD') response.end();
      else await pipeline(createReadStream(filename), response);
    } catch (error) {
      errors.push(`${record.method} ${record.path}: ${String(error)}`);
      if (!response.headersSent && !response.destroyed) reply(500, 'Git fixture failed.');
      else response.destroy();
    }
  });
  server.on('error', (error) => errors.push(error.message));

  let closing: Promise<void> | null = null;
  const close = (keepFiles = false): Promise<void> => {
    if (closing) return closing;
    release();
    closing = (async () => {
      if (server.listening) {
        await new Promise<void>((done, reject) => {
          server.close((error) => error ? reject(error) : done());
          server.closeAllConnections();
        });
      }
      if (!keepFiles) await rm(directory, { recursive: true });
    })();
    return closing;
  };

  try {
    await mkdir(join(repository, 'startup-gate'), { recursive: true });
    await writeFile(join(repository, 'startup-gate', 'SKILL.md'), [
      '---', 'name: startup-gate', 'description: Reference material for a startup scheduling check.',
      '---', '', 'This fixture contains no executable tools or external dependencies.', '',
    ].join('\n'));
    const git = async (...args: string[]) => {
      const result = await runFile('git', ['-C', repository, ...args], {
        timeout: 10_000, maxBuffer: 1024 * 1024,
        env: { ...process.env, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null' },
      });
      return result.stdout.trim();
    };
    const gitVersion = await git('--version');
    await git('init', '--template=', '--initial-branch=main');
    await git('add', 'startup-gate/SKILL.md');
    await git('-c', 'user.name=AstraBox fixture', '-c', 'user.email=fixture@example.invalid',
      '-c', 'commit.gpgsign=false', 'commit', '-m', 'Tiny startup Skill fixture');
    await git('update-server-info');
    const commit = await git('rev-parse', 'HEAD');
    if (!/^[a-f0-9]{40,64}$/.test(commit)) throw new Error(`invalid full Git commit: ${commit}`);
    await new Promise<void>((done, reject) => {
      server.once('error', reject);
      server.listen(0, advertised.hostname, () => {
        server.off('error', reject);
        done();
      });
    });
    const address = server.address() as AddressInfo;
    advertised.protocol = 'http:';
    advertised.port = String(address.port);
    advertised.pathname = '/repo.git';
    advertised.search = '';
    advertised.hash = '';
    advertised.username = '';
    advertised.password = '';
    return {
      descriptor: `${advertised.toString()}@${commit}#startup-gate`, commit, gitVersion, directory,
      get heldAt() { return heldAt; },
      get releasedAt() { return releasedAt; },
      requests, errors, release, close,
    };
  } catch (error) {
    await close(true);
    throw new Error(`startupSkillGate setup failed; files retained at ${directory}: ${String(error)}`, { cause: error });
  }
}
