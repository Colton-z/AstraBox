#!/usr/bin/env bash
# e2e_smoke.sh — curl-level proof of a LIVE AstraBox Community turn (live-turn gate).
#
# This is the headless, no-browser smoke for the one unverified piece of the
# Community bring-up: the live agent turn (the open_sandbox backend driving a
# real sandbox through the OpenSandbox lifecycle server). It exercises the whole
# open path end-to-end:
#
#   source .env (operator LLM creds)  ->  boot the source-mounted maintained
#   Compose deployment  ->  clone the deployment's seeded coding Agent  ->
#   POST /api/v1/sessions (cold-start an agent container)  ->  poll the
#   session to READY  ->  POST
#   /api/v1/sessions/{id}/ai-stream  ->  read the AI-SDK Data-Stream-Protocol SSE
#   and assert one complete assistant text block, a normal stream finish, and a
#   return to READY.
#
# The load-bearing assertion is the SUCCESS-vs-fake-success distinction:
# a turn that opens + finishes with ZERO text-delta frames
# (the literal "(empty reply, exit=0)" signature) is a FAILURE, not an empty
# answer. This script exits NON-ZERO on that signature, on an empty reply, or on
# any stage error — there is no silent fallback. A model
# HTTP failure is the sneakiest fake-success: the CLI surfaces a 402/401/429/5xx
# as an "API Error: ..." result whose "402" even contains a "2" — that is a FAILED
# turn, not an answer, and is excluded from the genuine-text proof and hard-failed.
#
# Prompt: "What is 1 + 1? Reply with just the number." keeps the probe cheap and
# deterministic to execute. Model wording is not the oracle: the proof is the
# platform delivery contract and terminal state. Requiring exactly one complete
# text block also catches a replayed or duplicated response.
#
# Usage:
#   scripts/e2e_smoke.sh                 # boot a fresh server, run, tear it down
#   ASTRABOX_E2E_BASE_URL=http://127.0.0.1:8088 scripts/e2e_smoke.sh
#                                        # reuse an already-running server (no boot)
#
# Requirements: a live local Docker daemon, the astrabox/sandbox-claude-code:latest image
# (`make build-agent-image`), the .env LLM creds (gitignored), and the project .venv.
# Exit codes: 0 = one complete, normally finished response was observed (GREEN);
#             non-zero = any failure (captures evidence to the log dir first).

set -u -o pipefail

# ── locations ───────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ASTRABOX_E2E_ENV_FILE:-${REPO_ROOT}/.env}"
VENV_PY="${ASTRABOX_E2E_PYTHON:-${REPO_ROOT}/.venv/bin/python}"

PORT="${ASTRABOX_E2E_PORT:-8088}"
# Docker label the backend writes per session. NOTE the HYPHEN form — it must
# match the create metadata in
# astrabox/providers/open_sandbox/executor.py ("astrabox.session-id"), which the
# lifecycle server turns into a container label verbatim. NOT the underscore
# "astrabox.session_id", which matches nothing.
SESSION_LABEL="astrabox.session-id"
# When set, reuse this base URL and DO NOT boot/teardown a server.
EXTERNAL_BASE_URL="${ASTRABOX_E2E_BASE_URL:-}"
AUTH_TOKEN_FILE="${ASTRABOX_E2E_AUTH_TOKEN_FILE:-}"

LOG_DIR="${ASTRABOX_E2E_LOG_DIR:-/tmp/astrabox-e2e}"
mkdir -p "${LOG_DIR}"
SERVER_LOG="${LOG_DIR}/server.log"
SSE_OUT="${LOG_DIR}/ai-stream.sse"
HEADERS_OUT="${LOG_DIR}/ai-stream.headers"
SMOKE_RESULT_OUT="${LOG_DIR}/smoke-result.json"
rm -f -- "${SMOKE_RESULT_OUT}"

# Tunables (a cold model endpoint + cold container pull can be slow).
READY_TIMEOUT="${ASTRABOX_E2E_READY_TIMEOUT:-120}"        # secs to reach READY
STREAM_TIMEOUT="${ASTRABOX_E2E_STREAM_TIMEOUT:-180}"       # secs for the SSE turn
HEALTH_TIMEOUT="${ASTRABOX_E2E_HEALTH_TIMEOUT:-60}"        # secs for /healthz
SETTLE_TIMEOUT="${ASTRABOX_E2E_SETTLE_TIMEOUT:-120}"       # secs to return to READY
PROMPT="${ASTRABOX_E2E_PROMPT:-What is 1 + 1? Reply with just the number.}"
EXACT_AGENT_ID="${ASTRABOX_E2E_AGENT_ID:-}"
EXPECTED_ENGINE_KIND="${ASTRABOX_E2E_EXPECTED_ENGINE_KIND:-}"
EXPECTED_RUNTIME_IMAGE="${ASTRABOX_E2E_EXPECTED_RUNTIME_IMAGE:-}"
EXPECTED_RUNTIME_IMAGE_DIGEST="${ASTRABOX_E2E_EXPECTED_RUNTIME_IMAGE_DIGEST:-}"
EXPECTED_SANDBOX_PERMISSION_LEVEL="${ASTRABOX_E2E_EXPECTED_SANDBOX_PERMISSION_LEVEL:-}"
KUBECONFIG_PATH="${KUBECONFIG:-}"
KUBE_NAMESPACE="${ASTRABOX_E2E_KUBE_NAMESPACE:-}"

SERVER_PID=""
SID=""
AGENT_ID=""
AGENT_OWNED=0
SANDBOX_ID=""
ACTUAL_ENGINE_KIND=""
ACTUAL_RUNTIME_IMAGE=""
ACTUAL_IMAGE_DIGEST=""
ACTUAL_RUNTIME_CONTENT_DIGEST=""
OBSERVED_IMAGE_ID_DIGEST=""
ACTUAL_SANDBOX_PERMISSION_LEVEL=""

