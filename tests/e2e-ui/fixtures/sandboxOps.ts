/** Runtime-aware sandbox operations for fault-injection specs. */
import { execFileSync, spawnSync } from 'node:child_process';
import { closeSync, mkdtempSync, openSync, readFileSync, rmdirSync, unlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import type { AstraApi } from './astraApi';
import {
  requireServiceContainer,
  SERVER_CONTAINER_HANDLE,
} from './serviceContainer';

const KUBE_NAMESPACE =
  String(process.env.ASTRABOX_E2E_KUBE_NAMESPACE || 'opensandbox').trim() || 'opensandbox';

export interface SandboxLocator {
  sandboxId: string;
  endpoint: string;
}

export type SandboxHandle =
  | {
    runtime: 'docker';
    sandboxId: string;
    endpoint: string;
    container: string;
  }
  | {
    runtime: 'kubernetes';
    sandboxId: string;
    endpoint: string;
    namespace: string;
    pod: string;
  };

export interface SandboxCommandResult {
  status: number | null;
  stdout: string;
  stderr: string;
  error?: Error;
}

export type SandboxCommandRunner = (
  args: string[],
  timeoutMs?: number,
) => SandboxCommandResult;

export interface SandboxOperationOptions {
  namespace?: string;
  runDocker?: SandboxCommandRunner;
  runKubectl?: SandboxCommandRunner;
}

type EgressRuleAction = 'allow' | 'deny';

interface SandboxSecurityPosture {
  sandbox_id?: unknown;
  available?: unknown;
  default_action?: unknown;
  egress_rules?: unknown;
  detail?: unknown;
}

interface EgressRule {
  action: EgressRuleAction;
  target: string;
}

export interface SandboxEgressFaultHandle {
  readonly sandboxId: string;
  readonly target: string;
  readonly previousAction: EgressRuleAction | null;
  readonly active: boolean;
  restore(): Promise<void>;
}

interface PodRecord {
  metadata?: {
    name?: unknown;
    deletionTimestamp?: unknown;
  };
  status?: {
    phase?: unknown;
    podIP?: unknown;
  };
}

interface RuntimeProbe<T> {
  matches: T[];
  diagnostic: string;
}

function localCommand(command: string): SandboxCommandRunner {
  return (args, timeoutMs = 30_000) => {
    // spawnSync closes pipes at timeout; an inherited file retains the Go SIGQUIT dump.
    const stderrRoot = command === 'kubectl' ? mkdtempSync(join(tmpdir(), 'astrabox-kubectl-')) : null;
    const stderrPath = stderrRoot ? join(stderrRoot, 'stderr') : null;
    const stderrFd = stderrPath ? openSync(stderrPath, 'wx', 0o600) : null;
    try {
      const result = spawnSync(command, command === 'kubectl' ? ['--v=6', ...args] : args, {
        encoding: 'utf8',
        timeout: timeoutMs,
        killSignal: command === 'kubectl' ? 'SIGQUIT' : 'SIGTERM',
        stdio: ['ignore', 'pipe', stderrFd ?? 'pipe'],
        maxBuffer: 64 * 1024 * 1024,
      });
      return {
        status: result.status,
        stdout: result.stdout || '',
        stderr: stderrPath ? readFileSync(stderrPath, 'utf8') : result.stderr || '',
        error: result.error,
      };
    } finally {
      if (stderrFd !== null) closeSync(stderrFd);
      if (stderrPath) unlinkSync(stderrPath);
      if (stderrRoot) rmdirSync(stderrRoot);
    }
  };
}

const runLocalDocker = localCommand('docker');
const runLocalKubectl = localCommand('kubectl');

function commandDiagnostic(result: SandboxCommandResult): string {
  const detail = [result.error?.message, result.stderr || result.stdout]
    .filter(Boolean).join('\n').trim();
  const status = result.status === null ? 'no exit status' : `exit ${result.status}`;
  return detail ? `${status}: ${detail}` : status;
}

function failedCommand(
  operation: string,
  command: string,
  args: string[],
  result: SandboxCommandResult,
): Error {
  const error = new Error(
    `sandboxOps: ${operation} failed: \`${command} ${args.join(' ')}\` (${commandDiagnostic(result)})`,
  ) as Error & { stdout?: string; stderr?: string };
  error.stdout = result.stdout;
  error.stderr = result.stderr;
  return error;
}

function endpointHost(endpoint: string): string {
  const value = String(endpoint || '').trim();
  if (!value) return '';
  try {
    const parsed = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(value) ? value : `http://${value}`);
    return parsed.hostname.replace(/^\[|\]$/g, '');
  } catch {
    return '';
  }
}

