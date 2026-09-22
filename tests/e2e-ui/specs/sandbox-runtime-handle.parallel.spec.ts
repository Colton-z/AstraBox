import { expect, test } from '@playwright/test';

import {
  killSandbox,
  resolveSandboxHandle,
  sandboxRunning,
  type SandboxHandle,
} from '../fixtures/sandboxOps';

const missingDocker = () => ({
  status: 1,
  stdout: '',
  stderr: 'Error: No such object: sandbox-box-1',
});

test('a Kubernetes sandbox handle follows the endpoint to its Pod', () => {
  const handle = resolveSandboxHandle(
    { sandboxId: 'box-1', endpoint: 'http://10.42.0.17:8000' },
    {
      namespace: 'sandbox-system',
      runDocker: missingDocker,
      runKubectl: () => ({
        status: 0,
        stdout: JSON.stringify({
          items: [
            {
              metadata: { name: 'pool-sandbox-7' },
              status: { phase: 'Running', podIP: '10.42.0.17' },
            },
          ],
        }),
        stderr: '',
      }),
    },
  );

  expect(handle).toEqual({
    runtime: 'kubernetes',
    sandboxId: 'box-1',
    endpoint: 'http://10.42.0.17:8000',
    namespace: 'sandbox-system',
    pod: 'pool-sandbox-7',
  });
});

test('Kubernetes out-of-band death removes the BatchSandbox that owns replaceable Pods', () => {
  const handle: SandboxHandle = {
    runtime: 'kubernetes',
    sandboxId: 'box-1',
    endpoint: 'http://10.42.0.17:8000',
    namespace: 'sandbox-system',
    pod: 'box-1-0',
  };
  const kubectlCalls: string[][] = [];
  let batchSandboxExists = true;
  let podExists = true;
  const runKubectl = (args: string[]) => {
    kubectlCalls.push(args);
    if (args[0] === 'delete' && args[1] === 'batchsandbox') {
      batchSandboxExists = false;
      podExists = false;
      return { status: 0, stdout: 'batchsandbox.sandbox.opensandbox.io "box-1" deleted', stderr: '' };
    }
    if (args[0] === 'delete' && args[1] === 'pod') {
      podExists = false;
      if (batchSandboxExists) podExists = true;
      return { status: 0, stdout: 'pod "box-1-0" deleted', stderr: '' };
    }
    if (args[0] === 'get' && args[1] === 'batchsandbox') {
      return batchSandboxExists
        ? { status: 0, stdout: 'batchsandbox.sandbox.opensandbox.io/box-1', stderr: '' }
        : { status: 1, stdout: '', stderr: 'Error from server (NotFound): batchsandboxes "box-1" not found' };
    }
    if (args[0] === 'get' && args[1] === 'pod') {
      return podExists
        ? { status: 0, stdout: JSON.stringify({ status: { phase: 'Running' } }), stderr: '' }
        : { status: 1, stdout: '', stderr: 'Error from server (NotFound): pods "box-1-0" not found' };
    }
    throw new Error(`unexpected kubectl call: ${args.join(' ')}`);
  };

  killSandbox(handle, {
    runDocker: () => {
      throw new Error('the Kubernetes handle must not dispatch through Docker');
    },
    runKubectl,
  });
  expect(
    sandboxRunning(handle, { runKubectl }),
    'the kill must remove the CR so its controller cannot revive the Pod',
  ).toBe(false);
  expect(kubectlCalls[0]).toEqual([
    'delete', 'batchsandbox', 'box-1', '-n', 'sandbox-system',
  ]);
});

test('a Kubernetes sandbox is stopped only after both its CR and Pod converge', () => {
  const handle: SandboxHandle = {
    runtime: 'kubernetes',
    sandboxId: 'box-3',
    endpoint: 'http://10.42.0.19:8000',
    namespace: 'sandbox-system',
    pod: 'box-3-0',
  };
  let batchSandboxExists = false;
  let podState: 'Running' | 'Succeeded' | 'missing' = 'Running';
  const runKubectl = (args: string[]) => {
    if (args[1] === 'batchsandbox') {
      return batchSandboxExists
        ? { status: 0, stdout: 'batchsandbox.sandbox.opensandbox.io/box-3', stderr: '' }
        : { status: 1, stdout: '', stderr: 'Error from server (NotFound): batchsandboxes "box-3" not found' };
    }
    if (args[1] === 'pod') {
      return podState === 'missing'
        ? { status: 1, stdout: '', stderr: 'Error from server (NotFound): pods "box-3-0" not found' }
        : { status: 0, stdout: JSON.stringify({ status: { phase: podState } }), stderr: '' };
    }
    throw new Error(`unexpected kubectl call: ${args.join(' ')}`);
  };

  expect(
    sandboxRunning(handle, { runKubectl }),
    'a Pod can keep running while CR deletion cascades',
  ).toBe(true);

  podState = 'Succeeded';
  expect(sandboxRunning(handle, { runKubectl })).toBe(false);

  batchSandboxExists = true;
  podState = 'missing';
  expect(
    sandboxRunning(handle, { runKubectl }),
    'a live CR can replace a missing Pod and is not stopped',
  ).toBe(true);
});