# Evidence may contain output from third-party services. Treat it as untrusted:
# redact inherited credential values and common token/URL forms before it can
# reach a terminal or CI artifact.
redact_stream() {
  local redactor="${VENV_PY}"
  if [[ ! -x "${redactor}" ]]; then
    redactor="$(command -v python3 2>/dev/null || true)"
  fi
  if [[ -z "${redactor}" ]]; then
    sed -E \
      -e 's/sk-[A-Za-z0-9._~+\/=:-]{8,}/[redacted]/g' \
      -e 's/[0-9A-Fa-f]{48,}/[redacted]/g'
    return
  fi
  "${redactor}" -c '
import os, re, sys

text = sys.stdin.read()
name_pattern = re.compile(
    r"(?:^|_)(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|SECRET|PASSWORD|MASTER_KEY|DATABASE_URL)(?:$|_)",
    re.I,
)
values = sorted(
    {value for name, value in os.environ.items() if len(value) >= 8 and name_pattern.search(name)},
    key=len,
    reverse=True,
)
for value in values:
    text = text.replace(value, "[redacted]")
text = re.sub(
    r"(?i)(\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis)://[^:\s/@]+:)[^@\s/]+(@)",
    r"\1[redacted]\2",
    text,
)
text = re.sub(
    r"(?i)(\b(?:api[ _-]?key|auth[ _-]?token|access[ _-]?token|master[ _-]?key|password)\b[\"'\'' ]*\s*(?:=|:)\s*)([\"'\'']?)([^\s,\"'\''}]+)",
    lambda match: match.group(1) + match.group(2) + "[redacted]",
    text,
)
text = re.sub(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[redacted]", text)
text = re.sub(r"\bsk-[A-Za-z0-9._~+/=-]{8,}\b", "[redacted]", text)
text = re.sub(r"\b[0-9a-fA-F]{48,}\b", "[redacted]", text)
sys.stdout.write(text)
'
}

log()  { printf '[e2e] %s\n' "$*" >&2; }
AUTH_TOKEN=""
api_curl() {
  if [[ -z "${AUTH_TOKEN}" ]]; then
    curl "$@"
    return
  fi
  # Keep the bearer value out of argv, process listings, logs, and artifacts.
  curl --config <(printf 'header = "Authorization: Bearer %s"\n' "${AUTH_TOKEN}") "$@"
}
fail() {
  local safe_message
  if ! safe_message="$(printf '%s' "$*" | redact_stream 2>/dev/null)"; then
    safe_message="failure details unavailable because redaction failed"
  fi
  printf '[e2e][FAIL] %s\n' "${safe_message}" >&2
  capture_evidence
  exit 1
}

prove_kubernetes_runtime_image() {
  local batch_proof batch_response batch_uid
  local architecture node_name node_response operating_system platform proof
  local registry_api runtime_evidence
  local pod_image_id pod_name pods_response
  batch_response="$(kubectl --kubeconfig "${KUBECONFIG_PATH}" \
    --namespace "${KUBE_NAMESPACE}" get batchsandbox "${SANDBOX_ID}" \
    -o json 2>/dev/null)" \
    || fail "could not read BatchSandbox ${KUBE_NAMESPACE}/${SANDBOX_ID}"
  batch_proof="$(printf '%s' "${batch_response}" | "${VENV_PY}" -c '
import json
import sys

sandbox_id = sys.argv[1]
batch = json.load(sys.stdin)
if not isinstance(batch, dict):
    raise SystemExit("BatchSandbox response is not an object")
metadata = batch.get("metadata") or {}
if metadata.get("name") != sandbox_id or metadata.get("deletionTimestamp") is not None:
    raise SystemExit(f"BatchSandbox {sandbox_id!r} identity is missing or deleting")
batch_uid = str(metadata.get("uid") or "").strip()
if not batch_uid:
    raise SystemExit(f"BatchSandbox {sandbox_id!r} has no UID")
print(batch_uid)
' "${SANDBOX_ID}" 2>/dev/null)" \
    || fail "BatchSandbox ${KUBE_NAMESPACE}/${SANDBOX_ID} is not a direct SDK-created sandbox"
  batch_uid="${batch_proof}"
  [[ -n "${batch_uid}" ]] \
    || fail "could not read BatchSandbox identity for ${SANDBOX_ID}"
  pods_response="$(kubectl --kubeconfig "${KUBECONFIG_PATH}" \
    --namespace "${KUBE_NAMESPACE}" get pods -o json 2>/dev/null)" \
    || fail "could not list Pods in namespace ${KUBE_NAMESPACE}"
  proof="$(printf '%s' "${pods_response}" | "${VENV_PY}" -c '
import json
import sys

sandbox_id, batch_uid, expected_image, expected_level = sys.argv[1:]
payload = json.load(sys.stdin)
items = payload.get("items") if isinstance(payload, dict) else None
if not isinstance(items, list):
    raise SystemExit("Pod inventory has no items list")


def belongs_to_batchsandbox(item):
    if not isinstance(item, dict):
        return False
    metadata = item.get("metadata") or {}
    owners = metadata.get("ownerReferences") or []
    return any(
        isinstance(owner, dict)
        and owner.get("kind") == "BatchSandbox"
        and owner.get("name") == sandbox_id
        and owner.get("uid") == batch_uid
        for owner in owners
    )


pods = [item for item in items if belongs_to_batchsandbox(item)]
if len(pods) != 1:
    raise SystemExit(
        f"BatchSandbox {sandbox_id!r} resolves to {len(pods)} Pods; expected exactly one"
    )
pod = pods[0]
metadata = pod.get("metadata") or {}
pod_name = str(metadata.get("name") or "").strip()
if not pod_name or metadata.get("deletionTimestamp") is not None:
    raise SystemExit(f"BatchSandbox {sandbox_id!r} Pod is unnamed or deleting")
if (pod.get("status") or {}).get("phase") != "Running":
    raise SystemExit(f"BatchSandbox {sandbox_id!r} Pod {pod_name!r} is not Running")

containers = (pod.get("spec") or {}).get("containers") or []
sandbox_containers = [
    container
    for container in containers
    if isinstance(container, dict) and container.get("name") == "sandbox"
]
if len(sandbox_containers) != 1:
    raise SystemExit(f"Pod {pod_name!r} does not have exactly one sandbox container")
sandbox_container = sandbox_containers[0]
actual_image = sandbox_container.get("image")
if actual_image != expected_image:
    raise SystemExit(
        f"Pod {pod_name!r} sandbox image {actual_image!r} != {expected_image!r}"
    )

security_context = sandbox_container.get("securityContext") or {}
capabilities = security_context.get("capabilities") or {}
added_capabilities = {
    str(capability).strip().upper()
    for capability in (capabilities.get("add") or [])
    if str(capability).strip()
}
apparmor_type = str(
    (security_context.get("appArmorProfile") or {}).get("type") or ""
).strip()
has_sys_admin = "SYS_ADMIN" in added_capabilities
has_unconfined_apparmor = apparmor_type == "Unconfined"
if expected_level == "advanced":
    if not has_sys_admin or not has_unconfined_apparmor:
        raise SystemExit(
            f"Pod {pod_name!r} does not carry the advanced sandbox grant "
            "(requires SYS_ADMIN and unconfined AppArmor)"
        )
elif expected_level == "default":
    if has_sys_admin or has_unconfined_apparmor:
        raise SystemExit(
            f"Pod {pod_name!r} carries an advanced sandbox grant for a default Environment"
        )
else:
    raise SystemExit(f"unsupported expected sandbox permission level: {expected_level!r}")

statuses = (pod.get("status") or {}).get("containerStatuses") or []
sandbox_statuses = [
    status
    for status in statuses
    if isinstance(status, dict) and status.get("name") == "sandbox"
]
if len(sandbox_statuses) != 1:
    raise SystemExit(f"Pod {pod_name!r} has no exact sandbox container status")
image_id = sandbox_statuses[0].get("imageID")
if not isinstance(image_id, str) or not image_id.strip():
    raise SystemExit(f"Pod {pod_name!r} sandbox status has no imageID")
node_name = str((pod.get("spec") or {}).get("nodeName") or "").strip()
if not node_name:
    raise SystemExit(f"Pod {pod_name!r} has no assigned node")
print(
    pod_name
    + "\t"
    + actual_image
    + "\t"
    + image_id.strip()
    + "\t"
    + node_name
    + "\t"
    + expected_level
)
' "${SANDBOX_ID}" "${batch_uid}" \
      "${EXPECTED_RUNTIME_IMAGE}" "${EXPECTED_SANDBOX_PERMISSION_LEVEL}" 2>/dev/null)" \
    || fail "BatchSandbox ${KUBE_NAMESPACE}/${SANDBOX_ID} did not resolve to one running Pod with the expected image and sandbox permission grant"
  IFS=$'\t' read -r pod_name ACTUAL_RUNTIME_IMAGE pod_image_id node_name \
    ACTUAL_SANDBOX_PERMISSION_LEVEL <<< "${proof}"
  [[ -n "${pod_name}" && "${ACTUAL_RUNTIME_IMAGE}" == "${EXPECTED_RUNTIME_IMAGE}" \
    && -n "${pod_image_id}" && -n "${node_name}" \
    && "${ACTUAL_SANDBOX_PERMISSION_LEVEL}" == "${EXPECTED_SANDBOX_PERMISSION_LEVEL}" ]] \
    || fail "could not record exact runtime Pod identity for ${SANDBOX_ID}"
  node_response="$(kubectl --kubeconfig "${KUBECONFIG_PATH}" \
    get node "${node_name}" -o json 2>/dev/null)" \
    || fail "could not read Kubernetes node ${node_name}"
  platform="$(printf '%s' "${node_response}" | "${VENV_PY}" -c '
import json
import sys

node = json.load(sys.stdin)
metadata = node.get("metadata") or {}
node_info = (node.get("status") or {}).get("nodeInfo") or {}
if metadata.get("name") != sys.argv[1]:
    raise SystemExit("node identity mismatch")
operating_system = str(node_info.get("operatingSystem") or "").strip()
architecture = str(node_info.get("architecture") or "").strip()
if not operating_system or not architecture:
    raise SystemExit("node platform is missing")
print(operating_system + "\t" + architecture)
' "${node_name}" 2>/dev/null)" \
    || fail "Kubernetes node ${node_name} did not report its runtime platform"
  registry_api="http://${EXPECTED_RUNTIME_IMAGE%%/*}"
  runtime_evidence="${LOG_DIR}/runtime-image.json"
  rm -f -- "${runtime_evidence}"
  IFS=$'\t' read -r operating_system architecture <<< "${platform}"
  "${VENV_PY}" "${REPO_ROOT}/scripts/runtime_image_identity.py" \
    --registry-api "${registry_api}" \
    --expected-image "${EXPECTED_RUNTIME_IMAGE}" \
    --expected-digest "${EXPECTED_RUNTIME_IMAGE_DIGEST}" \
    --observed-image-id "${pod_image_id}" \
    --operating-system "${operating_system}" \
    --architecture "${architecture}" --json-out "${runtime_evidence}" \
    || fail "BatchSandbox ${KUBE_NAMESPACE}/${SANDBOX_ID} Pod did not prove the exact runtime image content"
  proof="$("${VENV_PY}" -c '
import json
import sys

value = json.load(open(sys.argv[1], encoding="utf-8"))
if value.get("state") != "PASS":
    raise SystemExit("runtime image evidence is not PASS")
print("\t".join([
    str(value.get("registry_digest") or ""),
    str(value.get("runtime_manifest_digest") or ""),
    str(value.get("observed_image_id_digest") or ""),
]))
' "${runtime_evidence}" 2>/dev/null)" \
    || fail "could not read exact runtime image evidence for ${SANDBOX_ID}"
  IFS=$'\t' read -r ACTUAL_IMAGE_DIGEST ACTUAL_RUNTIME_CONTENT_DIGEST \
    OBSERVED_IMAGE_ID_DIGEST <<< "${proof}"
  [[ "${ACTUAL_IMAGE_DIGEST}" == "${EXPECTED_RUNTIME_IMAGE_DIGEST}" \
    && -n "${ACTUAL_RUNTIME_CONTENT_DIGEST}" \
    && -n "${OBSERVED_IMAGE_ID_DIGEST}" ]] \
    || fail "could not record exact runtime image evidence for ${SANDBOX_ID}"
  log "runtime Pod ${KUBE_NAMESPACE}/${pod_name} uses ${ACTUAL_RUNTIME_IMAGE}; permission=${ACTUAL_SANDBOX_PERMISSION_LEVEL} release=${ACTUAL_IMAGE_DIGEST} runtime=${ACTUAL_RUNTIME_CONTENT_DIGEST} observed=${OBSERVED_IMAGE_ID_DIGEST}"
}

# ── evidence capture (no fake success: dump everything on failure) ───────────
capture_evidence() {
  log "──────── EVIDENCE ────────"
  if [[ -n "${SID}" ]]; then
    log "session detail:"
    api_curl -fsS "${BASE_URL}/api/v1/sessions/${SID}" 2>/dev/null \
      | "${VENV_PY}" -m json.tool 2>/dev/null | redact_stream >&2 || true
    log "docker ps -a for session ${SID}:"
    docker ps -a --filter "label=${SESSION_LABEL}=${SID}" \
      --format '{{.ID}}  {{.Image}}  {{.Status}}  {{.Names}}' >&2 2>&1 || true
    local cid
    cid="$(docker ps -a --filter "label=${SESSION_LABEL}=${SID}" -q 2>/dev/null | head -1)"
    if [[ -n "${cid}" ]]; then
      log "in-container claude state (container ${cid}):"
      docker exec "${cid}" sh -lc 'command -v claude && claude --version' >&2 2>&1 || \
        log "  (claude not resolvable in container)"
      log "container egress probe to \$ANTHROPIC_BASE_URL:"
      docker exec "${cid}" sh -lc \
        'curl -sS -o /dev/null -w "%{http_code}\n" --max-time 10 "$ANTHROPIC_BASE_URL"' \
        >&2 2>&1 || log "  (egress probe failed)"
      log "docker logs (tail) for ${cid}:"
      docker logs --tail 50 "${cid}" 2>&1 | redact_stream >&2 || true
    fi
  fi
  if [[ -f "${SSE_OUT}" ]]; then
    log "SSE capture (tail) ${SSE_OUT}:"
    tail -40 "${SSE_OUT}" 2>&1 | redact_stream >&2 || true
  fi
  log "server log (tail) ${SERVER_LOG} (credentials redacted):"
  tail -60 "${SERVER_LOG}" 2>/dev/null | redact_stream >&2 || true
}

# ── cleanup of the script-owned server and sandboxes ────────────────────────
cleanup() {
  local rc=$?
  if [[ "${rc}" != 0 && -n "${EXTERNAL_BASE_URL}" ]]; then
    log "KEPT for diagnosis: agent=${AGENT_ID} session=${SID} sandbox=${SANDBOX_ID}"
    return "${rc}"
  fi
  if [[ -n "${BASE_URL:-}" && -n "${SID}" ]]; then
    # A timed-out curl can leave a turn active. Interrupt first so the normal
    # lifecycle deletion owns sandbox + egress cleanup instead of the test
    # harness deleting a workload container behind the provider's back.
    api_curl -fsS -X POST "${BASE_URL}/api/v1/sessions/${SID}/interrupt" \
      >/dev/null 2>&1 || true
    for _ in $(seq 1 40); do
      local cleanup_state
      cleanup_state="$(api_curl -fsS "${BASE_URL}/api/v1/sessions/${SID}" 2>/dev/null \
        | "${VENV_PY}" -c \
          'import json,sys
try: print(str((json.load(sys.stdin).get("data") or {}).get("state") or ""))
except Exception: print("")' 2>/dev/null || true)"
      case "${cleanup_state}" in
        READY|TERMINATED|RECOVERY_REQUIRED|DELETED|'') break ;;
      esac
      sleep 0.25
    done
    api_curl -fsS -X DELETE "${BASE_URL}/api/v1/sessions/${SID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${BASE_URL:-}" && -n "${AGENT_ID}" && "${AGENT_OWNED}" == 1 ]]; then
    api_curl -fsS -X DELETE "${BASE_URL}/api/v1/agents/${AGENT_ID}" >/dev/null 2>&1 || true
  fi
  if [[ -z "${EXTERNAL_BASE_URL}" && -n "${SID}" ]]; then
    local leftovers=""
    for _ in $(seq 1 40); do
      leftovers="$(docker ps -a --filter "label=${SESSION_LABEL}=${SID}" -q 2>/dev/null)"
      if [[ -n "${SANDBOX_ID}" ]]; then
        for container_name in "sandbox-${SANDBOX_ID}" "sandbox-egress-${SANDBOX_ID}"; do
          local exact_id
          exact_id="$(docker container inspect --format '{{.Id}}' "${container_name}" 2>/dev/null || true)"
          if [[ -n "${exact_id}" && "${leftovers}" != *"${exact_id}"* ]]; then
            leftovers="${leftovers}"$'\n'"${exact_id}"
          fi
        done
      fi
      [[ -z "${leftovers//$'\n'/}" ]] && break
      sleep 0.25
    done
    if [[ -n "${leftovers//$'\n'/}" ]]; then
      log "cleanup: AstraBox left sandbox container(s) behind for ${SID}"
      printf '%s\n' "${leftovers}" >&2
      rc=1
    fi
  fi
  if [[ -n "${SERVER_PID}" ]]; then
    log "cleanup: stopping the test-owned Compose deployment"
    kill "${SERVER_PID}" >/dev/null 2>&1 || true
    for _ in $(seq 1 60); do kill -0 "${SERVER_PID}" 2>/dev/null || break; sleep 0.5; done
    kill -9 "${SERVER_PID}" >/dev/null 2>&1 || true
    wait "${SERVER_PID}" >/dev/null 2>&1 || true
  fi
  return "${rc}"
}
trap cleanup EXIT