function parsePods(result: SandboxCommandResult): PodRecord[] {
  let payload: unknown;
  try {
    payload = JSON.parse(result.stdout);
  } catch (error) {
    throw new Error(
      `sandboxOps: kubectl returned invalid Pod JSON: ${(error as Error).message}`,
    );
  }
  if (!payload || typeof payload !== 'object' || !Array.isArray((payload as { items?: unknown }).items)) {
    throw new Error('sandboxOps: kubectl Pod JSON has no items array');
  }
  return (payload as { items: unknown[] }).items.filter(
    (item): item is PodRecord => Boolean(item) && typeof item === 'object',
  );
}

function dockerProbe(
  sandboxId: string,
  runDocker: SandboxCommandRunner,
): RuntimeProbe<string> {
  const container = sandboxContainerName(sandboxId);
  const result = runDocker(['inspect', '--format', '{{.State.Running}}', container]);
  if (result.error || result.status !== 0) {
    return { matches: [], diagnostic: commandDiagnostic(result) };
  }
  const running = result.stdout.trim();
  if (running !== 'true' && running !== 'false') {
    return {
      matches: [],
      diagnostic: `exit 0 with unexpected running state ${JSON.stringify(running)}`,
    };
  }
  return {
    matches: [container],
    diagnostic: `container ${container} exists (running=${running})`,
  };
}

function kubernetesProbe(
  endpoint: string,
  namespace: string,
  runKubectl: SandboxCommandRunner,
): RuntimeProbe<string> {
  const host = endpointHost(endpoint);
  if (!host) {
    return {
      matches: [],
      diagnostic: `endpoint ${JSON.stringify(endpoint || '<none>')} has no valid host`,
    };
  }
  // Level 6 records request timing without HTTP headers or response bodies.
  const args = ['get', 'pods', '-n', namespace, '-o', 'json'];
  const result = runKubectl(args);
  if (result.error || result.status !== 0) {
    return { matches: [], diagnostic: `kubectl ${args.join(' ')}: ${commandDiagnostic(result)}` };
  }
  let pods: PodRecord[];
  try {
    pods = parsePods(result);
  } catch (error) {
    return { matches: [], diagnostic: (error as Error).message };
  }
  const matches = pods
    .filter((pod) => String(pod.status?.podIP || '') === host)
    .map((pod) => String(pod.metadata?.name || '').trim())
    .filter(Boolean);
  return {
    matches,
    diagnostic: matches.length > 0
      ? `endpoint host ${host} matches Pod ${matches.join(', ')}`
      : `no Pod has status.podIP=${host}`,
  };
}

/** The docker container NAME of a sandbox — the OpenSandbox docker service
 * names containers `sandbox-<id>`; the bare id is not addressable. */
export function sandboxContainerName(sandboxId: string): string {
  return `sandbox-${sandboxId}`;
}

/**
 * Resolve the platform's sandbox identity to exactly one substrate object.
 *
 * The OpenSandbox backend name cannot distinguish its Docker and Kubernetes
 * server runtimes. The positive runtime signals are instead the deterministic
 * Docker container name and the Pod IP published as the sandbox endpoint. A
 * zero- or multi-match handle is a harness error, never an implicit skip.
 */
