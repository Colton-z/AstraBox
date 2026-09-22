#!/usr/bin/env bash
# Reproducible Kubernetes testbed for the open_sandbox backend.
#
# WHY THIS EXISTS: the K8s path is where OpenSandbox's full feature set lives
# (multi-tenancy, Secure Access, ingress are all K8s-only), so it
# needs to be exercised, not assumed. Standing that up by hand leaves
# half-installed state behind — cluster-scoped CRDs outlive a failed `helm
# install` and then poison the next one with mismatched ownership metadata. So
# every step here is idempotent and `down` cleans cluster-scoped objects too.
#
# TOPOLOGY (the same one the single-host deployment uses, one config apart):
#
#   AstraBox container ──HTTP──> opensandbox-server ──k8s API──> BatchSandbox CR
#     (the orchestrator            (a PROCESS the            (the controller
#      starts the server)           orchestrator owns,        reconciles it into
#                                   runtime.type=kubernetes)  sandbox Pods)
#
# The server is NOT deployed into the cluster. Upstream's documented install for
# it is `pip install opensandbox-server` plus a process (the Helm chart upstream
# publishes is the CONTROLLER's, which does belong in the cluster) — and running
# that process is exactly what the AstraBox orchestrator does. So single-host and
# K8s differ only in the server's `[runtime]` block, not in how AstraBox talks to
# it.
#
# WHAT THIS SCRIPT IS ALLOWED TO DO THAT THE LAUNCHER IS NOT: create namespaces.
# opensandbox-server addresses a sandbox namespace rather than creating one, and
# neither does astrabox/deploy/sandbox_server.py — that is a cluster-scoped
# privilege and a cluster administrator's call. This script IS the cluster
# administrator (it installs k3s), so it creates the sandbox namespace here and
# removes it in `down`.
#
# USAGE
#   scripts/k8s-testbed.sh up       # cluster + controller + E2E fixtures, then the env to set
#   scripts/k8s-testbed.sh fixtures # create or update the E2E fixtures on an existing cluster
#
# Pause & Resume uses OpenSandbox's native rootfs snapshot path. The controller
# commit Job expects the standard containerd socket, so this testbed runs k3s
# against that host daemon and ``ensure_snapshot_runtime`` verifies the Pods
# really live in its ``k8s.io`` namespace. Snapshot images go to the node-local
# registry that ``registry`` starts and configures as an insecure pull source
# for k3s.
#   scripts/k8s-testbed.sh status   # what is actually running
#   scripts/k8s-testbed.sh runtime  # pull an Agent image and prove its workspace volume
#   scripts/k8s-testbed.sh down     # remove controller + CRDs + both namespaces
#   scripts/k8s-testbed.sh purge    # down, then uninstall k3s itself
#
# Pinned on purpose: a testbed that drifts with `latest` cannot tell you whether
# a regression is yours or upstream's.
set -euo pipefail

OSB_CONTROLLER_VERSION="${OSB_CONTROLLER_VERSION:-0.2.0}"
# Where the CONTROLLER runs. Not where sandboxes land — the two are different by
# design, and confusing them is the quickest way to a cluster that looks right
# and creates nothing.
OSB_NAMESPACE="${OSB_NAMESPACE:-opensandbox-system}"
# Where sandbox Pods land. Must match ASTRABOX_SANDBOX_SERVER_KUBE_NAMESPACE;
# this is the default that variable already has.
OSB_SANDBOX_NAMESPACE="${OSB_SANDBOX_NAMESPACE:-opensandbox}"
OSB_RELEASE="${OSB_RELEASE:-osb-controller}"
HELM_VERSION="${HELM_VERSION:-v3.16.3}"
K3S_VERSION="${K3S_VERSION:-v1.36.3+k3s1}"
# This script's kubeconfig stays out of $HOME/.kube/config so re-running `up`
# can never destroy a developer's real cluster contexts; see ensure_k3s.
KUBECONFIG_PATH="${KUBECONFIG_PATH:-$HOME/.kube/k3s-testbed.yaml}"
# Only once it exists: pointing $KUBECONFIG at a missing file would make `status`
# and `down` report "no cluster" on a machine whose default config addresses this
# cluster, which is exactly the state a first `up` leaves behind.
if [ -f "$KUBECONFIG_PATH" ]; then
  export KUBECONFIG="$KUBECONFIG_PATH"
fi
# Serves locally built images to the node; see cmd_registry.
REGISTRY_NAME="${REGISTRY_NAME:-osb-registry}"
REGISTRY_STATE="${REGISTRY_STATE:-/var/lib/osb-registry}"
AGENT_IMAGE="${AGENT_IMAGE:-astrabox/sandbox-claude-code:latest}"
SNAPSHOT_COMMITTER_IMAGE="opensandbox/image-committer:v0.1.1"
SNAPSHOT_COMMIT_JOB_TIMEOUT="10m"
WORKSPACE_VOLUME="${WORKSPACE_VOLUME:-}"
WORKSPACE_STORAGE_CLASS="${WORKSPACE_STORAGE_CLASS:-${ASTRABOX_SANDBOX_WORKSPACE_STORAGE_CLASS:-}}"
WORKSPACE_VOLUME_SIZE="${WORKSPACE_VOLUME_SIZE:-20Gi}"
WORKSPACE_HOST_PATH="${WORKSPACE_HOST_PATH:-}"
WORKSPACE_STORAGE_PROVIDER="${WORKSPACE_STORAGE_PROVIDER:-mounted_volume}"
EFS_FILE_SYSTEM_ID="${EFS_FILE_SYSTEM_ID:-}"

TESTBED_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TESTBED_REPO_ROOT="$(cd -- "${TESTBED_SCRIPT_DIR}/.." && pwd)"
E2E_CREDENTIAL_PROBE_NAME="astrabox-e2e-credential-request-probe"
E2E_CREDENTIAL_PROBE_URL="${ASTRABOX_E2E_CREDENTIAL_PROBE_URL:-http://${E2E_CREDENTIAL_PROBE_NAME}.${OSB_SANDBOX_NAMESPACE}.svc.cluster.local}"
E2E_HTTPS_GATEWAY_NAME="astrabox-e2e-https-model-gateway"
E2E_HTTPS_GATEWAY_URL="${ASTRABOX_E2E_HTTPS_MODEL_GATEWAY_URL:-}"
E2E_CREDENTIAL_PROBE_MANIFEST="${TESTBED_REPO_ROOT}/tests/e2e-ui/fixtures/credential-request-probe.yaml"
E2E_HTTPS_GATEWAY_CADDYFILE="${TESTBED_REPO_ROOT}/tests/e2e-ui/fixtures/secure-team-gateway.Caddyfile"

