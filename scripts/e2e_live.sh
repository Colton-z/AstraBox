#!/usr/bin/env bash
# Run the live e2e suite and ABORT on the first failure, leaving the scene alone.
#
# Why a script and not just `-x`. `maxfail` stops pytest handing out new tests,
# but the workers already running keep going, and a live test can sit until its
# timeout — cluster capacity spent after the answer is known, and
# minutes before anyone can start reading it. This kills the run at the first
# failed report instead.
#
# What it deliberately does NOT do is clean up. The failing test's sandbox is the
# only reproducible scene there is, a rerun rarely lands in the same place, and
# every box carries the deployment's lease so the mess is bounded anyway. Killing
# the run also skips that test's teardown, which is the same answer arrived at
# from the other direction.
set -uo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: e2e_live.sh [--parallel-node tests/e2e/FILE.py::TEST ...]
                   [--restart-node tests/e2e/FILE.py::TEST ...]

With no node arguments, run the complete selected E2E lane. Explicit node
selection preserves main-lane parallel/restart isolation; an Assistant selection
has no restart group and stays single-worker.
EOF
}

parallel_nodes=()
restart_nodes=()
while (( $# )); do
  case "$1" in
    --parallel-node) parallel_nodes+=("$2"); shift 2 ;;
    --restart-node) restart_nodes+=("$2"); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage; exit 64 ;;
  esac
done

: "${ASTRABOX_E2E_BASE_URL:?set ASTRABOX_E2E_BASE_URL to a running deployment}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKERS="${E2E_WORKERS:-5}"
PYTEST="${ASTRABOX_E2E_PYTHON:-${REPO_ROOT}/.venv/bin/python}"
LOG="${E2E_LOG:-/tmp/astrabox-e2e-live.log}"
LANE="${E2E_LANE:-main}"
JUNIT_DIR="${ASTRABOX_E2E_JUNIT_DIR:-}"
case "$LANE" in
  main|assistant) ;;
  *) printf 'invalid E2E_LANE: %s (expected main or assistant)\n' "$LANE" >&2; exit 64 ;;