export function resolveSandboxHandle(
  locator: SandboxLocator,
  options: SandboxOperationOptions = {},
): SandboxHandle {
  const sandboxId = String(locator.sandboxId || '').trim();
  const endpoint = String(locator.endpoint || '').trim();
  if (!sandboxId) throw new Error('sandboxOps: cannot resolve a sandbox handle without sandbox_id');

  const namespace = String(options.namespace ?? KUBE_NAMESPACE).trim();
  if (!namespace) throw new Error('sandboxOps: Kubernetes namespace must not be empty');
  const docker = dockerProbe(sandboxId, options.runDocker ?? runLocalDocker);
  const kubernetes = kubernetesProbe(
    endpoint,
    namespace,
    options.runKubectl ?? runLocalKubectl,
  );
  const count = docker.matches.length + kubernetes.matches.length;
  if (count !== 1) {
    const reason = count === 0 ? 'does not resolve to live compute' : 'resolves ambiguously';
    throw new Error(
      `sandboxOps: sandbox ${JSON.stringify(sandboxId)} ${reason} ` +
        `(endpoint=${JSON.stringify(endpoint || '<none>')}). ` +
        `Docker probe \`docker inspect ${sandboxContainerName(sandboxId)}\`: ${docker.diagnostic}. ` +
        `Kubernetes probe \`kubectl get pods -n ${namespace} -o wide\`: ${kubernetes.diagnostic}. ` +
        'Run the spec where the owning Docker daemon or Kubernetes cluster is reachable; ' +
        'for Kubernetes, set KUBECONFIG and ASTRABOX_E2E_KUBE_NAMESPACE to that cluster and namespace.',
    );
  }
  if (docker.matches.length === 1) {
    return {
      runtime: 'docker',
      sandboxId,
      endpoint,
      container: docker.matches[0],
    };
  }
  return {
    runtime: 'kubernetes',
    sandboxId,
    endpoint,
    namespace,
    pod: kubernetes.matches[0],
  };
}

/** Resolve a sandbox through the backend detail the platform itself publishes. */
export async function requireSandboxHandle(
  api: AstraApi,
  sandboxId: string,
  options: SandboxOperationOptions = {},
): Promise<SandboxHandle> {
  const resolvedId = String(sandboxId || '').trim();
  if (!resolvedId) throw new Error('sandboxOps: cannot load a sandbox handle without sandbox_id');
  const detail = await api.data<Record<string, unknown>>(
    'GET',
    `/admin/sandboxes/${resolvedId}`,
  );
  const describedId = String(detail?.sandbox_id || '').trim();
  if (describedId && describedId !== resolvedId) {
    throw new Error(
      `sandboxOps: admin detail for ${resolvedId} described a different sandbox_id ${describedId}`,
    );
  }
  return resolveSandboxHandle({
    sandboxId: resolvedId,
    endpoint: String(detail?.endpoint || '').trim(),
  }, options);
}

function parseEgressRules(posture: SandboxSecurityPosture, sandboxId: string): EgressRule[] {
  if (!Array.isArray(posture.egress_rules)) {
    throw new Error(
      `sandboxOps: sandbox ${sandboxId} security read-back has no egress_rules array`,
    );
  }
  return posture.egress_rules.map((raw, index) => {
    if (!raw || typeof raw !== 'object') {
      throw new Error(
        `sandboxOps: sandbox ${sandboxId} egress rule ${index} is not an object`,
      );
    }
    const candidate = raw as Record<string, unknown>;
    const action = String(candidate.action || '').trim().toLowerCase();
    const target = String(candidate.target || '').trim();
    if ((action !== 'allow' && action !== 'deny') || !target) {
      throw new Error(
        `sandboxOps: sandbox ${sandboxId} egress rule ${index} is invalid: `
          + JSON.stringify({ action: candidate.action, target: candidate.target }),
      );
    }
    return { action, target };
  });
}