# The controller chart is the only Helm artifact upstream publishes with an
# attached tgz (the all-in-one `helm/opensandbox/0.2.0` tag ships no assets).
CHART_URL="https://github.com/alibaba/OpenSandbox/releases/download/helm/opensandbox-controller/${OSB_CONTROLLER_VERSION}/opensandbox-controller-${OSB_CONTROLLER_VERSION}.tgz"
CHART_SHA256="0e37b51da3f4e36a1d71c8bebbb41a58ba7f0aaf6d47916513fb2beb2d7a8925"
CHART_CACHE_PATH="${OSB_CONTROLLER_CHART_CACHE:-/opt/astrabox-worker-cache/opensandbox-controller-${OSB_CONTROLLER_VERSION}.tgz}"

# Cluster-scoped, so they survive a namespace delete and must be removed by name.
CRDS=(
  batchsandboxes.sandbox.opensandbox.io
  pools.sandbox.opensandbox.io
  sandboxsnapshots.sandbox.opensandbox.io
)

log() { printf '\033[1;34m[testbed]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[testbed] %s\033[0m\n' "$*" >&2; exit 1; }

egress_host_from_url() {
  printf '%s' "$1" \
    | sed -e 's|^[a-zA-Z][a-zA-Z0-9+.-]*://||' \
          -e 's|/.*$||' -e 's|^[^@]*@||' -e 's|:[0-9]*$||'
}

need_sudo() {
  sudo -n true 2>/dev/null || die "passwordless sudo is required to manage k3s"
}

render_external_containerd_config() {
  local output="$1"
  command -v containerd >/dev/null 2>&1 \
    || die "containerd is required for OpenSandbox snapshot commits"
  containerd config default >"$output"

  [ "$(grep -Fc 'SystemdCgroup = false' "$output")" -eq 1 ] \
    || die "the installed containerd has an unsupported SystemdCgroup template"
  [ "$(grep -Fc "bin_dirs = ['/opt/cni/bin']" "$output")" -eq 1 ] \
    || die "the installed containerd has an unsupported CNI bin_dirs template"
  [ "$(grep -Fc "conf_dir = '/etc/cni/net.d'" "$output")" -eq 1 ] \
    || die "the installed containerd has an unsupported CNI conf_dir template"
  [ "$(grep -Fc "config_path = '/etc/containerd/certs.d:/etc/docker/certs.d'" "$output")" -eq 1 ] \
    || die "the installed containerd has an unsupported registry config_path template"

  sed -i \
    -e 's/SystemdCgroup = false/SystemdCgroup = true/' \
    -e "s|bin_dirs = \['/opt/cni/bin'\]|bin_dirs = ['/var/lib/rancher/k3s/data/current/bin']|" \
    -e "s|conf_dir = '/etc/cni/net.d'|conf_dir = '/var/lib/rancher/k3s/agent/etc/cni/net.d'|" \
    -e "s|config_path = '/etc/containerd/certs.d:/etc/docker/certs.d'|config_path = '/etc/containerd/certs.d'|" \
    "$output"
}