# ── 0) preflight ─────────────────────────────────────────────────────────────
[[ -x "${VENV_PY}" ]] || fail "venv python not found at ${VENV_PY}"
if [[ -n "${AUTH_TOKEN_FILE}" ]]; then
  [[ "${AUTH_TOKEN_FILE}" == /* && -f "${AUTH_TOKEN_FILE}" && ! -L "${AUTH_TOKEN_FILE}" \
    && -r "${AUTH_TOKEN_FILE}" ]] \
    || fail "auth token must be a readable absolute regular file"
  AUTH_TOKEN="$(tr -d '\r\n' <"${AUTH_TOKEN_FILE}")"
  [[ -n "${AUTH_TOKEN}" && "${AUTH_TOKEN}" != *['"'\\[:space:]]* ]] \
    || fail "auth token file contains an empty or unsafe bearer value"
fi
if [[ -n "${EXACT_AGENT_ID}" ]]; then
  [[ -n "${EXPECTED_ENGINE_KIND}" && -n "${EXPECTED_RUNTIME_IMAGE}" \
    && -n "${EXPECTED_RUNTIME_IMAGE_DIGEST}" \
    && -n "${EXPECTED_SANDBOX_PERMISSION_LEVEL}" ]] \
    || fail "ASTRABOX_E2E_AGENT_ID requires expected engine kind, runtime image, image digest, and sandbox permission level"
  [[ "${EXPECTED_RUNTIME_IMAGE_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail "expected runtime image digest must be sha256:<64 lowercase hex>"
  [[ "${EXPECTED_SANDBOX_PERMISSION_LEVEL}" == default \
    || "${EXPECTED_SANDBOX_PERMISSION_LEVEL}" == advanced ]] \
    || fail "exact-Agent smoke supports sandbox permission level 'default' or 'advanced'"
  [[ "${KUBECONFIG_PATH}" == /* && -f "${KUBECONFIG_PATH}" \
    && -r "${KUBECONFIG_PATH}" ]] \
    || fail "exact-Agent smoke requires a readable absolute KUBECONFIG"
  [[ -n "${KUBE_NAMESPACE}" ]] \
    || fail "exact-Agent smoke requires ASTRABOX_E2E_KUBE_NAMESPACE"
  command -v kubectl >/dev/null 2>&1 || fail "kubectl CLI not found"
elif [[ -n "${EXPECTED_ENGINE_KIND}" || -n "${EXPECTED_RUNTIME_IMAGE}" \
  || -n "${EXPECTED_RUNTIME_IMAGE_DIGEST}" \
  || -n "${EXPECTED_SANDBOX_PERMISSION_LEVEL}" ]]; then
  fail "expected engine kind/runtime image/digest/sandbox permission level belong only to exact-Agent smoke mode"
fi
if [[ -z "${EXTERNAL_BASE_URL}" ]]; then
  command -v docker >/dev/null 2>&1 || fail "docker CLI not found"
  docker version --format '{{.Server.Version}}' >/dev/null 2>&1 \
    || fail "docker daemon not reachable"
  [[ -f "${ENV_FILE}" ]] || fail "env file not found at ${ENV_FILE} (LLM creds)"
fi

# ── 1) source the operator LLM creds (.env — NEVER printed) ──────────────────
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
fi

# The self-boot path uses the same defaults and generated database secrets as
# the maintained Compose deployment. Only the sandbox image is selected here.
export ASTRABOX_AGENT_IMAGE="${ASTRABOX_AGENT_IMAGE:-astrabox/sandbox-claude-code:latest}"

[[ -n "${ANTHROPIC_BASE_URL:-}" ]] || log "warning: ANTHROPIC_BASE_URL is empty (CLI will use its default)"
if [[ -z "${EXTERNAL_BASE_URL}" && -z "${ANTHROPIC_AUTH_TOKEN:-}" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
  fail "neither ANTHROPIC_AUTH_TOKEN nor ANTHROPIC_API_KEY is set in ${ENV_FILE}"
fi
if [[ -z "${EXTERNAL_BASE_URL}" ]]; then
  docker image inspect "${ASTRABOX_AGENT_IMAGE}" >/dev/null 2>&1 \
    || fail "agent image ${ASTRABOX_AGENT_IMAGE} missing — build it with make build-agent-image"
fi

# ── 2) resolve the server base URL (boot one unless reusing an external) ─────
if [[ -n "${EXTERNAL_BASE_URL}" ]]; then
  BASE_URL="${EXTERNAL_BASE_URL%/}"
  log "reusing external server at ${BASE_URL} (not booting one)"
else
  BASE_URL="http://127.0.0.1:${PORT}"
  log "booting test-owned Compose deployment on ${BASE_URL} (log -> ${SERVER_LOG})"
  : > "${SERVER_LOG}"
  (
    cd "${REPO_ROOT}" || exit 1
    export ASTRABOX_BACKEND_PORT="${PORT}"
    export ASTRABOX_E2E_ENV_FILE="${ENV_FILE}"
    exec bash e2e/scripts/serve-backend.sh
  ) >> "${SERVER_LOG}" 2>&1 &
  SERVER_PID=$!
  log "deployment supervisor pid=${SERVER_PID}"
fi

# ── 3) wait for /healthz ─────────────────────────────────────────────────────
health_ok=""
for _ in $(seq 1 "${HEALTH_TIMEOUT}"); do
  if curl -fsS "${BASE_URL}/healthz" >/dev/null 2>&1; then health_ok=1; break; fi
  if [[ -n "${SERVER_PID}" ]] && ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    fail "server process exited before /healthz came up"
  fi
  sleep 1
done
[[ -n "${health_ok}" ]] || fail "server /healthz not ready within ${HEALTH_TIMEOUT}s"
log "health OK"

# ── 4) Agent + conversation. The release probe names an exact existing Agent so
# its engine options are part of what starts. The standalone smoke keeps its
# original isolated path: select a seed Environment/model, clone a disposable
# Agent, and remove it during cleanup.
SEEDED_AGENT_NAME="${ASTRABOX_E2E_AGENT_NAME:-Claude Code}"
AGENTS_RESP="$(api_curl -fsS "${BASE_URL}/api/v1/agents" 2>/dev/null)" \
  || fail "GET /api/v1/agents failed (curl error)"
SEEDED_SELECTION="$(printf '%s' "${AGENTS_RESP}" | "${VENV_PY}" -c '
import json, sys

target, exact_id = sys.argv[1:]
payload = json.load(sys.stdin)
agents = payload.get("data", payload) if isinstance(payload, dict) else payload
if not isinstance(agents, list):
    raise SystemExit("agents response is not a list")
matches = [
    item
    for item in agents
    if str(item.get("name") or "") == target
    and (not exact_id or str(item.get("agent_id") or "") == exact_id)
]
if len(matches) != 1:
    raise SystemExit(
        f"expected one Agent named {target!r}"
        + (f" with id {exact_id!r}" if exact_id else "")
        + f"; found {len(matches)}"
    )
agent = matches[0]
if not isinstance(agent, dict):
    raise SystemExit(f"seeded Agent {target!r} was not found")
agent_id = str(agent.get("agent_id") or "").strip()
environment = str(agent.get("environment_name") or "").strip()
model = str(agent.get("model") or "").strip()
if not agent_id or not environment or not model:
    raise SystemExit(f"Agent {target!r} has no id, Environment, or model")
print(agent_id + "\t" + environment + "\t" + model)
' "${SEEDED_AGENT_NAME}" "${EXACT_AGENT_ID}" 2>/dev/null)" \
  || fail "cannot read a usable seeded Agent named '${SEEDED_AGENT_NAME}'"
IFS=$'\t' read -r SELECTED_AGENT_ID SEEDED_ENVIRONMENT SEEDED_MODEL <<< "${SEEDED_SELECTION}"
SMOKE_ENVIRONMENT="${ASTRABOX_E2E_ENVIRONMENT:-${SEEDED_ENVIRONMENT}}"
[[ -n "${SMOKE_ENVIRONMENT}" ]] || fail "the smoke Agent needs an Environment"
if [[ -n "${EXACT_AGENT_ID}" && "${SMOKE_ENVIRONMENT}" != "${SEEDED_ENVIRONMENT}" ]]; then
  fail "exact Agent Environment drifted: selected=${SEEDED_ENVIRONMENT} requested=${SMOKE_ENVIRONMENT}"
fi

ENCODED_ENVIRONMENT="$("${VENV_PY}" -c \
  'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' \
  "${SMOKE_ENVIRONMENT}")" || fail "could not encode the Environment name"
MODELS_RESP="$(api_curl -fsS \
  "${BASE_URL}/api/v1/admin/environments/${ENCODED_ENVIRONMENT}/models" 2>/dev/null)" \
  || fail "could not list models for Environment '${SMOKE_ENVIRONMENT}'"
PREFERRED_MODEL="${ASTRABOX_E2E_MODEL:-${SEEDED_MODEL:-${ANTHROPIC_MODEL:-${ASTRABOX_LLM_MODEL:-}}}}"
SMOKE_MODEL="$(printf '%s' "${MODELS_RESP}" | "${VENV_PY}" -c '
import json, sys
from urllib.parse import urlsplit

preferred, base_url, explicit = sys.argv[1:4]
payload = json.load(sys.stdin)
data = payload.get("data", payload) if isinstance(payload, dict) else {}
routes = data.get("models", []) if isinstance(data, dict) else []
routes = [str(item).strip() for item in routes if str(item).strip()]

def supports(candidate):
    return any(
        candidate.startswith(route[:-1]) if route.endswith("*") else candidate == route
        for route in routes
    )

if explicit and not supports(explicit):
    raise SystemExit(f"requested model {explicit!r} is not offered by this Environment")
if preferred and supports(preferred):
    print(preferred)
    raise SystemExit(0)

hostname = (urlsplit(base_url).hostname or "").lower()
prefix = "deepseek-" if hostname == "api.deepseek.com" else ""
exact = [route for route in routes if not route.endswith("*")]
if prefix:
    exact = [route for route in exact if route.startswith(prefix)] + [
        route for route in exact if not route.startswith(prefix)
    ]
if not exact:
    raise SystemExit("the Environment returned no concrete model id")
print(exact[0])
' "${PREFERRED_MODEL}" "${ANTHROPIC_BASE_URL:-}" "${ASTRABOX_E2E_MODEL:-}" 2>/dev/null)" \
  || fail "cannot choose a usable model for Environment '${SMOKE_ENVIRONMENT}'"
[[ -n "${SMOKE_MODEL}" ]] || fail "the Environment returned no usable model"
if [[ -n "${EXACT_AGENT_ID}" && "${SMOKE_MODEL}" != "${SEEDED_MODEL}" ]]; then
  fail "exact Agent model drifted: selected=${SEEDED_MODEL} resolved=${SMOKE_MODEL}"
fi
log "using Environment '${SMOKE_ENVIRONMENT}' and model '${SMOKE_MODEL}'"

if [[ -n "${EXACT_AGENT_ID}" ]]; then
  AGENT_ID="${SELECTED_AGENT_ID}"
  log "using exact existing Agent: ${AGENT_ID}"
else
  AGENT_BODY="$("${VENV_PY}" -c '
import json, sys
print(json.dumps({"name": "e2e-smoke", "model": sys.argv[1], "environment_name": sys.argv[2]}))
' "${SMOKE_MODEL}" "${SMOKE_ENVIRONMENT}")" \
    || fail "could not encode the Agent request"

  AGENT_RESP="$(api_curl -fsS -X POST "${BASE_URL}/api/v1/agents" \
    -H 'Content-Type: application/json' \
    -d "${AGENT_BODY}" 2>/dev/null)" \
    || fail "POST /api/v1/agents failed (curl error)"
  AGENT_ID="$(printf '%s' "${AGENT_RESP}" | "${VENV_PY}" -c \
    'import sys,json; d=json.load(sys.stdin); print((d.get("data") or {}).get("agent_id") or "")' \
    2>/dev/null)"
  [[ -n "${AGENT_ID}" ]] || fail "no agent_id in create response: ${AGENT_RESP}"
  AGENT_OWNED=1
  log "disposable Agent created: ${AGENT_ID} (model=${SMOKE_MODEL})"
fi

CREATE_RESP="$(api_curl -fsS -X POST "${BASE_URL}/api/v1/agents/${AGENT_ID}/conversations" \
  -H 'Content-Type: application/json' -d '{}' 2>/dev/null)" \
  || fail "POST /api/v1/agents/${AGENT_ID}/conversations failed (curl error)"

SID="$(printf '%s' "${CREATE_RESP}" | "${VENV_PY}" -c \
  'import sys,json; d=json.load(sys.stdin); print((d.get("data") or {}).get("session_id") or "")' \
  2>/dev/null)"
[[ -n "${SID}" ]] || fail "no session_id in create response: ${CREATE_RESP}"
log "conversation created: ${SID}"

# ── 5) poll the session to READY (or fail on a terminal state) ───────────────
state=""
for _ in $(seq 1 "${READY_TIMEOUT}"); do
  DET="$(api_curl -fsS "${BASE_URL}/api/v1/sessions/${SID}" 2>/dev/null)" || true
  state="$(printf '%s' "${DET}" | "${VENV_PY}" -c \
    'import sys,json
try: d=json.load(sys.stdin)
except Exception: print(""); raise SystemExit
print((d.get("data") or {}).get("state") or "")' 2>/dev/null)"
  case "${state}" in
    READY) break ;;
    TERMINATED|RECOVERY_REQUIRED|DELETED)
      err="$(printf '%s' "${DET}" | "${VENV_PY}" -c \
        'import sys,json; d=json.load(sys.stdin); print((d.get("data") or {}).get("last_error") or "")' 2>/dev/null)"
      fail "session reached terminal state ${state} before READY (last_error: ${err})" ;;
  esac
  sleep 1
done
[[ "${state}" == "READY" ]] || fail "session did not reach READY within ${READY_TIMEOUT}s (last state: ${state})"
SESSION_PROOF="$(printf '%s' "${DET}" | "${VENV_PY}" -c '
import json, sys

payload = json.load(sys.stdin)
data = payload.get("data") or {}
capabilities = data.get("engine_capabilities") or {}
print("\t".join([
    str(data.get("sandbox_id") or ""),
    str(data.get("engine_kind") or ""),
    "true" if data.get("runtime_unavailable") is True else "false",
    str(capabilities.get("engine_kind") or ""),
]))
' 2>/dev/null)" || fail "could not read the READY session proof"
IFS=$'\t' read -r SANDBOX_ID ACTUAL_ENGINE_KIND RUNTIME_UNAVAILABLE CAPABILITY_ENGINE_KIND \
  <<< "${SESSION_PROOF}"
[[ -n "${SANDBOX_ID}" ]] || fail "READY session ${SID} did not expose a sandbox_id"
[[ "${RUNTIME_UNAVAILABLE}" == false ]] \
  || fail "READY session ${SID} reports runtime_unavailable"
[[ -n "${ACTUAL_ENGINE_KIND}" && "${CAPABILITY_ENGINE_KIND}" == "${ACTUAL_ENGINE_KIND}" ]] \
  || fail "READY session engine capability identity is missing or mismatched"
if [[ -n "${EXPECTED_ENGINE_KIND}" && "${ACTUAL_ENGINE_KIND}" != "${EXPECTED_ENGINE_KIND}" ]]; then
  fail "exact Agent started engine ${ACTUAL_ENGINE_KIND}; expected ${EXPECTED_ENGINE_KIND}"
fi
if [[ -n "${EXPECTED_RUNTIME_IMAGE}" ]]; then
  SANDBOX_RESP="$(api_curl -fsS \
    "${BASE_URL}/api/v1/admin/sandboxes/${SANDBOX_ID}?backend=open_sandbox" 2>/dev/null)" \
    || fail "could not read OpenSandbox descriptor for ${SANDBOX_ID}"
  printf '%s' "${SANDBOX_RESP}" | "${VENV_PY}" -c '
import json, sys

expected_sandbox = sys.argv[1]
payload = json.load(sys.stdin)
data = payload.get("data") or {}
if str(data.get("sandbox_id") or "") != expected_sandbox:
    raise SystemExit("sandbox descriptor identity mismatch")
# Descriptor session_id is the immutable create-time owner. A prepared
# Agent box is created before this conversation and keeps its slot identity;
# READY Session sandbox_id above is the platform claim proof.
if data.get("backend") != "open_sandbox":
    raise SystemExit("sandbox descriptor backend mismatch")
if data.get("state") != "Running":
    raise SystemExit("sandbox descriptor is not Running")
' "${SANDBOX_ID}" 2>/dev/null \
    || fail "OpenSandbox descriptor did not match the bound sandbox"
fi
log "session READY"

# Use the same file API round trip as the live Python Files suite. The returned
# path is authoritative for every tenancy; the probe never guesses a HOME or
# rewrites permissions inside the box.
WORKSPACE_MARKER="astrabox-smoke:${SID}"
WORKSPACE_UPLOAD="$(printf '%s' "${WORKSPACE_MARKER}" | api_curl -fsS --max-time 30 \
  -X POST "${BASE_URL}/api/v1/sessions/${SID}/files/upload" \
  --form-string 'path=' --form 'files=@-;filename=astrabox-smoke.txt;type=text/plain' 2>/dev/null)" \
  || fail "the real Session workspace rejected the smoke file upload"
WORKSPACE_PATH="$(printf '%s' "${WORKSPACE_UPLOAD}" | "${VENV_PY}" -c '
import json, sys

data = json.load(sys.stdin).get("data") or {}
entries = data.get("entries") or []
if data.get("uploaded_count") != 1 or len(entries) != 1:
    raise SystemExit("workspace upload did not return exactly one file")
entry = entries[0]
path = str(entry.get("path") or "")
if entry.get("name") != "astrabox-smoke.txt" or not path.startswith("/"):
    raise SystemExit("workspace upload returned an invalid file identity")
print(path)
' 2>/dev/null)" || fail "the real Session workspace returned an invalid upload receipt"
api_curl -fsS --max-time 30 --get \
  "${BASE_URL}/api/v1/sessions/${SID}/files/download" \
  --data-urlencode "path=${WORKSPACE_PATH}" 2>/dev/null \
  | "${VENV_PY}" -c '
import sys

if sys.stdin.buffer.read() != sys.argv[1].encode("utf-8"):
    raise SystemExit("workspace download differs from uploaded bytes")
' "${WORKSPACE_MARKER}" \
  || fail "the real Session workspace did not return the exact uploaded content"
log "real Session workspace upload and read-back passed"

# A running agent container must now exist for the session — a check this
# script can only make when IT booted the server (the local docker runtime,
# where a sandbox is a container on this daemon). Against an external server
# the runtime shape is unknown (a kubernetes deployment's sandbox is a Pod on
# the cluster, invisible to this host's docker), so box liveness is proven by
# the turn itself: the text-delta assertion below cannot pass without a live
# box behind it.
if [[ -z "${EXTERNAL_BASE_URL}" ]]; then
  docker ps --filter "label=${SESSION_LABEL}=${SID}" -q 2>/dev/null | grep -q . \
    || fail "no RUNNING agent container for session ${SID} at READY"
  log "agent container is up"
fi

# ── 6) POST the prompt and stream the AI-SDK SSE ─────────────────────────────
log "POST ai-stream prompt: ${PROMPT}"
: > "${SSE_OUT}"
: > "${HEADERS_OUT}"
CLIENT_MESSAGE_ID="$("${VENV_PY}" -c 'import uuid; print(uuid.uuid4())')"
# -N: unbuffered so frames arrive live; --max-time bounds a hang.
api_curl -N --max-time "${STREAM_TIMEOUT}" \
  -D "${HEADERS_OUT}" \
  -X POST "${BASE_URL}/api/v1/sessions/${SID}/ai-stream" \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d "$(printf '%s' "${PROMPT}" | "${VENV_PY}" -c \
        'import sys,json; print(json.dumps({"content": sys.stdin.read(), "client_message_id": sys.argv[1]}))' \
        "${CLIENT_MESSAGE_ID}")" \
  > "${SSE_OUT}" 2>/dev/null \
  || true   # The stream-content checks below decide success; curl may time out after receiving data.

# A JSON error envelope (not SSE) means a pre-stream failure.
if head -c 1 "${SSE_OUT}" 2>/dev/null | grep -q '{'; then
  fail "ai-stream returned a JSON error envelope (not SSE): $(head -c 400 "${SSE_OUT}")"
fi
grep -Eiq '^x-vercel-ai-ui-message-stream:[[:space:]]*v1[[:space:]]*$' "${HEADERS_OUT}" \
  || fail "ai-stream omitted x-vercel-ai-ui-message-stream: v1"

# ── 7) judge the stream: one complete response is the proof ─────────────────
# Require one text-start/text-end pair containing genuine text deltas, followed
# by the AI SDK stream's normal finish. This proves the response crossed the
# engine transport and public stream exactly once without treating model prose
# as a deterministic test signal.
# A FAILED turn hard-fails FIRST — whether it arrives as an `error` frame, an
# `is_error` result, or the CLI's "API Error: ..." result/text (the real 402
# vector: "402" even contains a "2") — so an error surfaced as text can never green.
PROOF="$("${VENV_PY}" - "${SSE_OUT}" <<'PY'
import json, os, re, sys
path = sys.argv[1]
deltas = []
n_delta = 0
saw_error = None
open_text_blocks = set()
completed_text_blocks = set()
finish_reason = ""
finish_count = 0
protocol_error = None


def _is_model_error(s):
    # The claude CLI surfaces a model/gateway HTTP error (402/401/429/5xx) as an
    # error result — a FAILED turn, not an answer. Error text often contains a
    # status code or token hash with "2", so it must never count as genuine text.
    if not isinstance(s, str):
        return False
    folded = " ".join(s.lower().split())
    return any(
        marker in folded
        for marker in (
            "api error",
            "authentication error",
            "invalid api key",
            "invalid proxy server token",
            "received api key",
            "unable to find token",
            "status code: 401",
            "status_code=401",
            "error code: 401",
        )
    )


def _redact(s):
    value = str(s or "")
    name_pattern = re.compile(
        r"(?:^|_)(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|SECRET|PASSWORD|MASTER_KEY|DATABASE_URL)(?:$|_)",
        re.I,
    )
    secret_values = sorted(
        {
            env_value
            for name, env_value in os.environ.items()
            if len(env_value) >= 8 and name_pattern.search(name)
        },
        key=len,
        reverse=True,
    )
    for secret in secret_values:
        value = value.replace(secret, "[redacted]")
    value = re.sub(
        r"(?i)(\b(?:api[ _-]?key|auth[ _-]?token|access[ _-]?token|master[ _-]?key|password)\b[\"']?\s*(?:=|:)\s*)([\"']?)([^\s,\"'}]+)",
        lambda match: match.group(1) + match.group(2) + "[redacted]",
        value,
    )
    value = re.sub(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[redacted]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9._~+/=-]{8,}\b", "[redacted]", value)
    return re.sub(r"\b[0-9a-fA-F]{48,}\b", "[redacted]", value)


with open(path, "r", errors="replace") as fh:
    for line in fh:
        line = line.rstrip("\n")
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if t == "text-start":
            block_id = ev.get("id")
            if not isinstance(block_id, str) or not block_id:
                protocol_error = protocol_error or "text-start has no id"
            elif block_id in open_text_blocks or block_id in completed_text_blocks:
                protocol_error = protocol_error or f"duplicate text-start id {block_id!r}"
            else:
                open_text_blocks.add(block_id)
        elif t == "text-delta":
            # Count only GENUINE assistant text. A model failure (e.g. a 402) is
            # surfaced by the CLI as an "API Error: ..." text block — exclude it from
            # the proof AND flag the turn, so error text can never satisfy it.
            block_id = ev.get("id")
            if block_id not in open_text_blocks:
                protocol_error = protocol_error or f"text-delta for unopened id {block_id!r}"
            d = ev.get("delta")
            if isinstance(d, str):
                if _is_model_error(d):
                    saw_error = saw_error or d.strip()
                else:
                    n_delta += 1
                    deltas.append(d)
        elif t == "text-end":
            block_id = ev.get("id")
            if block_id not in open_text_blocks:
                protocol_error = protocol_error or f"text-end for unopened id {block_id!r}"
            else:
                open_text_blocks.remove(block_id)
                completed_text_blocks.add(block_id)
        elif t == "error":
            saw_error = ev.get("errorText") or "unknown error"
        elif t == "data-result":
            # The turn's terminal verdict. A failure shows as is_error (when the
            # backend sets it) and/or an "API Error: ..." result string. The
            # The pass-through drops is_error, so the result text is the
            # reliable signal — treat either as a FAILED turn (never green).
            data = ev.get("data")
            if isinstance(data, dict):
                result_text = str(data.get("result") or "")
                if data.get("is_error") or _is_model_error(result_text):
                    saw_error = saw_error or (result_text.strip() or "is_error result")
        elif t == "finish":
            finish_count += 1
            candidate = ev.get("finishReason")
            if isinstance(candidate, str):
                finish_reason = candidate
            if finish_count > 1:
                protocol_error = protocol_error or "multiple finish frames"
text = "".join(deltas)
# Backstop: an API-error split across multiple text-deltas won't match per-delta
# above, but the assembled genuine text would then still carry the signature.
if not saw_error and _is_model_error(text):
    saw_error = text.strip()
if open_text_blocks:
    protocol_error = protocol_error or "text block did not end"
# Emit a machine-judgable summary line. ERROR is flattened to one line + bounded
# so a multi-line result text can't corrupt the single-line sed extraction below.
err_detail = _redact(" ".join((saw_error or "").split())[:300])
print("N_TEXT_DELTA=%d" % n_delta)
print("TEXT_BLOCK_COUNT=%d" % len(completed_text_blocks))
print("FINISH_REASON=%s" % _redact(finish_reason.replace("\n", " ")))
print("PROTOCOL_ERROR=%s" % _redact(" ".join((protocol_error or "").split())[:300]))
print("ERROR=%s" % err_detail)
# A bounded preview of the assembled assistant text (last 200 chars).
print("TEXT_PREVIEW=%s" % _redact(text[-200:].replace("\n", " ")))
PY
)"
log "stream summary: $(printf '%s' "${PROOF}" | redact_stream | tr '\n' ' ')"

N_TEXT_DELTA="$(printf '%s' "${PROOF}" | sed -n 's/^N_TEXT_DELTA=//p')"
TEXT_BLOCK_COUNT="$(printf '%s' "${PROOF}" | sed -n 's/^TEXT_BLOCK_COUNT=//p')"
FINISH_REASON="$(printf '%s' "${PROOF}" | sed -n 's/^FINISH_REASON=//p')"
PROTOCOL_ERR="$(printf '%s' "${PROOF}" | sed -n 's/^PROTOCOL_ERROR=//p')"
STREAM_ERR="$(printf '%s' "${PROOF}" | sed -n 's/^ERROR=//p')"

if [[ -n "${STREAM_ERR}" ]]; then
  fail "ai-stream reported a FAILED turn (error frame or is_error result): ${STREAM_ERR}"
fi
if [[ -n "${PROTOCOL_ERR}" ]]; then
  fail "ai-stream violated the text-block protocol: ${PROTOCOL_ERR}"
fi
# A stream that opens and finishes with zero text deltas is not a valid answer.
if [[ "${N_TEXT_DELTA:-0}" -eq 0 ]]; then
  fail "(empty reply, exit=0): the turn produced ZERO text-delta frames"
fi
# At least one complete block. Not a count, and not a comparison of what the
# blocks say: this file's own rule is that model wording is not the oracle, and
# a reasoning model may open text, reason, and open text again — twice with the
# same short token if that is what it produces. A block delivered twice is
# caught structurally above, where a text-start for an id already open or
# already completed is a protocol error.
if [[ "${TEXT_BLOCK_COUNT:-0}" -lt 1 ]]; then
  fail "expected at least one complete assistant text block, got ${TEXT_BLOCK_COUNT:-0}"
fi
if [[ "${FINISH_REASON}" != "stop" ]]; then
  fail "ai-stream did not finish normally (finishReason=${FINISH_REASON:-missing})"
fi

settled_state=""
for _ in $(seq 1 $(( SETTLE_TIMEOUT * 2 ))); do
  settled_state="$(api_curl -fsS "${BASE_URL}/api/v1/sessions/${SID}" 2>/dev/null \
    | "${VENV_PY}" -c \
      'import json,sys; print(str((json.load(sys.stdin).get("data") or {}).get("state") or ""))' \
      2>/dev/null || true)"
  case "${settled_state}" in
    READY) break ;;
    TERMINATED|RECOVERY_REQUIRED|DELETED)
      fail "session reached ${settled_state} after its successful text response" ;;
  esac
  sleep 0.5
done
[[ "${settled_state}" == READY ]] \
  || fail "session did not return to READY within ${SETTLE_TIMEOUT}s (last state: ${settled_state})"

if [[ -n "${EXACT_AGENT_ID}" ]]; then
  # The session cleanup deletes its BatchSandbox. Read the live Pod only after
  # the real turn settles, while the exact workload that answered still exists.
  prove_kubernetes_runtime_image
fi

if ! "${VENV_PY}" - "${SMOKE_RESULT_OUT}" "${SMOKE_MODEL}" "${SMOKE_ENVIRONMENT}" \
  "${N_TEXT_DELTA}" "${TEXT_BLOCK_COUNT}" "${FINISH_REASON}" \
  "${AGENT_ID}" "${ACTUAL_ENGINE_KIND}" "${ACTUAL_RUNTIME_IMAGE}" \
  "${ACTUAL_IMAGE_DIGEST}" "${ACTUAL_RUNTIME_CONTENT_DIGEST}" \
  "${OBSERVED_IMAGE_ID_DIGEST}" "${SANDBOX_ID}" \
  "${ACTUAL_SANDBOX_PERMISSION_LEVEL}" <<'PY'
import json
import sys
from pathlib import Path

(
    path,
    model,
    environment,
    text_delta_count,
    completed_text_block_count,
    finish_reason,
    agent_id,
    engine_kind,
    image,
    image_digest,
    runtime_image_digest,
    observed_image_id_digest,
    sandbox_id,
    sandbox_permission_level,
) = sys.argv[1:]
Path(path).write_text(
    json.dumps(
        {
            "agent_id": agent_id,
            "completed_text_block_count": int(completed_text_block_count),
            "engine_kind": engine_kind,
            "environment": environment,
            "finish_reason": finish_reason,
            "image": image,
            "image_digest": image_digest,
            "model": model,
            "observed_image_id_digest": observed_image_id_digest,
            "runtime_image_digest": runtime_image_digest,
            "sandbox_id": sandbox_id,
            "sandbox_permission_level": sandbox_permission_level,
            "session_state": "READY",
            "state": "PASS",
            "text_delta_count": int(text_delta_count),
            "ui_message_stream": True,
            "workspace_round_trip": True,
        },
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
then
  fail "could not record the successful smoke result"
fi

log "GREEN: one complete assistant text block, ${N_TEXT_DELTA} text-delta frame(s), finishReason=${FINISH_REASON}"
echo "E2E_SMOKE_PASS session=${SID} text_blocks=${TEXT_BLOCK_COUNT} text_deltas=${N_TEXT_DELTA} finish_reason=${FINISH_REASON}"
exit 0