esac
if [[ -n "$JUNIT_DIR" ]]; then
  [[ "$JUNIT_DIR" == /* ]] || {
    printf 'ASTRABOX_E2E_JUNIT_DIR must be absolute\n' >&2
    exit 64
  }
  [[ ! -L "$JUNIT_DIR" && ( ! -e "$JUNIT_DIR" || -d "$JUNIT_DIR" ) ]] || {
    printf 'ASTRABOX_E2E_JUNIT_DIR is not a regular directory: %s\n' "$JUNIT_DIR" >&2
    exit 66
  }
  install -d -m 0700 "$JUNIT_DIR" || {
    printf 'cannot create ASTRABOX_E2E_JUNIT_DIR: %s\n' "$JUNIT_DIR" >&2
    exit 73
  }
fi
selection=0
if (( ${#parallel_nodes[@]} || ${#restart_nodes[@]} )); then
  selection=1
  [[ "$LANE" == main || ( "$WORKERS" == 1 && ${#restart_nodes[@]} == 0 ) ]] || {
    printf 'Assistant node selection requires one worker and no restart nodes\n' >&2
    exit 64
  }
  declare -A selected_node_lookup=()
  for node in "${parallel_nodes[@]}" "${restart_nodes[@]}"; do
    [[ "$node" == tests/e2e/test_*.py::* ]] || {
      printf 'invalid selected Python node: %s\n' "$node" >&2
      exit 64
    }
    [[ -z "${selected_node_lookup[$node]:-}" ]] || {
      printf 'duplicate selected Python node: %s\n' "$node" >&2
      exit 64
    }
    selected_node_lookup["$node"]=1
  done
fi
shared_sentinel=0
if [[ -n "${ASTRABOX_E2E_FAILURE_SENTINEL:-}" ]]; then
  SENTINEL="$ASTRABOX_E2E_FAILURE_SENTINEL"
  shared_sentinel=1
  [[ "$SENTINEL" == /* && "$LANE" == main && ${#parallel_nodes[@]} -gt 0 && ${#restart_nodes[@]} == 0 ]] || {
    printf 'shared first-red signal requires an absolute path and independent main nodes\n' >&2
    exit 64
  }
else
  SENTINEL="$(mktemp -u /tmp/astrabox-e2e-failure.XXXXXX)"
fi
# Stop on the first failure by default to preserve the scene for diagnosis.
# Survey mode collects all failures in a standalone lane by disabling the
# stop signal; failure-scene retention still applies. Shared batches require
# the signal so sibling lanes stop together.
ABORT_ON_FIRST_FAILURE="${E2E_ABORT_ON_FIRST_FAILURE:-1}"
if [ "$ABORT_ON_FIRST_FAILURE" = "0" ]; then
  (( ! shared_sentinel )) || { printf 'shared batches require first-red abort\n' >&2; exit 64; }
  echo "          SURVEY MODE: runs to the end and reports every failure"
else
  export ASTRABOX_E2E_FAILURE_SENTINEL="$SENTINEL"
fi
TEST_TIMEOUT_S="$(python3 - "$REPO_ROOT/tests/e2e-contract/suite-contract.json" <<'PY'
import json
import sys

value = json.load(open(sys.argv[1], encoding="utf-8")).get("max_test_seconds")
if value != 180:
    raise SystemExit(f"live E2E max_test_seconds must be exactly 180, got {value!r}")
print(value)
PY
)"
readonly TEST_TIMEOUT_S

reap_test_stacks() {
  # A killed run skips pytest finalizers, so a fixture-booted backend (the
  # reattach suite boots its own onebox from THIS checkout's venv) survives
  # the group kill and squats on its ports; the next run's boot then dies
  # rc=3 on the bind (measured: an orphan sandbox_server held :8990 across
  # rounds). Only processes started from this repo's venv match — the
  # deployment under test runs elsewhere (a container, another checkout).
  pkill -f "$REPO_ROOT/.venv/bin/python -m astrabox.deploy" 2>/dev/null || true
}

if (( ! shared_sentinel )); then
  reap_test_stacks
  rm -f "$SENTINEL"
fi
: > "$LOG"
echo "live e2e: lane=${LANE}"
echo "          against ${ASTRABOX_E2E_BASE_URL}; log ${LOG}"
echo "          aborts at the FIRST failure and keeps its sandbox"

aborted=0

run_phase() {
  local phase_name="$1" label="$2"
  shift 2
  local -a junit_arguments=()
  if [[ -n "$JUNIT_DIR" ]]; then
    junit_arguments+=(--junitxml="$JUNIT_DIR/$phase_name.xml")
  fi

  echo "live e2e phase: ${label}"
  if [[ -s "$SENTINEL" ]]; then
    aborted=1
    return 1
  fi
  # `setsid` gives this pytest invocation and any xdist workers their OWN
  # process group. The abort can then stop the active phase without killing the
  # reporting shell or leaving a worker holding a sandbox.
  setsid "$PYTEST" -m pytest "$@" \
    --timeout="$TEST_TIMEOUT_S" --timeout-disable-debugger-detection \
    "${junit_arguments[@]}" -q >>"$LOG" 2>&1 &
  PYTEST_PID=$!

  while kill -0 "$PYTEST_PID" 2>/dev/null; do
    if [ -s "$SENTINEL" ]; then
      aborted=1
      kill -TERM -"$PYTEST_PID" 2>/dev/null || kill -TERM "$PYTEST_PID" 2>/dev/null
      sleep 3
      kill -KILL -"$PYTEST_PID" 2>/dev/null || true
      break
    fi
    sleep 2
  done
  wait "$PYTEST_PID" 2>/dev/null
}

# Only the main lane has a restart phase. Each specialized lane contains one
# test on its own deployment; an empty restart invocation is an error in pytest.
case "$LANE" in
  main)
    # A scheduling group only serializes its own members on one worker; unmarked
    # workers can still call the deployment while that worker restarts it. Finish
    # every non-restart test first, then start a separate phase with no xdist worker.
    if (( selection )); then
      rc=0
      if (( ${#parallel_nodes[@]} )); then
        parallel_targets=()
        for node in "${parallel_nodes[@]}"; do
          parallel_targets+=("${REPO_ROOT}/${node}")
        done
        run_phase parallel "selected main parallel nodes" \
          "${parallel_targets[@]}" \
          -m "e2e and not backend_restart and not assistant_live" \
          -n "$WORKERS" --dist loadgroup
        rc=$?
      fi
      if [ "$rc" -eq 0 ] && (( ${#restart_nodes[@]} )); then
        restart_targets=()
        for node in "${restart_nodes[@]}"; do
          restart_targets+=("${REPO_ROOT}/${node}")
        done
        run_phase restart "selected main backend restart nodes" \
          "${restart_targets[@]}" \
          -m "e2e and backend_restart and not assistant_live"
        rc=$?
      fi
    else
      run_phase parallel "main parallel (excluding backend restarts)" \
        "${REPO_ROOT}/tests/e2e" \
        -m "e2e and not backend_restart and not assistant_live" \
        -n "$WORKERS" --dist loadgroup
      rc=$?
      if [ "$rc" -eq 0 ]; then
        run_phase restart "main backend restarts (exclusive)" \
          "${REPO_ROOT}/tests/e2e" \
          -m "e2e and backend_restart and not assistant_live"
        rc=$?
      fi
    fi
    ;;
  assistant)
    assistant_targets=("${REPO_ROOT}/tests/e2e")
    if (( selection )); then
      assistant_targets=()
      for node in "${parallel_nodes[@]}"; do
        assistant_targets+=("${REPO_ROOT}/${node}")
      done
    fi
    run_phase assistant "Assistant/Hermes contract" \
      "${assistant_targets[@]}" \
      -m "e2e and assistant_live and not backend_restart"
    rc=$?
    ;;
esac

if [ "$aborted" -eq 1 ] && (( ! shared_sentinel )); then
  reap_test_stacks
fi

if [ "$aborted" -eq 1 ]; then
  echo
  echo "=============================================================="
  echo "ABORTED at the first failure: $(head -n1 "$SENTINEL" | cut -f1)"
  echo "=============================================================="
  echo "--- why (captured from the report, since an aborted run prints no summary) ---"
  tail -n +2 "$SENTINEL"
  echo
  echo "--- the scene, still running ---"
  if command -v kubectl >/dev/null 2>&1; then
    kubectl get pods -n "${ASTRABOX_SANDBOX_NAMESPACE:-opensandbox}" 2>/dev/null | tail -n +1
  fi
  echo
  echo "Nothing was cleaned up. Read it before you rerun — see the 'e2e' skill."
  if (( ! shared_sentinel )); then rm -f "$SENTINEL"; fi
  exit 1
fi

if (( ! shared_sentinel )); then rm -f "$SENTINEL"; fi
tail -n 5 "$LOG"
exit "$rc"