wait_for_snapshot_node() {
  log "waiting for the node and snapshot containerd to report Ready"
  local deadline=$((SECONDS + 180))
  until [ "$(kubectl get nodes -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = "True" ] \
      && sudo ctr --address /run/containerd/containerd.sock namespaces list 2>/dev/null \
        | awk 'NR > 1 {print $1}' | grep -Fxq k8s.io; do
    ((SECONDS < deadline)) \
      || die "node did not become Ready on the snapshot containerd within 180 seconds"
    sleep 3
  done
}

ensure_k3s() {
  need_sudo
  command -v ctr >/dev/null 2>&1 \
    || die "ctr is required for OpenSandbox snapshot commits"
  [ -S /run/containerd/containerd.sock ] \
    || die "the standard containerd socket is absent: /run/containerd/containerd.sock"

  local tmp desired_containerd desired_k3s runtime_changed=0 k3s_changed=0
  tmp="$(mktemp -d)"
  desired_containerd="$tmp/containerd.toml"
  desired_k3s="$tmp/k3s.yaml"
  render_external_containerd_config "$desired_containerd"
  printf '%s\n' \
    'container-runtime-endpoint: unix:///run/containerd/containerd.sock' \
    >"$desired_k3s"

  sudo cmp -s "$desired_containerd" /etc/containerd/config.toml \
    || runtime_changed=1
  sudo cmp -s "$desired_k3s" /etc/rancher/k3s/config.yaml \
    || k3s_changed=1
  sudo install -D -m 0644 "$desired_containerd" /etc/containerd/config.toml
  sudo install -D -m 0644 "$desired_k3s" /etc/rancher/k3s/config.yaml
  rm -rf "$tmp"

  if command -v k3s >/dev/null 2>&1; then
    if [ "$runtime_changed" -eq 1 ] || [ "$k3s_changed" -eq 1 ] \
        || ! sudo ctr --address /run/containerd/containerd.sock namespaces list 2>/dev/null \
          | awk 'NR > 1 {print $1}' | grep -Fxq k8s.io; then
      [ -x /usr/local/bin/k3s-killall.sh ] \
        || die "k3s-killall.sh is required to migrate the existing cluster runtime"
      log "moving k3s, CNI, and image pulls onto the snapshot containerd"
      sudo /usr/local/bin/k3s-killall.sh
      sudo systemctl restart containerd
      sudo systemctl start k3s
    else
      log "k3s already uses the snapshot committer's containerd"
    fi
  else
    [ "$runtime_changed" -eq 0 ] || sudo systemctl restart containerd
    log "installing pinned k3s ${K3S_VERSION} (traefik disabled — nothing here needs an ingress yet)"
    curl -sfL https://get.k3s.io \
      | sudo INSTALL_K3S_VERSION="$K3S_VERSION" \
        INSTALL_K3S_EXEC="--disable=traefik --write-kubeconfig-mode=644" sh -
  fi
  wait_for_snapshot_node
  # A user-owned kubeconfig so kubectl/helm work without sudo. 600 because helm
  # refuses to be quiet about a group-readable one.
  #
  # Written to its OWN file, never over $HOME/.kube/config: `up` is re-entrant by
  # design, and a developer whose default config holds EKS/GKE/staging contexts
  # would lose every one of them with no backup — twice, if they ran `up` again.
  # $KUBECONFIG is exported here so the rest of this script (and the export line
  # printed at the end) addresses this cluster explicitly.
  mkdir -p "$HOME/.kube"
  sudo cp /etc/rancher/k3s/k3s.yaml "$KUBECONFIG_PATH"
  sudo chown "$(id -u):$(id -g)" "$KUBECONFIG_PATH"
  chmod 600 "$KUBECONFIG_PATH"
  export KUBECONFIG="$KUBECONFIG_PATH"
  # A machine with no kubeconfig at all is the common fresh-cluster case; adopting it
  # as the default there costs nothing and destroys nothing.
  if [ ! -e "$HOME/.kube/config" ]; then
    cp "$KUBECONFIG_PATH" "$HOME/.kube/config"
    chmod 600 "$HOME/.kube/config"
    log "no existing kubeconfig — also installed as $HOME/.kube/config"
  elif ! cmp -s "$KUBECONFIG_PATH" "$HOME/.kube/config"; then
    log "left your existing $HOME/.kube/config untouched; use KUBECONFIG=$KUBECONFIG_PATH"
  fi
}

ensure_helm() {
  if command -v helm >/dev/null 2>&1; then
    log "helm already present ($(helm version --short 2>/dev/null))"
    return
  fi
  need_sudo
  log "installing helm ${HELM_VERSION}"
  local tmp
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/helm.tgz" \
    "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz"
  tar -xzf "$tmp/helm.tgz" -C "$tmp"
  sudo install -m 0755 "$tmp/linux-amd64/helm" /usr/local/bin/helm
  rm -rf "$tmp"
}

ensure_snapshot_runtime() {
  local target_socket=/run/containerd/containerd.sock
  [ -S "$target_socket" ] \
    || die "the standard containerd socket is absent: ${target_socket}"
  command -v ctr >/dev/null 2>&1 \
    || die "ctr is required to verify the snapshot committer's containerd"
  need_sudo
  sudo ctr --address "$target_socket" namespaces list \
    | awk 'NR > 1 {print $1}' \
    | grep -Fxq k8s.io \
    || die "k3s is not using ${target_socket}; OpenSandbox snapshot commits would inspect the wrong daemon"
}

install_controller() {
  local tmp tgz output ip snapshot_registry
  tmp="$(mktemp -d)"
  tgz="$tmp/opensandbox-controller.tgz"
  ip="$(node_ip)"
  [ -n "$ip" ] || die "no node IP — the snapshot registry cannot be addressed"
  snapshot_registry="${ip}:5000/opensandbox-snapshots"
  if [ -r "$CHART_CACHE_PATH" ]; then
    log "using cached controller chart ${OSB_CONTROLLER_VERSION}"
    cp "$CHART_CACHE_PATH" "$tgz"
  else
    log "fetching controller chart ${OSB_CONTROLLER_VERSION}"
    curl -fsSL -o "$tgz" "$CHART_URL" \
      || die "chart ${OSB_CONTROLLER_VERSION} not downloadable — check the release has an attached tgz"
  fi
  printf '%s  %s\n' "$CHART_SHA256" "$tgz" | sha256sum -c - >/dev/null \
    || die "controller chart ${OSB_CONTROLLER_VERSION} checksum mismatch"
  # upgrade --install so a re-run converges instead of colliding. The output is
  # captured rather than piped: piping into a filter would make the FILTER's exit
  # status the pipeline's, so a failed install would read as a successful one.
  log "installing/upgrading release ${OSB_RELEASE} in ${OSB_NAMESPACE}"
  if ! output="$(helm upgrade --install "$OSB_RELEASE" "$tgz" \
      --namespace "$OSB_NAMESPACE" --create-namespace \
      --set-string "controller.snapshot.registry=${snapshot_registry}" \
      --set controller.snapshot.registryInsecure=true \
      --set-string "controller.snapshot.imageCommitterImage=${SNAPSHOT_COMMITTER_IMAGE}" \
      --set-string "controller.snapshot.commitJobTimeout=${SNAPSHOT_COMMIT_JOB_TIMEOUT}" \
      --wait --timeout 6m 2>&1)"; then
    printf '%s\n' "$output" >&2
    rm -rf "$tmp"
    die "helm could not install the controller"
  fi
  printf '%s\n' "$output" | grep -viE 'coalesce|^warning' || true
  rm -rf "$tmp"
}

ensure_sandbox_namespace() {
  # Pre-created on purpose: opensandbox-server addresses this namespace and does
  # not create it, and AstraBox refuses to create it either (see the header).
  if kubectl get namespace "$OSB_SANDBOX_NAMESPACE" >/dev/null 2>&1; then
    log "sandbox namespace ${OSB_SANDBOX_NAMESPACE} already exists"
  else
    log "creating sandbox namespace ${OSB_SANDBOX_NAMESPACE}"
    kubectl create namespace "$OSB_SANDBOX_NAMESPACE"
  fi
}

validate_e2e_https_gateway_url() {
  [ -n "${E2E_HTTPS_GATEWAY_URL}" ] || return 0
  if ! printf '%s' "${E2E_HTTPS_GATEWAY_URL}" \
      | grep -Eq '^https://[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:443)?$'; then
    die "ASTRABOX_E2E_HTTPS_MODEL_GATEWAY_URL must be an HTTPS FQDN on port 443 with no path"
  fi
  local host
  host="$(egress_host_from_url "${E2E_HTTPS_GATEWAY_URL}")"
  if printf '%s' "${host}" | grep -Eq '^[0-9]+(\.[0-9]+){3}$'; then
    die "ASTRABOX_E2E_HTTPS_MODEL_GATEWAY_URL must use an FQDN, not an IP address"
  fi
}

render_e2e_fixture_manifests() {
  [ -r "${E2E_CREDENTIAL_PROBE_MANIFEST}" ] \
    || die "credential probe manifest is missing: ${E2E_CREDENTIAL_PROBE_MANIFEST}"
  cat "${E2E_CREDENTIAL_PROBE_MANIFEST}"

  [ -n "${E2E_HTTPS_GATEWAY_URL}" ] || return 0
  [ -r "${E2E_HTTPS_GATEWAY_CADDYFILE}" ] \
    || die "HTTPS gateway Caddyfile is missing: ${E2E_HTTPS_GATEWAY_CADDYFILE}"
  local gateway_host gateway_upstream
  gateway_host="$(egress_host_from_url "${E2E_HTTPS_GATEWAY_URL}")"
  gateway_upstream="http://astrabox-model-gw.${OSB_SANDBOX_NAMESPACE}.svc.cluster.local:80"

  # The upstream the Caddyfile names. A selector-less Service plus a manual
  # Endpoints is the k8s way to give a cluster name to a process on the host:
  # LiteLLM listens on the Docker bridge address, which no selector can find.
  # Both resources are required: without endpoint backing, Caddy can resolve
  # the Service name but has no LiteLLM upstream to reach and returns 502.
  printf '\n---\n'
  cat <<YAML
apiVersion: v1
kind: Service
metadata:
  name: astrabox-model-gw
  labels:
    app.kubernetes.io/part-of: astrabox-e2e
spec:
  ports:
  - port: 80
    targetPort: ${E2E_MODEL_GW_HOST_PORT:-4400}
    protocol: TCP
---
apiVersion: v1
kind: Endpoints
metadata:
  name: astrabox-model-gw
  labels:
    app.kubernetes.io/part-of: astrabox-e2e
subsets:
- addresses:
  - ip: ${E2E_MODEL_GW_HOST_IP:-172.17.0.1}
  ports:
  - port: ${E2E_MODEL_GW_HOST_PORT:-4400}
    protocol: TCP
YAML

  printf '\n---\n'
  cat <<YAML
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${E2E_HTTPS_GATEWAY_NAME}
  labels:
    app.kubernetes.io/name: ${E2E_HTTPS_GATEWAY_NAME}
    app.kubernetes.io/part-of: astrabox-e2e
data:
  Caddyfile: |
YAML
  sed 's/^/    /' "${E2E_HTTPS_GATEWAY_CADDYFILE}"
  cat <<YAML
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${E2E_HTTPS_GATEWAY_NAME}
  labels:
    app.kubernetes.io/name: ${E2E_HTTPS_GATEWAY_NAME}
    app.kubernetes.io/part-of: astrabox-e2e
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 256Mi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${E2E_HTTPS_GATEWAY_NAME}
  labels:
    app.kubernetes.io/name: ${E2E_HTTPS_GATEWAY_NAME}
    app.kubernetes.io/part-of: astrabox-e2e
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: ${E2E_HTTPS_GATEWAY_NAME}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${E2E_HTTPS_GATEWAY_NAME}
    spec:
      automountServiceAccountToken: false
      securityContext:
        fsGroup: 65532
      containers:
        - name: gateway
          image: caddy:2.10-alpine
          imagePullPolicy: IfNotPresent
          env:
            - name: ASTRABOX_E2E_GATEWAY_DOMAIN
              value: "${gateway_host}"
            - name: ASTRABOX_E2E_GATEWAY_UPSTREAM
              value: "${gateway_upstream}"
          ports:
            - name: https
              containerPort: 443
          readinessProbe:
            tcpSocket:
              port: https
            periodSeconds: 2
            timeoutSeconds: 1
          resources:
            requests:
              cpu: 20m
              memory: 32Mi
            limits:
              cpu: 250m
              memory: 128Mi
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
              add: ["NET_BIND_SERVICE"]
            runAsNonRoot: true
            runAsUser: 65532
            seccompProfile:
              type: RuntimeDefault
          volumeMounts:
            - name: caddyfile
              mountPath: /etc/caddy/Caddyfile
              subPath: Caddyfile
              readOnly: true
            - name: data
              mountPath: /data
            - name: config
              mountPath: /config
      volumes:
        - name: caddyfile
          configMap:
            name: ${E2E_HTTPS_GATEWAY_NAME}
        - name: data
          persistentVolumeClaim:
            claimName: ${E2E_HTTPS_GATEWAY_NAME}
        - name: config
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: ${E2E_HTTPS_GATEWAY_NAME}
  labels:
    app.kubernetes.io/name: ${E2E_HTTPS_GATEWAY_NAME}
    app.kubernetes.io/part-of: astrabox-e2e
spec:
  type: LoadBalancer
  selector:
    app.kubernetes.io/name: ${E2E_HTTPS_GATEWAY_NAME}
  ports:
    - name: https
      port: 443
      targetPort: https
YAML
}

wait_for_e2e_https_gateway() {
  [ -n "${E2E_HTTPS_GATEWAY_URL}" ] || return 0
  command -v curl >/dev/null \
    || die "curl is required to verify the HTTPS model gateway"
  log "waiting for the trusted HTTPS model gateway at ${E2E_HTTPS_GATEWAY_URL}"
  local deadline=$((SECONDS + 300))
  local status
  until status="$(curl --silent --show-error --output /dev/null \
      --write-out '%{http_code}' --connect-timeout 5 --max-time 10 \
      "${E2E_HTTPS_GATEWAY_URL}/v1/models")" \
      && case "${status}" in 200|401|403) true ;; *) false ;; esac; do
    ((SECONDS < deadline)) \
      || die "HTTPS model gateway did not reach its upstream within 5 minutes: ${E2E_HTTPS_GATEWAY_URL} (last HTTP status ${status:-none})"
    sleep 2
  done
}