async function readSandboxSecurity(
  api: AstraApi,
  sandboxId: string,
): Promise<{ defaultAction: EgressRuleAction; rules: EgressRule[] }> {
  const posture = await api.data<SandboxSecurityPosture>(
    'GET',
    `/admin/sandboxes/${encodeURIComponent(sandboxId)}/security`,
  );
  if (posture.available !== true) {
    const detail = String(posture.detail || 'the sandbox did not report an egress policy').trim();
    throw new Error(
      `sandboxOps: sandbox ${sandboxId} has no observable egress policy: ${detail}. `
        + 'This fault requires the OpenSandbox egress sidecar and a network policy on the box; '
        + 'the backend must also run with ASTRABOX_E2E_FAULTS=1.',
    );
  }
  const defaultAction = String(posture.default_action || '').trim().toLowerCase();
  if (defaultAction !== 'allow' && defaultAction !== 'deny') {
    throw new Error(
      `sandboxOps: sandbox ${sandboxId} security read-back has invalid default_action `
        + JSON.stringify(posture.default_action),
    );
  }
  return { defaultAction, rules: parseEgressRules(posture, sandboxId) };
}

function ruleForTarget(
  rules: EgressRule[],
  target: string,
  sandboxId: string,
): EgressRule | null {
  const matches = rules.filter(
    (rule) => rule.target.toLowerCase() === target.toLowerCase(),
  );
  if (matches.length > 1) {
    throw new Error(
      `sandboxOps: sandbox ${sandboxId} reports ${matches.length} egress rules for ${target}; `
        + 'the fault cannot save one unambiguous state to restore',
    );
  }
  return matches[0] ?? null;
}

async function mutateSandboxEgress(
  api: AstraApi,
  sandboxId: string,
  target: string,
  operation: 'patch' | 'delete',
  action?: EgressRuleAction,
): Promise<void> {
  const route = `/admin/e2e/sandboxes/${encodeURIComponent(sandboxId)}/egress`;
  try {
    await api.data(
      'POST',
      route,
      operation === 'patch'
        ? { operation, action, target }
        : { operation, target },
    );
  } catch (error) {
    throw new Error(
      `sandboxOps: ${operation} egress fault for sandbox ${sandboxId}, target ${target} failed. `
        + `The test-only route ${route} exists only when the backend starts with `
        + `ASTRABOX_E2E_FAULTS=1, and the selected sandbox backend must implement live `
        + `egress mutation. Backend response: ${(error as Error).message}`,
    );
  }
}

function assertTargetState(
  state: { defaultAction: EgressRuleAction; rules: EgressRule[] },
  sandboxId: string,
  target: string,
  expectedAction: EgressRuleAction | null,
  expectedDefaultAction: EgressRuleAction,
  operation: string,
): void {
  const observed = ruleForTarget(state.rules, target, sandboxId);
  if (
    state.defaultAction !== expectedDefaultAction
    || (observed?.action ?? null) !== expectedAction
  ) {
    throw new Error(
      `sandboxOps: ${operation} egress fault for sandbox ${sandboxId}, target ${target} `
        + 'did not converge on read-back: '
        + JSON.stringify({
          expected: { defaultAction: expectedDefaultAction, action: expectedAction },
          observed: { defaultAction: state.defaultAction, action: observed?.action ?? null },
        }),
    );
  }
}

