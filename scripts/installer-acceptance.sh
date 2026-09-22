#!/usr/bin/env bash
# Prove that a stack scripts/install.sh started serves what a first-time user
# opens, the way that user reaches it.
#
#   scripts/installer-acceptance.sh BASE_URL VERSION [--agent-image IMAGE] [--turn]
#
# Checks, in order: the console page is served; /healthz reports VERSION; the
# API lists the Agents a fresh installation seeds; a Session for the seeded
# Claude Code Agent reaches READY, which takes a real sandbox. With
# --agent-image, a running container on this Docker host must use IMAGE, the
# Agent image the installation was expected to resolve. With --turn it also
# sends one message and requires one complete, normally finished reply and the
# Session's return to READY, so it needs a configured model service. The reply
# is judged by the stream's structure, not by its wording.
#
# Used by .github/workflows/installer.yml and by maintainers' verification of
# the installer on a Docker host; the deployment itself never runs it. Needs
# curl and python3.
set -euo pipefail

usage() {
  printf 'usage: %s BASE_URL VERSION [--agent-image IMAGE] [--turn]\n' "$0" >&2
  exit 64
}

[ "$#" -ge 2 ] || usage
readonly base_url="${1%/}"
readonly version="$2"
shift 2
agent_image=""
with_turn=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --agent-image) [ "$#" -ge 2 ] || usage; agent_image="$2"; shift 2 ;;
    --turn) with_turn=1; shift ;;
    *) usage ;;
  esac
done
readonly ready_timeout_seconds=900
readonly settle_timeout_seconds=180

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$*"; }

json_field() {
  python3 -c 'import json, sys
value = json.load(sys.stdin)
for part in sys.argv[1].split("."):
    value = value[int(part)] if isinstance(value, list) else value[part]
print(value)' "$1"
}

session_state() {
  curl -fsS "$base_url/api/v1/sessions/$1" | json_field data.state
}

status="$(curl -s -o "$work/console.html" -w '%{http_code}' "$base_url/")"
[ "$status" = 200 ] || fail "console page returned HTTP $status"
grep -q '<title>AstraBox</title>' "$work/console.html" || fail "console page is not the AstraBox console"
grep -Eq '<script[^>]+src="[^"]*assets/' "$work/console.html" || fail "console page references no built assets"
pass "console page served at $base_url/"

health="$(curl -fsS "$base_url/healthz")" || fail "/healthz did not answer"
reported="$(json_field version <<<"$health")"
[ "$reported" = "$version" ] || fail "/healthz reports $reported, not $version"
pass "/healthz reports AstraBox $version"

agents="$(curl -fsS "$base_url/api/v1/agents")" || fail "GET /api/v1/agents failed"
agent_id="$(python3 -c 'import json, sys
agents = [a for a in json.load(sys.stdin)["data"] if a.get("name") == "Claude Code"]
print(agents[0]["agent_id"] if len(agents) == 1 else "")' <<<"$agents")"
[ -n "$agent_id" ] || fail "the seeded Claude Code Agent is not listed exactly once"
pass "API lists the seeded Claude Code Agent ($agent_id)"

session_id="$(curl -fsS -X POST "$base_url/api/v1/agents/$agent_id/conversations" \
  -H 'Content-Type: application/json' --data '{}' | json_field data.session_id)" \
  || fail "creating a Session failed"
pass "created Session $session_id"

deadline=$((SECONDS + ready_timeout_seconds))
while :; do
  state="$(session_state "$session_id")" || fail "reading Session $session_id failed"
  case "$state" in
    READY) break ;;
    CREATING) ;;
    *) fail "Session $session_id entered $state instead of READY" ;;
  esac
  [ "$SECONDS" -lt "$deadline" ] || fail "Session $session_id stayed $state for ${ready_timeout_seconds}s"
  sleep 5
done
pass "Session $session_id is READY"

if [ -n "$agent_image" ]; then
  running="$(docker ps --filter "ancestor=$agent_image" --filter status=running --format '{{.ID}}')" \
    || fail "cannot list this host's containers"
  [ -n "$running" ] || fail "no running container uses the expected Agent image $agent_image"
  pass "a running sandbox uses $agent_image"
fi

[ "$with_turn" = 1 ] || exit 0

curl -sS --no-buffer --max-time 600 -X POST "$base_url/api/v1/sessions/$session_id/ai-stream" \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  --data '{"content": "Reply with one short sentence.", "client_message_id": "installer-acceptance-turn"}' \
  >"$work/turn.sse" || fail "the message request failed"
verdict="$(python3 - "$work/turn.sse" <<'PY'
import json
import sys

# The Claude Code CLI reports a model or gateway HTTP error as text, not as an
# error frame; such text is a failed turn, never a reply.
MODEL_ERROR_MARKERS = (
    "api error",
    "authentication error",
    "invalid api key",
    "status code: 401",
)


def model_error(text: str) -> bool:
    folded = " ".join(text.lower().split())
    return any(marker in folded for marker in MODEL_ERROR_MARKERS)


error = ""
open_blocks: set[str] = set()
completed: list[str] = []
text: dict[str, list[str]] = {}
finish = ""
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    if not line.startswith("data:"):
        continue
    payload = line[len("data:"):].strip()
    if not payload or payload == "[DONE]":
        continue
    frame = json.loads(payload)
    kind = frame.get("type")
    if kind == "text-start":
        open_blocks.add(frame["id"])
        text[frame["id"]] = []
    elif kind == "text-delta" and frame.get("id") in open_blocks:
        text[frame["id"]].append(str(frame.get("delta") or ""))
    elif kind == "text-end" and frame.get("id") in open_blocks:
        open_blocks.discard(frame["id"])
        completed.append("".join(text[frame["id"]]))
    elif kind == "error":
        error = error or str(frame.get("errorText") or "error frame")
    elif kind == "data-result" and isinstance(frame.get("data"), dict):
        result = frame["data"]
        if result.get("is_error") or model_error(str(result.get("result") or "")):
            error = error or str(result.get("result") or "is_error result")
    elif kind == "finish":
        finish = str(frame.get("finishReason") or "")

reply = " ".join(block.strip() for block in completed if block.strip())
if not error and model_error(reply):
    error = reply
if error:
    print("FAIL the turn failed: " + " ".join(error.split())[:300])
elif not reply:
    print("FAIL the turn produced no complete text block")
elif finish != "stop":
    print(f"FAIL the stream finished with {finish or 'no finish frame'}")
else:
    print("PASS " + reply[:200])
PY
)"
case "$verdict" in
  PASS*) pass "the Agent replied through the model service: ${verdict#PASS }" ;;
  *) fail "${verdict#FAIL }" ;;
esac

deadline=$((SECONDS + settle_timeout_seconds))
until [ "$(session_state "$session_id")" = READY ]; do
  [ "$SECONDS" -lt "$deadline" ] || fail "Session $session_id did not return to READY after the reply"
  sleep 2
done
pass "Session $session_id returned to READY"