test('a Docker sandbox handle uses the deterministic OpenSandbox container name', () => {
  const dockerCalls: string[][] = [];
  const handle = resolveSandboxHandle(
    { sandboxId: 'box-2', endpoint: 'http://127.0.0.1:49152' },
    {
      runDocker: (args) => {
        dockerCalls.push(args);
        return { status: 0, stdout: 'true\n', stderr: '' };
      },
      runKubectl: () => ({
        status: 0,
        stdout: JSON.stringify({ items: [] }),
        stderr: '',
      }),
    },
  );

  expect(handle).toEqual({
    runtime: 'docker',
    sandboxId: 'box-2',
    endpoint: 'http://127.0.0.1:49152',
    container: 'sandbox-box-2',
  });

  dockerCalls.length = 0;
  killSandbox(handle, {
    runDocker: (args) => {
      dockerCalls.push(args);
      return { status: 0, stdout: 'sandbox-box-2', stderr: '' };
    },
    runKubectl: () => {
      throw new Error('the Docker handle must not dispatch through kubectl');
    },
  });
  expect(dockerCalls).toEqual([
    ['kill', '--signal', 'KILL', 'sandbox-box-2'],
  ]);
});

test('a locator that names no sandbox fails with both probes and rerun instructions', () => {
  let message = '';
  try {
    resolveSandboxHandle(
      { sandboxId: 'missing-box', endpoint: 'http://10.42.0.99:8000' },
      {
        namespace: 'sandbox-system',
        runDocker: () => ({
          status: 1,
          stdout: '',
          stderr: 'Error: No such object: sandbox-missing-box',
        }),
        runKubectl: () => ({
          status: 0,
          stdout: JSON.stringify({ items: [] }),
          stderr: '',
        }),
      },
    );
  } catch (error) {
    message = (error as Error).message;
  }

  expect(message).toContain('sandbox "missing-box" does not resolve to live compute');
  expect(message).toContain('docker inspect sandbox-missing-box');
  expect(message).toContain('kubectl get pods -n sandbox-system -o wide');
  expect(message).toContain('no Pod has status.podIP=10.42.0.99');
  expect(message).toContain('set KUBECONFIG and ASTRABOX_E2E_KUBE_NAMESPACE');
});

test('conflicting runtime signals fail instead of choosing a target', () => {
  expect(() => resolveSandboxHandle(
    { sandboxId: 'ambiguous-box', endpoint: 'http://10.42.0.20:8000' },
    {
      namespace: 'sandbox-system',
      runDocker: () => ({ status: 0, stdout: 'true\n', stderr: '' }),
      runKubectl: () => ({
        status: 0,
        stdout: JSON.stringify({
          items: [
            {
              metadata: { name: 'also-this-pod' },
              status: { phase: 'Running', podIP: '10.42.0.20' },
            },
          ],
        }),
        stderr: '',
      }),
    },
  )).toThrow('sandbox "ambiguous-box" resolves ambiguously');
});

test('a stale resolved handle fails the kill with its exact target diagnostic', () => {
  const handle: SandboxHandle = {
    runtime: 'kubernetes',
    sandboxId: 'gone-box',
    endpoint: 'http://10.42.0.18:8000',
    namespace: 'sandbox-system',
    pod: 'gone-pod',
  };

  expect(() => killSandbox(handle, {
    runKubectl: () => ({
      status: 1,
      stdout: '',
      stderr: 'Error from server (NotFound): batchsandboxes "gone-box" not found',
    }),
  })).toThrow(
    'sandboxOps: killing sandbox gone-box failed: `kubectl delete batchsandbox gone-box -n sandbox-system',
  );
});