install_e2e_fixtures() {
  validate_e2e_https_gateway_url
  log "installing the credential request probe in ${OSB_SANDBOX_NAMESPACE}"
  if [ -n "${E2E_HTTPS_GATEWAY_URL}" ]; then
    log "installing the trusted-TLS model gateway for ${E2E_HTTPS_GATEWAY_URL}"
  else
    log "ASTRABOX_E2E_HTTPS_MODEL_GATEWAY_URL is unset; the probe will be installed, but the HTTPS gateway fixture will not"
  fi
  render_e2e_fixture_manifests \
    | kubectl -n "${OSB_SANDBOX_NAMESPACE}" apply -f -
  kubectl -n "${OSB_SANDBOX_NAMESPACE}" rollout status \
    "deployment/${E2E_CREDENTIAL_PROBE_NAME}" --timeout=2m
  if [ -n "${E2E_HTTPS_GATEWAY_URL}" ]; then
    kubectl -n "${OSB_SANDBOX_NAMESPACE}" rollout status \
      "deployment/${E2E_HTTPS_GATEWAY_NAME}" --timeout=2m
    wait_for_e2e_https_gateway
  fi
}

cmd_fixtures() {
  ensure_sandbox_namespace
  install_e2e_fixtures
}

node_ip() {
  kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}'
}

