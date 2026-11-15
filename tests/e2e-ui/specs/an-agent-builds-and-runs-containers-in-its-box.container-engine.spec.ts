/**
 * E2E: a conversation's box runs a container engine its Agent can build and
 * run containers with, and the box's egress policy still holds for them.
 *
 * The deployment runs the bundled OpenSandbox server on Docker, on a host that
 * allows unprivileged user namespaces, with the release's egress image and the
 * Credential Vault on; the run says so with ASTRABOX_E2E_CONTAINER_ENGINE=1,
 * and without it Playwright does not collect this file (playwright.config.ts).
 *
 * The spec saves its own copy of the Codex conversation-tenancy RuntimeProfile
 * with `sandbox_container_engine: docker` and `Limited` networking that admits
 * only the registry and the Alpine package CDN. Everything runs through the
 * conversation's terminal, the surface whose commands reach the box, with the
 * engine driven as the workload account the Agent runs as:
 *
 * - `docker info` answers that account on the default socket path, rootless;
 * - an image builds whose RUN steps fetch over HTTPS from an allowed host,
 *   which needs genuine TLS for a host no credential is bound to;
 * - a container's published port answers the box at 127.0.0.1;
 * - from a container, the allowed host answers while a host the RuntimeProfile
 *   does not allow is refused by name and by address. The address check skips
 *   DNS, so only the egress sidecar's packet filter can stop it: an engine
 *   whose containers bypassed the sidecar would reach it.
 */
import { randomUUID } from 'node:crypto';
import { lookup } from 'node:dns/promises';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { engineProfileFor } from '../fixtures/engineProfile';
import { terminalOutput } from '../fixtures/nativeMcpServer';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

// The box image, the registry and CDN hosts it pulls from, and a destination
// the RuntimeProfile does not admit. Docker Hub serves its manifests from
// *.docker.io and its blobs from *.docker.com.
const BASE_IMAGE = 'alpine:3.22';
const ALLOWED_HOSTS = ['*.docker.io', '*.docker.com', 'dl-cdn.alpinelinux.org'];
const ALLOWED_URL = 'https://dl-cdn.alpinelinux.org/alpine/';
const DENIED_HOST = 'example.com';
// A port no service of the box itself listens on.
const PUBLISHED_PORT = 19080;
// The value the stored RuntimeProfile reads back for a set credential; a copy
// that carried it would save the mask, not the key.
const SECRET_MASK = '••••••••';

const sessions = trackSessions();
let agentId = '';
let runtimeProfile: Record<string, unknown> | null = null;
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  if (agentId) await api.deleteAgent(agentId);
  if (runtimeProfile) {
    await api.data('PUT', `/admin/runtime-profiles/${encodeURIComponent(String(runtimeProfile.name))}`, {
      ...runtimeProfile, enabled: false,
    });
  }
});