/** Deny one destination and return an exact, read-back-confirmed restore handle. */
export async function setSandboxEgressFault(
  api: AstraApi,
  sandboxId: string,
  target: string,
): Promise<SandboxEgressFaultHandle> {
  const resolvedId = String(sandboxId || '').trim();
  const resolvedTarget = String(target || '').trim();
  if (!resolvedId) throw new Error('sandboxOps: cannot fault egress without sandbox_id');
  if (!resolvedTarget) throw new Error('sandboxOps: cannot fault egress without a target');

  const before = await readSandboxSecurity(api, resolvedId);
  const previousRule = ruleForTarget(before.rules, resolvedTarget, resolvedId);
  const previousAction = previousRule?.action ?? null;
  const mutationTarget = previousRule?.target ?? resolvedTarget;
  const effectiveAction = previousAction ?? before.defaultAction;
  if (effectiveAction !== 'allow') {
    throw new Error(
      `sandboxOps: sandbox ${resolvedId} already denies egress to ${resolvedTarget} `
        + `(rule=${previousAction ?? 'absent'}, default=${before.defaultAction}); `
        + 'there is no reachable-to-block transition for this fault to prove',
    );
  }

  const restoreMutation = async (): Promise<void> => {
    if (previousAction === null) {
      await mutateSandboxEgress(api, resolvedId, mutationTarget, 'delete');
    } else {
      await mutateSandboxEgress(api, resolvedId, mutationTarget, 'patch', previousAction);
    }
  };

  await mutateSandboxEgress(api, resolvedId, mutationTarget, 'patch', 'deny');
  try {
    const denied = await readSandboxSecurity(api, resolvedId);
    assertTargetState(
      denied,
      resolvedId,
      resolvedTarget,
      'deny',
      before.defaultAction,
      'setting',
    );
  } catch (error) {
    try {
      await restoreMutation();
      const rolledBack = await readSandboxSecurity(api, resolvedId);
      assertTargetState(
        rolledBack,
        resolvedId,
        resolvedTarget,
        previousAction,
        before.defaultAction,
        'rolling back',
      );
    } catch (restoreError) {
      throw new Error(
        `${(error as Error).message}; automatic rollback also failed and the deny rule may `
          + `still be active: ${(restoreError as Error).message}`,
      );
    }
    throw error;
  }

  let active = true;
  return {
    sandboxId: resolvedId,
    target: resolvedTarget,
    previousAction,
    get active() { return active; },
    async restore(): Promise<void> {
      if (!active) return;
      await restoreMutation();
      const restored = await readSandboxSecurity(api, resolvedId);
      assertTargetState(
        restored,
        resolvedId,
        resolvedTarget,
        previousAction,
        before.defaultAction,
        'restoring',
      );
      active = false;
    },
  };
}

/**
 * The Pod backing a sandbox, found by the address the platform resolved for it.
 *
 * A sandbox is a container ONLY on the docker runtime. Under
 * ASTRABOX_SANDBOX_SERVER_RUNTIME=kubernetes it is a Pod, and the lifecycle
 * record does not expose that Pod name. The platform publishes the sandbox's
 * resolved `endpoint`; matching its host against Pod IPs identifies the Pod
 * without relying on a server-Pool label that standard creates do not carry.
 *
 * Returns '' when nothing matches (docker runtime, or the box is gone).
 */
export function sandboxPodForEndpoint(endpoint: string, namespace = KUBE_NAMESPACE): string {
  const host = endpointHost(endpoint);
  if (!host) return '';
  const args = ['get', 'pods', '-n', namespace, '-o', 'json'];
  const result = runLocalKubectl(args);
  if (result.error || result.status !== 0) {
    throw failedCommand(`looking up the Pod for endpoint ${endpoint}`, 'kubectl', args, result);
  }
  const matches = parsePods(result)
    .filter((pod) => String(pod.status?.podIP || '') === host)
    .map((pod) => String(pod.metadata?.name || '').trim())
    .filter(Boolean);
  if (matches.length > 1) {
    throw new Error(
      `sandboxOps: endpoint ${endpoint} resolves ambiguously to Pods ${matches.join(', ')} ` +
        `in namespace ${namespace}`,
    );
  }
  return matches[0] ?? '';
}

/** Image used by the workload container in an OpenSandbox Kubernetes Pool. */
export function sandboxPoolWorkloadImage(
  poolName: string,
  namespace = KUBE_NAMESPACE,
): string {
  const raw = execFileSync(
    'kubectl',
    ['get', 'pool', poolName, '-n', namespace, '-o', 'json'],
    {
      encoding: 'utf8',
      timeout: 30_000,
      stdio: ['ignore', 'pipe', 'pipe'],
      maxBuffer: 64 * 1024 * 1024,
    },
  );
  const pool = JSON.parse(raw) as Record<string, any>;
  const containers = (pool?.spec?.template?.spec?.containers ?? []) as Array<
    Record<string, unknown>
  >;
  const workload = containers.find((item) => String(item.name || '') === 'sandbox');
  const image = String(workload?.image || '').trim();
  if (!image) {
    throw new Error(
      `sandboxOps: Pool ${namespace}/${poolName} has no sandbox workload image`,
    );
  }
  return image;
}

