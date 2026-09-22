/**
 * A real Agent sandbox must not inherit or reach the platform database.
 *
 * This is a deployment-boundary E2E, not a Compose text assertion. It creates
 * an Agent and conversation through AstraBox, waits for OpenSandbox to provision
 * the workload, and sends a terminal probe through the public session API. The
 * probe knows PostgreSQL's private container address and the node's published
 * database port, so a pass cannot depend on the database merely being hard to
 * guess. The probe crosses the public terminal API, which keeps the assertion
 * the same for Docker-created and Kubernetes-created sandboxes.
 */
import { execFileSync, spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { absoluteBaseUrl, parseTimeoutEnv } from '../fixtures/env';

const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 180_000);
const POSTGRES_CONTAINER = String(process.env.ASTRABOX_E2E_POSTGRES_CONTAINER || '').trim();
const SERVER_CONTAINER = String(process.env.ASTRABOX_E2E_SERVER_CONTAINER || '').trim();
const SECRET_MOUNTS = [
  ['postgres_admin_password', '/run/secrets/postgres_admin_password'],
  ['astrabox_password', '/run/secrets/astrabox_database_password'],
  ['litellm_password', '/run/secrets/litellm_database_password'],
  ['casdoor_password', '/run/secrets/casdoor_database_password'],
] as const;

interface DockerNetworkAttachment {
  IPAddress?: string;
}

interface DockerInspect {
  Mounts?: Array<{
    Destination?: string;
    Source?: string;
  }>;
  NetworkSettings?: {
    Networks?: Record<string, DockerNetworkAttachment>;
    Ports?: Record<string, Array<{ HostIp?: string; HostPort?: string }> | null>;
  };
}

function docker(args: string[], timeoutMs = 30_000): string {
  return execFileSync('docker', args, {
    encoding: 'utf8',
    timeout: timeoutMs,
    stdio: ['ignore', 'pipe', 'pipe'],
    maxBuffer: 64 * 1024 * 1024,
  }).trim();
}