/** `value` as one single-quoted shell word. */
function shellWord(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

/**
 * One terminal command run as the workload account (uid 1000 in both engine
 * images), its output once it exited 0. The terminal's own shell is root's.
 */
async function asWorkload(api: AstraApi, sessionId: string, command: string, timeoutMs = 60_000): Promise<string> {
  const wrapped = `runuser -u "$(id -un 1000)" -- env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin `
    + `HOME="$(getent passwd 1000 | cut -d: -f6)" sh -c ${shellWord(command)}`;
  return terminalOutput(await api.runTerminalCommand(sessionId, wrapped, '/tmp', timeoutMs));
}

test('an Agent builds and runs containers in its box, under the box\'s egress policy', async ({ request }) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('codex');
  const id = randomUUID().slice(0, 8);
  const sourceName = `${profile.runtime_profile_name}-conversation`;
  const source = (await new PlatformApi(request).listEnvironments())
    .find((item) => String(item.name || '') === sourceName);
  expect(source?.sandbox_tenancy, `${sourceName} is the conversation-tenancy Codex RuntimeProfile`).toBe('conversation');
  const access = (source!.provider_access ?? {}) as Record<string, unknown>;
  expect(access.api_key, 'a copy cannot carry a model key it can only read masked').not.toBe(SECRET_MASK);
  const schema = await api.data<{ fields?: Array<Record<string, unknown>> }>('GET', '/admin/runtime-profile-schema');
  const fields = new Set((schema.fields ?? []).map((field) => String(field.key)));
  const name = `__e2e_container_engine_${id}`;
  runtimeProfile = {
    ...Object.fromEntries(Object.entries(source!).filter(([key]) => fields.has(key))),
    name,
    display_name: name,
    enabled: true,
    sandbox_tenancy: 'conversation',
    sandbox_permission_level: 'advanced',
    sandbox_container_engine: 'docker',
    networking: { type: 'limited', allowed_hosts: ALLOWED_HOSTS, allow_mcp_servers: false },
  };
  await api.data('PUT', `/admin/runtime-profiles/${encodeURIComponent(name)}`, runtimeProfile);
  agentId = (await api.createAgent({
    name, model: profile.model, runtime_profile_name: name, prepared_capacity: 0,
  })).agent_id;
  const sessionId = String((await api.startConversation(agentId)).session_id || '');
  sessions.push(sessionId);
  // The box is handed to the conversation only after its engine answered.
  await api.waitForSessionReady(sessionId, 120_000);

  const info = await asWorkload(api, sessionId, "docker info --format '{{json .SecurityOptions}}'");
  expect(info, 'the engine answers the workload account, rootless').toContain('name=rootless');

  const tag = `e2e-engine-${id}`;
  const build = await asWorkload(api, sessionId, [
    `mkdir -p /tmp/${tag} && cd /tmp/${tag}`,
    `printf 'FROM ${BASE_IMAGE}\\nRUN apk add --no-cache curl busybox-extras && curl -fsS -o /dev/null -w "https-status=%%{http_code}\\\\n" ${ALLOWED_URL}\\n' > Dockerfile`,
    `docker build --progress=plain -t ${tag} . 2>&1`,
  ].join(' && '), 150_000);
  expect(build, 'a build step reached an allowed host over HTTPS').toContain('https-status=200');

  const marker = `ENGINE${id}`;
  await asWorkload(api, sessionId,
    `docker run -d --name ${tag} -p ${PUBLISHED_PORT}:8080 ${tag} sh -c `
    + `'mkdir -p /www && echo ${marker} > /www/index.html && exec httpd -f -p 8080 -h /www'`);
  await expect.poll(async () => {
    const raw = await api.runTerminalCommand(
      sessionId, `curl -sS -m 3 http://127.0.0.1:${PUBLISHED_PORT}/ || true`, '/tmp', 15_000,
    );
    return terminalOutput(raw);
  }, { message: 'the box reaches the container\'s published port', timeout: 20_000, intervals: [1_000] })
    .toContain(marker);

  // Resolved here, outside the box, whose resolver refuses the name.
  const deniedAddress = (await lookup(DENIED_HOST, { family: 4 })).address;
  const reach = await asWorkload(api, sessionId, [
    `docker run --rm ${tag} sh -c '`,
    `curl -sS -m 10 -o /dev/null -w "allowed=%{http_code}\\n" ${ALLOWED_URL};`,
    `curl -sS -m 8 -o /dev/null https://${DENIED_HOST}/; echo "denied-by-name-exit=$?";`,
    `curl -sS -m 8 -o /dev/null --resolve ${DENIED_HOST}:443:${deniedAddress} https://${DENIED_HOST}/; echo "denied-by-address-exit=$?"`,
    `' 2>&1`,
  ].join(' '), 60_000);
  expect(reach, 'a container reaches an allowed host').toContain('allowed=200');
  const exits = Object.fromEntries([...reach.matchAll(/(denied-by-(?:name|address))-exit=(\d+)/g)]
    .map((match) => [match[1], Number(match[2])]));
  expect(Object.keys(exits).sort(), reach).toEqual(['denied-by-address', 'denied-by-name']);
  expect(exits['denied-by-name'], `a container cannot resolve a denied host: ${reach}`).not.toBe(0);
  expect(exits['denied-by-address'], `a container cannot reach a denied host's address: ${reach}`).not.toBe(0);
});
