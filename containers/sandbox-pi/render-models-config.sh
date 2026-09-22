#!/bin/sh
# Write pi's provider configuration into the box, under the account the base
# image creates.
#
# Pi reaches any OpenAI-compatible endpoint through a provider declared in
# models.json, so the deployment's gateway needs no shim. That file accepts
# environment interpolation for `apiKey` and `headers` but requires a LITERAL
# `baseUrl`, which is why it is rendered here rather than baked: the base URL
# is a deployment fact, and the image must serve every deployment.
#
# The credential is deliberately NOT written. The file carries the variable's
# NAME and pi resolves it at request time, so the value never lands on disk —
# which is also what lets the egress sidecar swap a placeholder for the real
# key.
set -eu

: "${ASTRABOX_WORKLOAD_USER:?ASTRABOX_WORKLOAD_USER is required}"
PI_PROVIDER_NAME="${PI_PROVIDER_NAME:-astrabox}"

fail() {
    # Loud and terminal. A box whose pi has no gateway reaches READY and then
    # cannot answer, which is the failure shape that costs a whole session to
    # diagnose; saying so in the boot log is what makes it one glance instead.
    echo "astrabox-pi-config: $*" >&2
    exit 1
}

[ -n "${ASTRABOX_PI_BASE_URL:-}" ] || fail "ASTRABOX_PI_BASE_URL is unset; pi has no model gateway to reach"
[ -n "${ASTRABOX_PI_MODEL:-}" ] || fail "ASTRABOX_PI_MODEL is unset; pi has no model to publish"

# The base image creates its workload account during boot. Wait for that fixed
# image capability; never create or alter an account here — the entrypoint owns
# it, and a second account at the same uid kills the whole box.
waits=0
while ! passwd_entry="$(getent passwd "$ASTRABOX_WORKLOAD_USER")"; do
    waits=$((waits + 1))
    if [ "$waits" -ge 300 ]; then
        fail "workload account $ASTRABOX_WORKLOAD_USER was not created"
    fi
    sleep 0.1
done

workload_home="$(printf '%s\n' "$passwd_entry" | cut -d: -f6)"
[ -n "$workload_home" ] || fail "workload account $ASTRABOX_WORKLOAD_USER has no home"

agent_dir="${PI_CODING_AGENT_DIR:-${workload_home}/.pi/agent}"
install -d -o "$ASTRABOX_WORKLOAD_USER" -g "$ASTRABOX_WORKLOAD_USER" -m 0755 \
    "$agent_dir" "${workload_home}/.pi/sessions"

# jq renders the JSON so a base URL or model id containing a quote cannot break
# the file. The apiKey is the literal string "$ASTRABOX_PI_API_KEY": pi
# resolves it, this script does not.
tmp="${agent_dir}/.models.json.tmp"
jq -n \
    --arg provider "$PI_PROVIDER_NAME" \
    --arg base_url "$ASTRABOX_PI_BASE_URL" \
    --arg model "$ASTRABOX_PI_MODEL" \
    '{
        providers: {
            ($provider): {
                baseUrl: $base_url,
                api: "openai-completions",
                apiKey: "$ASTRABOX_PI_API_KEY",
                models: [ { id: $model } ]
            }
        }
    }' > "$tmp"

chown "$ASTRABOX_WORKLOAD_USER:$ASTRABOX_WORKLOAD_USER" "$tmp"
chmod 0644 "$tmp"
# Renamed into place so a pi process starting concurrently reads either the
# previous file or the complete new one, never a half-written one.
mv -f "$tmp" "${agent_dir}/models.json"

echo "astrabox-pi-config: wrote ${agent_dir}/models.json for provider ${PI_PROVIDER_NAME}"