/** Run a shell script through the one runtime fixed in the sandbox handle. */
export function sandboxExec(
  handle: SandboxHandle,
  script: string,
  timeoutMs = 30_000,
  options: SandboxOperationOptions = {},
): string {
  const command = handle.runtime === 'docker' ? 'docker' : 'kubectl';
  const args = handle.runtime === 'docker'
    ? ['exec', handle.container, 'bash', '-c', script]
    : ['exec', '-n', handle.namespace, handle.pod, '-c', 'sandbox', '--', 'bash', '-c', script];
  const runner = handle.runtime === 'docker'
    ? options.runDocker ?? runLocalDocker
    : options.runKubectl ?? runLocalKubectl;
  const result = runner(args, timeoutMs);
  if (result.error || result.status !== 0) {
    throw failedCommand(`exec in sandbox ${handle.sandboxId}`, command, args, result);
  }
  return result.stdout;
}

/** Kill the sandbox through its substrate ownership root without notifying the platform. */
export function killSandbox(
  handle: SandboxHandle,
  options: SandboxOperationOptions = {},
): void {
  const command = handle.runtime === 'docker' ? 'docker' : 'kubectl';
  const args = handle.runtime === 'docker'
    ? ['kill', '--signal', 'KILL', handle.container]
    : ['delete', 'batchsandbox', handle.sandboxId, '-n', handle.namespace];
  const runner = handle.runtime === 'docker'
    ? options.runDocker ?? runLocalDocker
    : options.runKubectl ?? runLocalKubectl;
  const result = runner(args, 60_000);
  if (result.error || result.status !== 0) {
    throw failedCommand(`killing sandbox ${handle.sandboxId}`, command, args, result);
  }
}

function dockerTargetAbsent(result: SandboxCommandResult): boolean {
  return /no such (container|object)/i.test(`${result.stderr}\n${result.error?.message || ''}`);
}

function kubernetesTargetAbsent(result: SandboxCommandResult): boolean {
  return /\bnotfound\b|\bnot found\b/i.test(`${result.stderr}\n${result.error?.message || ''}`);
}

/** True while the sandbox ownership root or its resolved compute can still run work. */
export function sandboxRunning(
  handle: SandboxHandle,
  options: SandboxOperationOptions = {},
): boolean {
  if (handle.runtime === 'docker') {
    const args = ['inspect', '--format', '{{.State.Running}}', handle.container];
    const result = (options.runDocker ?? runLocalDocker)(args);
    if (result.error || result.status !== 0) {
      if (dockerTargetAbsent(result)) return false;
      throw failedCommand(`checking sandbox ${handle.sandboxId}`, 'docker', args, result);
    }
    const running = result.stdout.trim();
    if (running !== 'true' && running !== 'false') {
      throw new Error(
        `sandboxOps: docker reported invalid running state ${JSON.stringify(running)} ` +
          `for sandbox ${handle.sandboxId}`,
      );
    }
    return running === 'true';
  }

  const runKubectl = options.runKubectl ?? runLocalKubectl;
  const batchSandboxArgs = [
    'get', 'batchsandbox', handle.sandboxId, '-n', handle.namespace, '-o', 'name',
  ];
  const batchSandboxResult = runKubectl(batchSandboxArgs);
  let batchSandboxExists = true;
  if (batchSandboxResult.error || batchSandboxResult.status !== 0) {
    if (kubernetesTargetAbsent(batchSandboxResult)) {
      batchSandboxExists = false;
    } else {
      throw failedCommand(
        `checking BatchSandbox for sandbox ${handle.sandboxId}`,
        'kubectl',
        batchSandboxArgs,
        batchSandboxResult,
      );
    }
  }

  const podArgs = ['get', 'pod', handle.pod, '-n', handle.namespace, '-o', 'json'];
  const podResult = runKubectl(podArgs);
  if (podResult.error || podResult.status !== 0) {
    if (kubernetesTargetAbsent(podResult)) return batchSandboxExists;
    throw failedCommand(`checking sandbox ${handle.sandboxId}`, 'kubectl', podArgs, podResult);
  }
  let pod: PodRecord;
  try {
    const parsed = JSON.parse(podResult.stdout) as unknown;
    if (!parsed || typeof parsed !== 'object') throw new Error('response is not an object');
    pod = parsed as PodRecord;
  } catch (error) {
    throw new Error(
      `sandboxOps: kubectl returned invalid JSON for Pod ${handle.namespace}/${handle.pod}: ` +
        `${(error as Error).message}`,
    );
  }
  const phase = String(pod.status?.phase || '');
  if (!phase) {
    throw new Error(
      `sandboxOps: Pod ${handle.namespace}/${handle.pod} has no status.phase`,
    );
  }
  // BatchSandbox is the lifecycle root: a missing Pod is replaceable while its
  // CR exists. Once the CR is gone, the old Pod must also be absent or terminal
  // before a spec can dispatch without racing the deletion cascade.
  const podCanRun = phase !== 'Failed' && phase !== 'Succeeded';
  return batchSandboxExists || podCanRun;
}