function dockerOutput(args: string[], timeoutMs = 30_000): string {
  const result = spawnSync('docker', args, {
    encoding: 'utf8',
    timeout: timeoutMs,
    stdio: ['ignore', 'pipe', 'pipe'],
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(
      `docker ${args.join(' ')} failed (${result.status}): ${String(result.stderr || '').trim()}`,
    );
  }
  return `${result.stdout || ''}\n${result.stderr || ''}`.trim();
}

function inspectContainer(name: string): DockerInspect {
  const parsed = JSON.parse(docker(['inspect', name])) as DockerInspect[];
  if (!parsed[0]) throw new Error(`docker inspect returned no record for ${name}`);
  return parsed[0];
}

function databaseSecrets(postgres: DockerInspect): Record<string, string> {
  const mounts = postgres.Mounts ?? [];
  return Object.fromEntries(
    SECRET_MOUNTS.map(([name, destination]) => {
      const matches = mounts.filter((mount) => mount.Destination === destination);
      if (matches.length !== 1 || !matches[0].Source) {
        throw new Error(`PostgreSQL must bind exactly one ${destination} secret`);
      }
      const value = readFileSync(matches[0].Source, 'utf8').trim();
      if (!/^[0-9a-f]{64}$/.test(value)) {
        throw new Error(`database secret ${name} is not a generated 64-character value`);
      }
      return [name, value];
    }),
  );
}

function exposedSecretNames(
  secrets: Record<string, string>,
  surfaces: Record<string, string>,
): string[] {
  const exposed: string[] = [];
  for (const [surface, content] of Object.entries(surfaces)) {
    for (const [name, value] of Object.entries(secrets)) {
      if (content.includes(value)) exposed.push(`${surface}:${name}`);
    }
  }
  return exposed;
}

function isolationProbe(
  postgresPrivateIp: string,
  platformHost: string,
  postgresHostPort: number,
): string {
  const source = `
import os
import socket

database_env = sorted(
    name for name in os.environ
    if name in {"ASTRABOX_DB_URL", "DATABASE_URL"}
    or name.startswith("ASTRABOX_DB_")
    or name.startswith("LITELLM_DATABASE_")
    or name.startswith("POSTGRES_")
)
secret_paths = {
    "postgres_admin_password": "/run/secrets/postgres_admin_password",
    "astrabox_database_password": "/run/secrets/astrabox_database_password",
    "litellm_database_password": "/run/secrets/litellm_database_password",
    "casdoor_database_password": "/run/secrets/casdoor_database_password",
}
mounted_secrets = sorted(name for name, path in secret_paths.items() if os.path.exists(path))
targets = {
    "compose_service": ("postgres", 5432),
    "database_private_ip": (${JSON.stringify(postgresPrivateIp)}, 5432),
    "platform_published_port": (${JSON.stringify(platformHost)}, ${postgresHostPort}),
    "platform_default_postgres_port": (${JSON.stringify(platformHost)}, 5432),
}
connected = []
for name, address in targets.items():
    try:
        with socket.create_connection(address, timeout=2):
            connected.append(name)
    except OSError:
        pass

print("DATABASE_ENV_KEYS=" + (",".join(database_env) or "none"))
print("DATABASE_SECRET_MOUNTS=" + (",".join(mounted_secrets) or "none"))
print("DATABASE_REACHABLE_TARGETS=" + (",".join(connected) or "none"))
if database_env or mounted_secrets or connected:
    raise SystemExit(47)
print("ASTRABOX_DATABASE_ISOLATION_OK=1")
`.trim();
  const encoded = Buffer.from(source, 'utf8').toString('base64');
  return `printf '%s' '${encoded}' | base64 -d | python3`;
}

function terminalOutput(raw: string): string {
  const chunks: string[] = [];
  for (const line of raw.split('\n')) {
    if (!line.startsWith('data:')) continue;
    try {
      const frame = JSON.parse(line.slice(5).trim()) as Record<string, unknown>;
      if ((frame.type === 'stdout' || frame.type === 'stderr') && typeof frame.text === 'string') {
        chunks.push(frame.text);
      }
    } catch {
      // Keepalives and a partially received line are not terminal frames.
    }
  }
  return chunks.join('');
}

// Passing cleanup keeps the old `finally` order: the session goes before the
// Agent it ran on. A failure retains both as one diagnosable scene.
let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('a real Agent sandbox cannot read or connect to the platform database', async ({
  request,
}) => {
  expect(POSTGRES_CONTAINER, 'set the exact PostgreSQL container name').not.toEqual('');
  expect(SERVER_CONTAINER, 'set the exact server container name').not.toEqual('');

  const postgresInspect = inspectContainer(POSTGRES_CONTAINER);
  const secrets = databaseSecrets(postgresInspect);
  expect(new Set(Object.values(secrets)).size, 'database roles must not share a password').toBe(
    SECRET_MOUNTS.length,
  );

  const postgresInspectText = docker(['inspect', POSTGRES_CONTAINER]);
  const serverInspectText = docker(['inspect', SERVER_CONTAINER]);
  const startupLogs = [
    dockerOutput(['logs', '--tail', '2000', POSTGRES_CONTAINER]),
    dockerOutput(['logs', '--tail', '2000', SERVER_CONTAINER]),
  ].join('\n');
  expect(
    exposedSecretNames(secrets, {
      postgres_inspect: postgresInspectText,
      server_inspect: serverInspectText,
      startup_logs: startupLogs,
    }),
    'generated database values must stay out of Docker metadata and startup logs',
  ).toEqual([]);

  const postgresNetworks = postgresInspect.NetworkSettings?.Networks ?? {};
  const postgresNetworkNames = Object.keys(postgresNetworks);
  expect(postgresNetworkNames, 'PostgreSQL must have one private network attachment').toHaveLength(1);
  const databaseNetwork = postgresNetworkNames[0];
  const postgresPrivateIp = String(postgresNetworks[databaseNetwork]?.IPAddress || '').trim();
  expect(postgresPrivateIp, 'PostgreSQL must have a private container address').toMatch(
    /^\d{1,3}(?:\.\d{1,3}){3}$/,
  );

  const serverNetworks = Object.keys(
    inspectContainer(SERVER_CONTAINER).NetworkSettings?.Networks ?? {},
  );
  expect(serverNetworks, 'the platform server must be able to use its database').toContain(
    databaseNetwork,
  );
  const postgresBindings = postgresInspect.NetworkSettings?.Ports?.['5432/tcp'] ?? [];
  expect(postgresBindings, 'PostgreSQL must publish one loopback-only test port').toHaveLength(1);
  expect(postgresBindings[0]?.HostIp).toBe('127.0.0.1');
  const postgresHostPort = Number.parseInt(String(postgresBindings[0]?.HostPort || ''), 10);
  expect(Number.isInteger(postgresHostPort) && postgresHostPort > 0).toBe(true);
  const platformHost = new URL(absoluteBaseUrl()).hostname;
  expect(platformHost, 'the E2E base URL must name the platform node').not.toEqual('');

  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const agentName = `__e2e_database_isolation_${runId}`;
  let sessionId = '';

  try {
    const base = await api.defaultAgent();
    const environmentName = String(base.environment_name || '').trim();
    expect(environmentName, 'the seeded Agent must identify its environment').not.toEqual('');
    const models = await api.listEnvironmentModels(environmentName);
    const model = models.find((candidate) => candidate && !candidate.includes('*')) || 'deepseek-chat';

    const agent = await api.createAgent({
      name: agentName,
      model,
      environment_name: environmentName,
    });
    agentId = String(agent.agent_id || '').trim();
    expect(agentId).not.toEqual('');

    const created = await api.startConversation(agentId);
    sessionId = String(created.session_id || '').trim();
    if (sessionId) sessions.push(sessionId);
    expect(sessionId).not.toEqual('');
    const ready = await api.waitForSessionReady(sessionId, READY_TIMEOUT_MS);
    const sandboxId = String(ready.sandbox_id || '').trim();
    expect(sandboxId, 'the conversation must own a real OpenSandbox workload').not.toEqual('');

    const terminal = terminalOutput(await api.runTerminalCommand(
      sessionId,
      isolationProbe(postgresPrivateIp, platformHost, postgresHostPort),
      undefined,
      60_000,
    ));
    expect(terminal).toContain('DATABASE_ENV_KEYS=none');
    expect(terminal).toContain('DATABASE_SECRET_MOUNTS=none');
    expect(terminal).toContain('DATABASE_REACHABLE_TARGETS=none');
    expect(terminal).toContain('ASTRABOX_DATABASE_ISOLATION_OK=1');
  } finally {
    // Nothing here is released on a failing run. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why the unit is the whole block.
  }
});
