"""The Kubernetes testbed installs OpenSandbox's real snapshot prerequisites."""

from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "k8s-testbed.sh"


def test_controller_is_given_a_reachable_snapshot_registry_and_committer() -> None:
    """Removing a Helm value makes pause fail before it can commit a sandbox."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'snapshot_registry="${ip}:5000/opensandbox-snapshots"' in source
    assert '--set-string "controller.snapshot.registry=${snapshot_registry}"' in source
    assert "--set controller.snapshot.registryInsecure=true" in source
    assert (
        '--set-string "controller.snapshot.imageCommitterImage=${SNAPSHOT_COMMITTER_IMAGE}"'
        in source
    )
    assert (
        '--set-string "controller.snapshot.commitJobTimeout=${SNAPSHOT_COMMIT_JOB_TIMEOUT}"'
        in source
    )
    assert "opensandbox/image-committer:v0.1.1" in source
    assert 'SNAPSHOT_COMMIT_JOB_TIMEOUT="10m"' in source


def test_up_converges_k3s_cni_and_images_on_the_committer_containerd() -> None:
    """The commit Job must inspect the daemon that actually owns sandbox Pods."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "container-runtime-endpoint: unix:///run/containerd/containerd.sock" in source
    assert 'K3S_VERSION="${K3S_VERSION:-v1.36.3+k3s1}"' in source
    assert "SystemdCgroup = true" in source
    assert "/var/lib/rancher/k3s/data/current/bin" in source
    assert "/var/lib/rancher/k3s/agent/etc/cni/net.d" in source
    assert "sudo /usr/local/bin/k3s-killall.sh" in source
    assert "target_socket=/run/containerd/containerd.sock" in source
    assert 'sudo ctr --address "$target_socket" namespaces list' in source
    assert "grep -Fxq k8s.io" in source
    assert "ensure_local_registry\n  ensure_snapshot_runtime\n  install_controller" in source


def test_node_local_registry_is_configured_for_the_external_runtime() -> None:
    """The embedded-k3s registry file cannot configure the selected runtime."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "/etc/containerd/certs.d/${ip}:5000" in source
    assert 'server = \\"http://${ip}:5000\\"' in source
    assert '[host.\\"http://${ip}:5000\\"]' in source
    assert 'capabilities = [\\"pull\\", \\"resolve\\"]' in source
    assert "/etc/rancher/k3s/registries.yaml" not in source


def test_node_local_registry_waits_for_the_distribution_api() -> None:
    """Container creation precedes service readiness on a clean Docker host."""
    source = SCRIPT.read_text(encoding="utf-8")
    helper = source[
        source.index("ensure_local_registry() {") : source.index("pull_agent_image_through_cri() {")
    ]

    assert helper.index('docker run -d --name "$REGISTRY_NAME"') < helper.index(
        "for attempt in $(seq 1 30)"
    )
    assert '"http://${ip}:5000/v2/"' in helper
    assert "sleep 1" in helper
    assert 'docker logs --tail 20 "$REGISTRY_NAME"' in helper
    assert "within 30 seconds" in helper


def test_registry_startup_wait_tolerates_a_container_that_listens_late(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    docker.chmod(0o755)
    attempts = tmp_path / "curl-attempts"
    curl = fake_bin / "curl"
    curl.write_text(
        """#!/bin/sh
count=0
[ ! -f "$CURL_ATTEMPTS" ] || count=$(cat "$CURL_ATTEMPTS")
count=$((count + 1))
printf '%s\n' "$count" > "$CURL_ATTEMPTS"
[ "$count" -ge 3 ]
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            "-c",
            r"""
source "$1"
need_sudo() { :; }
node_ip() { printf '10.0.0.8\n'; }
sudo() {
  case "$1" in
    cat) return 1 ;;
    install) return 0 ;;
    *) "$@" ;;
  esac
}
sleep() { :; }
ensure_local_registry
""",
            "bash",
            str(SCRIPT),
        ],
        env={
            "CURL_ATTEMPTS": str(attempts),
            "HOME": str(tmp_path),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "REGISTRY_STATE": str(tmp_path / "registry"),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert attempts.read_text(encoding="utf-8").strip() == "3"


def test_runtime_preflight_proves_the_agent_image_through_cri() -> None:
    """An HTTP registry probe cannot prove that kubelet will use it."""
    source = SCRIPT.read_text(encoding="utf-8")
    helper = source[source.index("pull_agent_image_through_cri() {") :]
    runtime = source[source.index("cmd_runtime() {") :]

    assert '--runtime-endpoint "$runtime_endpoint"' in helper
    assert '--image-endpoint "$runtime_endpoint"' in helper
    assert 'pull "$AGENT_IMAGE"' in helper
    assert "--kill-after=5s 180s" in helper
    assert runtime.index("pull_agent_image_through_cri") < runtime.index(
        "assert_workspace_claim_is_durable"
    )


def test_containerd_renderer_moves_cni_and_cgroups_together(tmp_path: Path) -> None:
    """A socket-only migration leaves the node NotReady and snapshots unusable."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    containerd = fake_bin / "containerd"
    containerd.write_text(
        """#!/bin/sh
cat <<'EOF'
version = 3
        SystemdCgroup = false
      bin_dirs = ['/opt/cni/bin']
      conf_dir = '/etc/cni/net.d'
      config_path = '/etc/containerd/certs.d:/etc/docker/certs.d'
EOF
""",
        encoding="utf-8",
    )
    containerd.chmod(0o755)
    output = tmp_path / "config.toml"

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; render_external_containerd_config "$2"',
            "bash",
            str(SCRIPT),
            str(output),
        ],
        env={"HOME": str(tmp_path), "PATH": f"{fake_bin}:/usr/bin:/bin"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    rendered = output.read_text(encoding="utf-8")
    assert "SystemdCgroup = true" in rendered
    assert "bin_dirs = ['/var/lib/rancher/k3s/data/current/bin']" in rendered
    assert "conf_dir = '/var/lib/rancher/k3s/agent/etc/cni/net.d'" in rendered
    assert "config_path = '/etc/containerd/certs.d'" in rendered
    assert "/etc/docker/certs.d" not in rendered