/** Neither the ownership root nor its resolved compute can run work when this resolves. */
export async function waitForSandboxStopped(
  handle: SandboxHandle,
  timeoutMs = 30_000,
  options: SandboxOperationOptions = {},
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!sandboxRunning(handle, options)) return;
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  const target = handle.runtime === 'docker'
    ? handle.container
    : `BatchSandbox ${handle.namespace}/${handle.sandboxId} or Pod ${handle.namespace}/${handle.pod}`;
  throw new Error(
    `sandboxOps: ${handle.runtime} sandbox ${handle.sandboxId} (${target}) ` +
      `still running after ${timeoutMs}ms`,
  );
}

function dockerText(args: string[], timeoutMs = 30_000): string {
  try {
    return execFileSync('docker', args, {
      encoding: 'utf8',
      timeout: timeoutMs,
      stdio: ['ignore', 'pipe', 'pipe'],
    }).trim();
  } catch (error) {
    const err = error as { code?: string; stderr?: Buffer | string; message?: string };
    if (err.code === 'ENOENT') {
      throw new Error(
        'sandboxOps: the `docker` CLI was not found. Server-restart specs require ' +
          'the deployment service container to be visible to this host; set ' +
          'ASTRABOX_E2E_SERVER_CONTAINER and run on that Docker host.',
      );
    }
    const stderr = typeof err.stderr === 'string' ? err.stderr : err.stderr?.toString() ?? '';
    throw new Error(`sandboxOps: \`docker ${args.join(' ')}\` failed: ${stderr.trim() || err.message || String(error)}`);
  }
}

/**
 * Restart the backend server container and wait for it to come back healthy.
 *
 * Recovery specs use this to simulate an instance replacement (serverless
 * deploy / crash): the sandbox compute keeps running (survive-disconnect),
 * only the platform process restarts. Waits on BOTH the docker healthcheck and
 * the e2e base URL actually answering, so the next API call cannot race the
 * listener.
 * A stop/inject/start caller passes the container resolved before stopping it;
 * ordinary restart callers resolve a running service on entry.
 */
export async function restartServerContainer(
  baseUrl: string,
  timeoutMs = 120_000,
  serverContainer = requireServiceContainer(SERVER_CONTAINER_HANDLE),
): Promise<void> {
  dockerText(['restart', serverContainer], 60_000);
  const deadline = Date.now() + timeoutMs;
  let lastError = '';
  while (Date.now() < deadline) {
    try {
      const health = dockerText([
        'inspect', serverContainer, '--format', '{{.State.Health.Status}}',
      ]).trim();
      if (health === 'healthy') {
        const response = await fetch(`${baseUrl.replace(/\/$/, '')}/healthz`).catch(() => null);
        if (response && response.ok) return;
        lastError = `base URL not answering (health=${health})`;
      } else {
        lastError = `container health=${health}`;
      }
    } catch (error) {
      lastError = (error as Error).message;
    }
    await new Promise((r) => setTimeout(r, 2_000));
  }
  throw new Error(`sandboxOps: server container did not come back healthy within ${timeoutMs}ms: ${lastError}`);
}