ensure_local_registry() {
  # Both agent pulls and snapshot commits must use a registry the node runtime
  # can resolve. The standard containerd reads native hosts.toml files; k3s's
  # registries.yaml belongs to its embedded daemon and is inert here.
  need_sudo
  local ip hosts_dir desired current tmp registry_ready
  ip="$(node_ip)"
  [ -n "$ip" ] || die "no node IP — is the cluster up?"
  # Presence is not the criterion, configuration is. A registry started before
  # deletion was enabled refuses DELETE for the life of the host, and an
  # "already running" check never notices — the flag below then sits in this
  # script while nothing on that host can ever be reclaimed, until a disk
  # preflight refuses a deploy. Recreating is safe: the state lives in the
  # bind mount, not the container.
  if [ -n "$(docker ps -q -f "name=^${REGISTRY_NAME}$" 2>/dev/null)" ] \
    && docker inspect "$REGISTRY_NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
      | grep -q '^REGISTRY_STORAGE_DELETE_ENABLED=true$'; then
    log "registry already running with deletion enabled"
  else
    log "starting registry on ${ip}:5000 (state in ${REGISTRY_STATE})"
    docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
    # Deletion stays enabled because park/pause commits one snapshot repo per
    # sandbox here, and destroy is the only chance to reclaim it — with the
    # API refusing DELETE, orphaned snapshot repos accumulate silently until
    # the disk preflight refuses the whole deploy (measured: ~70 repos/18G).
    docker run -d --name "$REGISTRY_NAME" --restart=always \
      -e REGISTRY_STORAGE_DELETE_ENABLED=true \
      -p 5000:5000 -v "${REGISTRY_STATE}:/var/lib/registry" registry:2 >/dev/null
  fi

  hosts_dir="/etc/containerd/certs.d/${ip}:5000"
  desired="server = \"http://${ip}:5000\"

[host.\"http://${ip}:5000\"]
  capabilities = [\"pull\", \"resolve\"]
  skip_verify = true"
  current="$(sudo cat "${hosts_dir}/hosts.toml" 2>/dev/null || true)"
  if [ "$current" = "$desired" ]; then
    log "snapshot containerd already trusts ${ip}:5000"
  else
    tmp="$(mktemp)"
    printf '%s\n' "$desired" >"$tmp"
    sudo install -D -m 0644 "$tmp" "${hosts_dir}/hosts.toml"
    rm -f "$tmp"
    log "configured ${ip}:5000 as the snapshot containerd's HTTP registry"
  fi
  registry_ready=""
  for attempt in $(seq 1 30); do
    if curl --fail --silent --show-error "http://${ip}:5000/v2/" >/dev/null 2>&1; then
      registry_ready=1
      break
    fi
    sleep 1
  done
  if [ -z "$registry_ready" ]; then
    docker inspect "$REGISTRY_NAME" --format 'state={{.State.Status}} error={{.State.Error}}' >&2 \
      || true
    docker logs --tail 20 "$REGISTRY_NAME" >&2 || true
    die "the node-local registry did not serve /v2/ at ${ip}:5000 within 30 seconds"
  fi
}

pull_agent_image_through_cri() {
  local runtime_endpoint="unix:///run/containerd/containerd.sock"
  local crictl_path timeout_path
  crictl_path="$(command -v crictl)" \
    || die "crictl is required to verify kubelet image pulls"
  timeout_path="$(command -v timeout)" \
    || die "timeout is required to bound the kubelet image-pull check"
  [ -S /run/containerd/containerd.sock ] \
    || die "the standard containerd socket is absent: /run/containerd/containerd.sock"
  need_sudo
  log "proving kubelet can pull ${AGENT_IMAGE} through CRI"
  sudo "$timeout_path" --signal=TERM --kill-after=5s 180s \
    "$crictl_path" \
      --runtime-endpoint "$runtime_endpoint" \
      --image-endpoint "$runtime_endpoint" \
      pull "$AGENT_IMAGE" >/dev/null \
    || die "CRI could not pull ${AGENT_IMAGE} within 180 seconds; fix the node registry configuration before deploying the runtime"
}

cmd_up() {
  ensure_k3s
  ensure_helm
  ensure_local_registry
  ensure_snapshot_runtime
  install_controller
  cmd_fixtures
  # Runtime image validation is deliberately explicit. `up` can be called
  # before a release image exists, so the release deployer calls `runtime` only
  # after the immutable manifest names the new image.
  cmd_status
  local ip gateway_fixture_note compose_gateway_url
  ip="$(node_ip)"
  compose_gateway_url="${E2E_HTTPS_GATEWAY_URL:-https://<pod-reachable-litellm-fqdn>}"
  if [ -n "${E2E_HTTPS_GATEWAY_URL}" ]; then
    gateway_fixture_note="     HTTPS gateway: ${E2E_HTTPS_GATEWAY_URL}
     Set ASTRABOX_LITELLM_BASE_URL to that same origin when deploying AstraBox."
  else
    gateway_fixture_note="     HTTPS gateway: not installed; set ASTRABOX_E2E_HTTPS_MODEL_GATEWAY_URL
     to a public FQDN before the next 'up'."
  fi
  cat <<EOF

$(log 'controller is up. To point AstraBox at this cluster:')

  1. The orchestrator starts opensandbox-server itself. The maintained
     Kubernetes Compose overlay selects that runtime and mounts the kubeconfig;
     do not mount the Docker socket or start a second lifecycle server.

  2. That API server address is not cosmetic. This kubeconfig says
     https://127.0.0.1:6443, which inside a container is the container itself —
     and the obvious fix (host.docker.internal) reaches the API server and then
     fails TLS, because the certificate covers the node's names and IPs, not
     Docker's gateway name. ${ip:+The node IP above is in that certificate.}
     Check for yourself with:
       openssl s_client -connect ${ip:-<node-ip>}:6443 </dev/null 2>/dev/null \\
         | openssl x509 -noout -text | grep -A1 'Subject Alternative Name'

  3. The kubeconfig must be readable by the image's unprivileged user (uid 999,
     'astrabox'), which is not the uid that owns it on the host — otherwise the
     server exits 503 with EACCES before it serves anything. Hand it a copy owned
     by that uid rather than widening the original, which holds cluster-admin
     credentials:
       sudo install -o 999 -g 999 -m 600 ${KUBECONFIG_PATH} \\
         \$HOME/astrabox-kubeconfig     # then mount THIS one at /etc/astrabox/kubeconfig

  4. The agent image is pulled by the NODE, not by AstraBox and not by the
     server. A locally built one lives in the Docker daemon's store, which k3s
     (containerd) cannot see. Run 'registry' below to serve it from this host
     (13.9GB pulled by the node in 2m18s on the testbed), then point AstraBox at
     the registry name:
       ${0##*/} registry
       export ASTRABOX_AGENT_IMAGE=${ip:-<node-ip>}:5000/astrabox/sandbox-claude-code:latest

  5. Every address AstraBox hands to the sandbox is dialled from the POD network,
     not from the AstraBox container: host.docker.internal does not exist in a
     Pod. A model endpoint on the host must be given as an address the Pod can
     reach — the Docker bridge gateway (172.17.0.1) if that is where it is
     published, the node IP if it listens on all interfaces.

  6. From the repository root, start the complete deployment through the
     secret-generating wrapper. The callback address and bind IP are the same
     node/private address so Pods can call AstraBox. The model URL must be an
     external Pod-reachable gateway, not the embedded Docker-only gateway:

       export ASTRABOX_KUBECONFIG_HOST_PATH=\$HOME/astrabox-kubeconfig
       export ASTRABOX_SANDBOX_SERVER_KUBE_API_SERVER=https://${ip:-<node-ip>}:6443
       export ASTRABOX_SERVER_BIND_IP=${ip:-<node-ip>}
       export ASTRABOX_MCP_PROXY_BASE_URL=http://${ip:-<node-ip>}:8088
       export ASTRABOX_ALLOWED_HOSTS=${ip:-<node-ip>}
       export ASTRABOX_LITELLM_BASE_URL=${compose_gateway_url}
       scripts/compose.sh -f containers/compose.kubernetes.yaml up -d

  7. The credential request-matching fixture is installed with the cluster:
       export ASTRABOX_E2E_CREDENTIAL_PROBE_URL=${E2E_CREDENTIAL_PROBE_URL}
${gateway_fixture_note}
EOF
}

live_sandbox_resources() {
  # `kubectl get a,b,c` fails as a WHOLE when any one type is unregistered, and
  # under `set -o pipefail` that status would propagate out of the assignment in
  # cmd_down and trip `set -e` before a single line is logged — so `down` would
  # exit 1 in silence on the two most likely calls: after a half-failed `up`, and
  # on a second `down`. Count per type, and treat "no such type" as zero.
  local total=0 kind count
  for kind in batchsandboxes pools sandboxsnapshots; do
    count="$(kubectl get "$kind" -A --no-headers 2>/dev/null | grep -c . || true)"
    total=$((total + count))
  done
  printf '%s\n' "$total"
}

cmd_status() {
  log "cluster"
  kubectl get nodes 2>/dev/null || { echo "  (no cluster)"; return; }
  log "controller (${OSB_NAMESPACE})"
  kubectl get pods -n "$OSB_NAMESPACE" 2>/dev/null || echo "  (namespace absent)"
  log "sandbox namespace (${OSB_SANDBOX_NAMESPACE})"
  kubectl get pods -n "$OSB_SANDBOX_NAMESPACE" 2>/dev/null || echo "  (namespace absent)"
  log "E2E fixtures (${OSB_SANDBOX_NAMESPACE})"
  kubectl get deployment,service,persistentvolumeclaim \
    -n "$OSB_SANDBOX_NAMESPACE" \
    -l app.kubernetes.io/part-of=astrabox-e2e 2>/dev/null \
    || echo "  (none)"
  log "CRDs"
  kubectl get crd 2>/dev/null | grep -E 'opensandbox|agents.x-k8s.io' || echo "  (none)"
  log "sandbox workloads"
  kubectl get batchsandboxes,pools -A --no-headers 2>/dev/null | grep . \
    || echo "  (none)"
}

cmd_down() {
  command -v kubectl >/dev/null 2>&1 || die "kubectl absent — nothing to tear down"
  # BEFORE anything is removed. Refusing after the controller is gone would leave
  # the cluster half-torn-down with live sandboxes and nothing left to reconcile
  # them — the state this guard exists to prevent.
  local live
  live="$(live_sandbox_resources)"
  if [ "$live" -gt 0 ]; then
    die "refusing to tear down while ${live} sandbox resource(s) still exist — inspect with 'status'"
  fi
  log "uninstalling ${OSB_RELEASE}"
  helm uninstall "$OSB_RELEASE" -n "$OSB_NAMESPACE" 2>/dev/null || true
  # CRDs are cluster-scoped: helm leaves them behind, and a leftover CRD carries
  # the old release's ownership annotations, which makes the NEXT install fail
  # with "invalid ownership metadata". Removing them is what makes `up` idempotent.
  log "removing cluster-scoped CRDs"
  kubectl delete crd "${CRDS[@]}" --ignore-not-found 2>/dev/null || true
  log "removing namespaces"
  kubectl delete ns "$OSB_NAMESPACE" "$OSB_SANDBOX_NAMESPACE" --ignore-not-found 2>/dev/null || true
  log "down"
}

cmd_registry() {
  # The node pulls images through containerd, which cannot see the Docker
  # daemon's store — so a locally built agent image is invisible to the cluster
  # no matter how many times you `docker build`. A registry is what a multi-node
  # cluster would need anyway, and it re-pushes only changed layers.
  ensure_k3s
  ensure_local_registry
  local ip
  ip="$(node_ip)"
  # The seed push is for the developer loop, where the image was just built
  # locally. On a freshly provisioned host no agent image exists yet — the
  # release build is what creates and pushes it — and failing here would make
  # the registry step unrunnable on exactly the host that needs it most.
  if docker image inspect "$AGENT_IMAGE" >/dev/null 2>&1; then
    log "pushing ${AGENT_IMAGE} (large; layers are reused on later pushes)"
    docker tag "$AGENT_IMAGE" "localhost:5000/${AGENT_IMAGE}"
    docker push "localhost:5000/${AGENT_IMAGE}"
    log "done — set ASTRABOX_AGENT_IMAGE=${ip}:5000/${AGENT_IMAGE}"
  else
    log "no local ${AGENT_IMAGE} to seed — the release build pushes the real one"
  fi
}

assert_workspace_claim_is_durable() {
  # A named claim that does not exist, or exists unbound, is the shape that
  # loses a user's files silently: the platform asks the backend for a mount,
  # the backend cannot provide it, and the box either fails to start or runs on
  # its own disk. Neither is discoverable from inside the platform, which talks
  # to OpenSandbox and not to Kubernetes — so the deployment is the only place
  # this can be established.
  local namespace="$1"
  [ -n "${WORKSPACE_VOLUME}" ] || {
    [ "${WORKSPACE_STORAGE_PROVIDER}" = mounted_volume ] && [ -z "${EFS_FILE_SYSTEM_ID}" ] \
      || die "no-volume deployment cannot select an EFS provider or filesystem"
    log "no durable workspace claim configured; boxes keep their files on their own disk"
    return 0
  }
  if [ "${WORKSPACE_STORAGE_PROVIDER}" = aws_efs ]; then
    [ -z "${WORKSPACE_STORAGE_CLASS}" ] || die "static EFS backing cannot select a dynamic StorageClass"
    python3 "${TESTBED_SCRIPT_DIR}/efs-workspace.py" --volume "${WORKSPACE_VOLUME}" \
      --namespace "${namespace}" --file-system-id "${EFS_FILE_SYSTEM_ID}" --size "${WORKSPACE_VOLUME_SIZE}" \
      || die "AWS EFS workspace preparation failed; resources retained"
    return 0
  fi
  [ "${WORKSPACE_STORAGE_PROVIDER}" = mounted_volume ] && [ -z "${EFS_FILE_SYSTEM_ID}" ] \
    || die "invalid workspace provider configuration"
  [ -n "${WORKSPACE_HOST_PATH}" ] || {
    die "WORKSPACE_HOST_PATH must name the candidate-owned durable workspace directory"
  }
  [[ "${WORKSPACE_HOST_PATH}" =~ ^/[A-Za-z0-9._/-]+$ \
    && "${WORKSPACE_HOST_PATH}" != / \
    && "$(realpath -m -- "${WORKSPACE_HOST_PATH}")" = "${WORKSPACE_HOST_PATH}" ]] \
    || die "WORKSPACE_HOST_PATH must be a normalized absolute path"
  local phase
  phase="$(kubectl -n "${namespace}" get pvc "${WORKSPACE_VOLUME}" \
    -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  if [ -z "${phase}" ]; then
    if [ -n "${WORKSPACE_STORAGE_CLASS}" ]; then
      log "creating durable workspace claim ${WORKSPACE_VOLUME} (${WORKSPACE_VOLUME_SIZE}, class=${WORKSPACE_STORAGE_CLASS})"
      kubectl -n "${namespace}" apply -f - >/dev/null <<YAML || die "could not create the workspace claim ${WORKSPACE_VOLUME}"
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${WORKSPACE_VOLUME}
  labels:
    app.kubernetes.io/part-of: astrabox
spec:
  accessModes: ["ReadWriteMany"]
  storageClassName: ${WORKSPACE_STORAGE_CLASS}
  resources:
    requests:
      storage: ${WORKSPACE_VOLUME_SIZE}
YAML
    else
      # The maintained testbed is one node. Its first durable workspace was a
      # hand-applied hostPath PV over this directory; replacing it with
      # loopback NFS later froze that node under writeback pressure. Restore
      # the proven one-node shape here so a fresh worker does not depend on
      # unrecorded cluster state. Multi-node deployments name an external RWX
      # StorageClass above instead.
      need_sudo
      sudo install -d -m 0777 "${WORKSPACE_HOST_PATH}" \
        || die "could not prepare ${WORKSPACE_HOST_PATH} for durable workspaces"
      local retained_pv pv_phase pv_path pv_class pv_reclaim pv_claim_namespace pv_claim_name
      retained_pv="$(kubectl get pv "${WORKSPACE_VOLUME}" \
        -o jsonpath='{.status.phase}{"|"}{.spec.hostPath.path}{"|"}{.spec.storageClassName}{"|"}{.spec.persistentVolumeReclaimPolicy}{"|"}{.spec.claimRef.namespace}{"|"}{.spec.claimRef.name}' \
        2>/dev/null || true)"
      if [ -n "${retained_pv}" ]; then
        IFS='|' read -r pv_phase pv_path pv_class pv_reclaim \
          pv_claim_namespace pv_claim_name <<<"${retained_pv}"
        [ "${pv_path}" = "${WORKSPACE_HOST_PATH}" ] \
          && [ "${pv_class}" = "${WORKSPACE_VOLUME}" ] \
          && [ "${pv_reclaim}" = Retain ] \
          || die "existing PV ${WORKSPACE_VOLUME} is not the maintained single-node workspace volume"
        case "${pv_phase}" in
          Available)
            [ -z "${pv_claim_namespace}${pv_claim_name}" ] \
              || die "available workspace PV ${WORKSPACE_VOLUME} still names a prior claim"
            ;;
          Released)
            [ "${pv_claim_namespace}" = "${namespace}" ] \
              && [ "${pv_claim_name}" = "${WORKSPACE_VOLUME}" ] \
              || die "released workspace PV ${WORKSPACE_VOLUME} belongs to a different claim"
            log "releasing the prior ${namespace}/${WORKSPACE_VOLUME} binding while retaining its workspace files"
            kubectl patch pv "${WORKSPACE_VOLUME}" --type=merge \
              -p '{"spec":{"claimRef":null}}' >/dev/null \
              || die "could not release the prior binding for workspace PV ${WORKSPACE_VOLUME}"
            ;;
          *)
            die "workspace PVC ${namespace}/${WORKSPACE_VOLUME} is absent but its PV is ${pv_phase:-unknown}, not Available or Released"
            ;;
        esac
      fi
      log "creating single-node durable workspace PV/PVC ${WORKSPACE_VOLUME} (${WORKSPACE_VOLUME_SIZE}, hostPath=${WORKSPACE_HOST_PATH})"
      kubectl apply -f - >/dev/null <<YAML || die "could not create the workspace PV/PVC ${WORKSPACE_VOLUME}"
