import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

import { repoRoot } from './env';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from './serviceContainer';

interface ReplicaRecord {
  mode: string;
  location: string;
  log: string;
  exit: string;
}

export interface RecoveryReplica {
  mode: string;
  location: string;
  output: string;
  process: { exitCode: number | null };
  done: Promise<number>;
}

export function servicePython(server: string, program: string, ...args: string[]): string {
  return execFileSync('docker', ['exec', server, 'python', '-c', program, ...args], {
    encoding: 'utf8', timeout: 20_000,
  });
}

function manifestPath(): string {
  const output = process.env.ASTRABOX_E2E_OUTPUT_DIR;
  if (!output || !path.isAbsolute(output)) throw new Error('recovery replicas require an absolute ASTRABOX_E2E_OUTPUT_DIR');
  return path.join(output, 'recovery-replicas.json');
}

const pause = () => new Promise<void>((resolve) => setTimeout(resolve, 100));

/** Compose independent replicas in global setup, before the live test clock. */
export async function prepareRecoveryReplicas(): Promise<() => Promise<void>> {
  const manifest = manifestPath();
  fs.mkdirSync(path.dirname(manifest), { recursive: true });
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const directory = `/tmp/astrabox-recovery-${randomUUID()}`;
  servicePython(server, 'import pathlib,sys; pathlib.Path(sys.argv[1]).mkdir()', directory);
  execFileSync('docker', ['cp', path.join(repoRoot, 'tests/e2e-ui/fixtures/runtime_recovery_replica.py'), `${server}:${directory}/replica.py`], { timeout: 20_000 });
  const records: ReplicaRecord[] = [];
  const children: { process: ChildProcess; record: ReplicaRecord; done: Promise<void> }[] = [];
  const dispose = async () => {
    for (const child of children) {
      if (child.process.exitCode !== null) continue;
      servicePython(server, `import os,pathlib,signal,sys
root=pathlib.Path(sys.argv[1]); mode=sys.argv[2]; pidfile=root/(mode+'.pid')
if pidfile.exists():
    pid=int(pidfile.read_text()); cmd=pathlib.Path('/proc')/str(pid)/'cmdline'
    if cmd.exists():
        parts=cmd.read_bytes().split(bytes([0]))
        if sys.argv[3].encode() not in parts or str(root).encode() not in parts or mode.encode() not in parts:
            raise RuntimeError('fixture PID no longer belongs to this replica')
        os.kill(pid,signal.SIGTERM)
`, child.record.location, child.record.mode, `${directory}/replica.py`);
      await child.done;
    }
  };
  try {
    for (const mode of ['follower', 'winner', 'before-allocation', 'after-allocation', 'assistant-before-allocation']) {
      const location = ['follower', 'winner'].includes(mode) ? directory : `${directory}/${mode}`;
      if (location !== directory) servicePython(server, 'import pathlib,sys; pathlib.Path(sys.argv[1]).mkdir()', location);
      const record = { mode, location, log: path.join(path.dirname(manifest), `${mode}.replica.log`), exit: path.join(path.dirname(manifest), `${mode}.replica-exit.json`) };
      fs.writeFileSync(record.log, '');
      const process = spawn('docker', ['exec', server, 'python', `${directory}/replica.py`, '--directory', location, '--mode', mode]);
      const done = new Promise<void>((resolve) => {
        process.on('error', (error) => fs.appendFileSync(record.log, String(error)));
        process.on('close', (code) => {
          fs.writeFileSync(record.exit, JSON.stringify({ code: code ?? -1 }));
          resolve();
        });
      });
      process.stdout.on('data', (part) => fs.appendFileSync(record.log, part));
      process.stderr.on('data', (part) => fs.appendFileSync(record.log, part));
      children.push({ process, record, done });
      const deadline = Date.now() + 60_000;
      for (;;) {
        if (fs.existsSync(record.exit)) throw new Error(`replica configuration exited: ${fs.readFileSync(record.log, 'utf8')}`);
        const ready = servicePython(server, 'import pathlib,sys; p=pathlib.Path(sys.argv[1]); print(p.read_text() if p.exists() else "")', `${location}/${mode}.ready`).trim();
        if (ready) break;
        if (Date.now() >= deadline) throw new Error(`replica ${mode} configuration exceeded 60000ms: ${fs.readFileSync(record.log, 'utf8')}`);
        await pause();
      }
      records.push(record);
    }
    fs.writeFileSync(manifest, JSON.stringify({ server, directory, records }));
    return dispose;
  } catch (error) {
    await dispose();
    throw error;
  }
}

/** Adopt the exact prepared processes; no service composition runs in the test. */
export function configuredRecoveryReplicas(): { directory: string; children: RecoveryReplica[] } {
  const manifest = manifestPath();
  if (!fs.existsSync(manifest)) throw new Error('globalSetup must call prepareRecoveryReplicas before selecting the recovery ownership case');
  const prepared = JSON.parse(fs.readFileSync(manifest, 'utf8')) as { server: string; directory: string; records: ReplicaRecord[] };
  if (prepared.server !== requireServiceContainer(SERVER_CONTAINER_HANDLE) || prepared.records.length !== 5) throw new Error('recovery replica configuration does not match this deployment');
  const children = prepared.records.map((record): RecoveryReplica => ({
    mode: record.mode,
    location: record.location,
    get output() { return fs.readFileSync(record.log, 'utf8'); },
    process: { get exitCode() { return fs.existsSync(record.exit) ? Number(JSON.parse(fs.readFileSync(record.exit, 'utf8')).code) : null; } },
    get done() { return (async () => {
      while (!fs.existsSync(record.exit)) await pause();
      return Number(JSON.parse(fs.readFileSync(record.exit, 'utf8')).code);
    })(); },
  }));
  return { directory: prepared.directory, children };
}
