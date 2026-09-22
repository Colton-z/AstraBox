/**
 * Replace the image-baked Python runner inside a spec-owned cold sandbox.
 *
 * The Claude agent image boots `/opt/astrabox/start-runner.sh`, which becomes
 * `runuser -u <workload account> -- env ... python3.12 /opt/astrabox/sandbox_runner.py`.
 * Only that Python child is the runner process a fault may replace; the
 * launcher parent is the image's, not the platform's. The script here finds
 * exactly one such process, terminates it, detaches the caller-supplied
 * replacement with `setsid -f`, and waits for the runner's plain-HTTP
 * `GET /health` contract (`sandbox_runner.py`, `RunnerWsServer._process_request`)
 * before printing the caller's evidence line. Every other outcome exits
 * non-zero with the replacement's log tail on stderr.
 *
 * Two specs share this operation with different replacements: the gap-rebuild
 * spec launches the runner module with a one-envelope compaction bound, and
 * the runner-restart spec launches the image's own launcher unchanged. Neither
 * touches product code; the runner they restart is the one the image installed.
 */

/** Where the Claude image installs the runner (`containers/sandbox-claude-code/Dockerfile`). */
export const IMAGE_RUNNER_PATH = '/opt/astrabox/sandbox_runner.py';
/** The interpreter the image launcher execs for the runner (`start-runner.sh`). */
export const IMAGE_RUNNER_PYTHON = '/usr/local/bin/python3.12';
/** The image launcher that starts the runner under the workload account. */
export const IMAGE_RUNNER_LAUNCHER = '/opt/astrabox/start-runner.sh';
/** The runner's envelope/health port (`sandbox_runner.py`, `RunnerWsServer`). */
export const IMAGE_RUNNER_PORT = 8000;

export interface ImageRunnerRestartOptions {
  /** Abrupt callback loss uses KILL; ordinary replacement retains TERM. */
  signal?: 'TERM' | 'KILL';
  /** Shell command that starts the replacement; it runs detached under `setsid -f`. */
  launch: string;
  /** File receiving the replacement's stdout and stderr. */
  log: string;
  /**
   * Shell line printed once the health contract answers. `$old_pid` is in
   * scope; the caller decides what a passing restart reports.
   */
  evidence: string;
  /** Name used in failure messages, e.g. `compacting runner`. */
  name: string;
}

/**
 * Shell that prints the PID of the single image-baked runner process.
 *
 * Matches a Python interpreter whose last argument is the runner path, so the
 * `runuser` and `env` parents, this script's own `bash -c`, and the awk
 * itself (all of which carry the path in their arguments) never match.
 */
export function imageRunnerPidLookup(): string {
  return [
    `runner_path=${JSON.stringify(IMAGE_RUNNER_PATH)}`,
    `runner_pids="$(ps -eo pid=,comm=,args= | awk -v target="$runner_path" '$2 ~ /^python([0-9]+([.][0-9]+)*)?$/ && $NF == target { print $1 }')"`,
    'set -- $runner_pids',
    'if [ "$#" -ne 1 ]; then',
    '  echo "expected exactly one image-baked Python runner, found $# (${runner_pids:-none})" >&2',
    '  exit 1',
    'fi',
  ].join('\n');
}

/** Python one-liner that fails unless the runner answers its exact health body. */
export function imageRunnerHealthProgram(): string {
  return [
    'import urllib.request',
    `body=urllib.request.urlopen("http://127.0.0.1:${IMAGE_RUNNER_PORT}/health", timeout=0.2).read()`,
    'assert body == b"OK"',
  ].join('; ');
}

/**
 * The restart script: one runner found, terminated, replaced, and healthy.
 *
 * `set -eu` is the first line so a lookup that finds zero or several runners,
 * a runner that ignores TERM, or a replacement that never answers `/health`
 * all fail the `sandboxExec` call instead of leaving a half-restarted box.
 */
export function imageRunnerRestartScript(options: ImageRunnerRestartOptions): string {
  return [
    'set -eu',
    imageRunnerPidLookup(),
    'old_pid="$1"',
    `kill -${options.signal ?? 'TERM'} "$old_pid"`,
    'for _ in $(seq 1 50); do',
    '  if ! kill -0 "$old_pid" 2>/dev/null; then break; fi',
    '  sleep 0.1',
    'done',
    'if kill -0 "$old_pid" 2>/dev/null; then',
    '  echo "image-baked runner $old_pid did not stop" >&2',
    '  ps -o pid,ppid,stat,etime,comm -p "$old_pid" >&2',
    '  exit 1',
    'fi',
    'command -v setsid >/dev/null',
    `setsid -f ${options.launch} >${options.log} 2>&1`,
    'for _ in $(seq 1 100); do',
    `  if ${IMAGE_RUNNER_PYTHON} -c ${JSON.stringify(imageRunnerHealthProgram())} >/dev/null 2>&1; then`,
    `    ${options.evidence}`,
    '    exit 0',
    '  fi',
    '  sleep 0.1',
    'done',
    `echo '${options.name} did not answer its health contract' >&2`,
    `tail -80 ${options.log} >&2 || true`,
    'exit 1',
  ].join('\n');
}