apiVersion: v1
kind: PersistentVolume
metadata:
  name: ${WORKSPACE_VOLUME}
  labels:
    app.kubernetes.io/part-of: astrabox
spec:
  accessModes: ["ReadWriteMany"]
  capacity:
    storage: ${WORKSPACE_VOLUME_SIZE}
  hostPath:
    path: ${WORKSPACE_HOST_PATH}
    type: Directory
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ${WORKSPACE_VOLUME}
  volumeMode: Filesystem
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${WORKSPACE_VOLUME}
  namespace: ${namespace}
  labels:
    app.kubernetes.io/part-of: astrabox
spec:
  accessModes: ["ReadWriteMany"]
  storageClassName: ${WORKSPACE_VOLUME}
  volumeName: ${WORKSPACE_VOLUME}
  resources:
    requests:
      storage: ${WORKSPACE_VOLUME_SIZE}
YAML
    fi
    # Binding is asynchronous even for a static PV. Keep the release bounded,
    # then read the actual phase so the refusal below names what Kubernetes did.
    kubectl -n "${namespace}" wait \
      --for=jsonpath='{.status.phase}'=Bound \
      "pvc/${WORKSPACE_VOLUME}" --timeout=30s >/dev/null 2>&1 || true
    phase="$(kubectl -n "${namespace}" get pvc "${WORKSPACE_VOLUME}" \
      -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  fi
  if [ "${phase}" != "Bound" ]; then
    die "durable workspace claim ${WORKSPACE_VOLUME} is ${phase:-absent}, not Bound. \
Every conversation's files are supposed to live on it, and a box that cannot mount it \
writes to its own disk instead. Provide a StorageClass that can satisfy \
ReadWriteMany (set WORKSPACE_STORAGE_CLASS), or repair the existing claim before \
starting AstraBox"
  fi
  # A workspace volume served by THIS node's own NFS server deadlocks the
  # whole machine under write load: memory reclaim flushes the NFS client's
  # dirty pages, the commit waits on the local nfsd, and nfsd's ext4 write
  # needs the memory being reclaimed. Every nfsd thread parks in D state and
  # sandboxes writing to that mount can hang with it. On one node a
  # hostPath PersistentVolume over the same directory is the
  # supported shape; a real multi-node deployment must serve NFS from a
  # machine that is not also a client.
  local bound_pv nfs_server this_node
  this_node="$(node_ip || true)"
  bound_pv="$(kubectl -n "${namespace}" get pvc "${WORKSPACE_VOLUME}" \
    -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)"
  if [ -n "${bound_pv}" ]; then
    nfs_server="$(kubectl get pv "${bound_pv}" \
      -o jsonpath='{.spec.nfs.server}' 2>/dev/null || true)"
    case "${nfs_server}" in
      "") : ;;
      127.0.0.1|localhost|"${this_node}")
        die "workspace claim ${WORKSPACE_VOLUME} is served by NFS from this \
same node (${nfs_server}); loopback NFS deadlocks under write load. Bind the \
claim to a hostPath PersistentVolume over the same directory instead, or \
serve NFS from another machine"
        ;;
    esac
  fi
  log "durable workspace claim ${WORKSPACE_VOLUME} is Bound (class=$(kubectl -n "${namespace}" get pvc "${WORKSPACE_VOLUME}" -o jsonpath='{.spec.storageClassName}' 2>/dev/null || true):-<none>)"
}


cmd_runtime() {
  pull_agent_image_through_cri
  assert_workspace_claim_is_durable "${OSB_SANDBOX_NAMESPACE}"
  log "runtime image and configured workspace prerequisites are ready"
}

cmd_purge() {
  cmd_down || true
  if [ -x /usr/local/bin/k3s-uninstall.sh ]; then
    need_sudo
    log "uninstalling k3s"
    sudo /usr/local/bin/k3s-uninstall.sh
  else
    log "k3s uninstaller not present; nothing to purge"
  fi
}

# Sourcing the script defines its functions without running a subcommand, which
# is how the deployment helper contracts are exercised by the unit suite.
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  return 0 2>/dev/null || true
fi

case "${1:-}" in
  up) cmd_up ;;
  fixtures) cmd_fixtures ;;
  down) cmd_down ;;
  status) cmd_status ;;
  registry) cmd_registry ;;
  runtime) cmd_runtime ;;
  purge) cmd_purge ;;
  *)
    # Spelled out rather than sliced out of the header comment: a line range is
    # wrong the moment the header grows a paragraph.
    cat >&2 <<'EOF'
Reproducible Kubernetes testbed for the open_sandbox backend.

  scripts/k8s-testbed.sh up       cluster + controller + E2E fixtures, then the env to set
  scripts/k8s-testbed.sh fixtures create or update E2E fixtures on an existing cluster
  scripts/k8s-testbed.sh status   what is actually running
  scripts/k8s-testbed.sh registry serve locally built images to the node
  scripts/k8s-testbed.sh runtime  pull an Agent image and prove its workspace volume
  scripts/k8s-testbed.sh down     remove controller + CRDs + both namespaces
  scripts/k8s-testbed.sh purge    down, then uninstall k3s itself

See docs/providers/opensandbox.md for what this stands up and why.
EOF
    exit 2
    ;;
esac
