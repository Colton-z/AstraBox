"""The Kubernetes testbed renders the live fixtures its credential E2E lane uses."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "k8s-testbed.sh"

_RENDER_HARNESS = r"""
set -uo pipefail
. "$SCRIPT"
validate_e2e_https_gateway_url
render_e2e_fixture_manifests
"""

_INSTALL_HARNESS = r"""
set -uo pipefail
. "$SCRIPT"
kubectl() {
  printf 'kubectl %s\n' "$*" >> "$CALLS"
  case " $* " in
    *" apply -f - "*) cat >/dev/null ;;
  esac
}
curl() {
  printf 'curl %s\n' "$*" >> "$CALLS"
  case "$(grep -c '^curl ' "$CALLS")" in
    1) printf '502' ;;
    *) printf '401' ;;
  esac
}
sleep() { printf 'sleep %s\n' "$*" >> "$CALLS"; }
cmd_fixtures
"""


def _render(tmp_path: Path, *, gateway_url: str = "") -> list[dict]:
    if shutil.which("bash") is None:  # pragma: no cover - bash is a hard dep here
        pytest.skip("bash is required to render the testbed fixtures")
    done = subprocess.run(
        ["bash", "-c", _RENDER_HARNESS],
        cwd=REPO,
        env={
            "PATH": "/usr/bin:/bin",
            "KUBECONFIG_PATH": str(tmp_path / "kubeconfig"),
            "SCRIPT": str(SCRIPT),
            "ASTRABOX_LITELLM_BASE_URL": "https://model-gateway.test",
            "ASTRABOX_MCP_PROXY_BASE_URL": "https://callback.test",
            "ASTRABOX_E2E_HTTPS_MODEL_GATEWAY_URL": gateway_url,
        },
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, f"rendering the E2E fixtures failed:\n{done.stderr}"
    return [document for document in yaml.safe_load_all(done.stdout) if document]


def _resource(documents: list[dict], kind: str, name: str) -> dict:
    matches = [
        document
        for document in documents
        if document["kind"] == kind and document["metadata"]["name"] == name
    ]
    assert len(matches) == 1, f"expected one {kind}/{name}, got {len(matches)}"
    return matches[0]


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.lstrip(), encoding="utf-8")
    path.chmod(0o755)


def test_probe_manifest_enforces_the_request_matching_contract(tmp_path: Path) -> None:
    """Removing the probe unit leaves the spec with no credential oracle and fails here."""
    documents = _render(tmp_path)
    name = "astrabox-e2e-credential-request-probe"
    deployment = _resource(documents, "Deployment", name)
    service = _resource(documents, "Service", name)

    container = deployment["spec"]["template"]["spec"]["containers"][0]
    environment = {item["name"]: item["value"] for item in container["env"]}
    program = container["args"][0]
    assert environment["EXPECTED_AUTHORIZATION"] == (
        "Bearer astrabox-e2e-request-match-secret"
    )
    assert 'self.command == "GET"' in program
    assert 'self.path == "/allowed"' in program
    assert "self.send_response(204)" in program
    assert "self.send_response(401)" in program
    assert "do_POST = _reply" in program
    assert service["spec"]["ports"] == [
        {"name": "http", "port": 80, "targetPort": "http"}
    ]


def test_https_gateway_terminates_real_tls_on_a_443_service(tmp_path: Path) -> None:
    documents = _render(tmp_path, gateway_url="https://models.team.example:443")
    name = "astrabox-e2e-https-model-gateway"
    config = _resource(documents, "ConfigMap", name)
    deployment = _resource(documents, "Deployment", name)
    service = _resource(documents, "Service", name)

    caddyfile = config["data"]["Caddyfile"]
    assert "auto_https disable_redirects" in caddyfile
    assert "reverse_proxy {$ASTRABOX_E2E_GATEWAY_UPSTREAM}" in caddyfile
    assert "keepalive 4s" in caddyfile
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    environment = {item["name"]: item["value"] for item in container["env"]}
    assert environment == {
        "ASTRABOX_E2E_GATEWAY_DOMAIN": "models.team.example",
        "ASTRABOX_E2E_GATEWAY_UPSTREAM": (
            "http://astrabox-model-gw.opensandbox.svc.cluster.local:80"
        ),
    }
    assert container["ports"] == [{"name": "https", "containerPort": 443}]
    assert service["spec"]["type"] == "LoadBalancer"
    assert service["spec"]["ports"] == [
        {"name": "https", "port": 443, "targetPort": "https"}
    ]


def test_install_waits_for_publicly_trusted_tls_after_gateway_rollout(
    tmp_path: Path,
) -> None:
    """Removing the post-rollout TLS wait makes this real install path fail."""
    calls_path = tmp_path / "calls"
    done = subprocess.run(
        ["bash", "-c", _INSTALL_HARNESS],
        cwd=REPO,
        env={
            "PATH": "/usr/bin:/bin",
            "KUBECONFIG_PATH": str(tmp_path / "kubeconfig"),
            "SCRIPT": str(SCRIPT),
            "CALLS": str(calls_path),
            "ASTRABOX_LITELLM_BASE_URL": "https://model-gateway.test",
            "ASTRABOX_MCP_PROXY_BASE_URL": "https://callback.test",
            "ASTRABOX_E2E_HTTPS_MODEL_GATEWAY_URL": "https://models.team.example",
        },
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, f"installing the E2E fixtures failed:\n{done.stderr}"
    calls = calls_path.read_text().splitlines()
    gateway_rollout = next(
        index
        for index, call in enumerate(calls)
        if "rollout status deployment/astrabox-e2e-https-model-gateway" in call
    )
    curl_calls = [
        (index, call)
        for index, call in enumerate(calls)
        if call.startswith("curl ")
    ]
    assert len(curl_calls) == 2
    assert gateway_rollout < curl_calls[0][0]
    assert calls[curl_calls[0][0] + 1] == "sleep 2"
    assert curl_calls[0][1].endswith(
        "https://models.team.example/v1/models"
    )
    assert "--write-out %{http_code}" in curl_calls[0][1]
    assert "--insecure" not in curl_calls[0][1]
    assert " -k " not in f" {curl_calls[0][1]} "


def test_fixtures_subcommand_owns_genesis_and_update_through_apply(
    tmp_path: Path,
) -> None:
    calls_path = tmp_path / "calls"
    fake_bin = tmp_path / "bin"
    _write_executable(
        fake_bin / "kubectl",
        """
        #!/usr/bin/env bash
        printf 'kubectl %s\n' "$*" >> "$CALLS"
        case " $* " in
          *" apply -f - "*) cat >/dev/null ;;
        esac
        """,
    )
    _write_executable(
        fake_bin / "curl",
        """
        #!/usr/bin/env bash
        printf 'curl %s\n' "$*" >> "$CALLS"
        printf '401'
        """,
    )
    done = subprocess.run(
        [str(SCRIPT), "fixtures"],
        cwd=REPO,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "CALLS": str(calls_path),
            "KUBECONFIG_PATH": str(tmp_path / "kubeconfig"),
            "ASTRABOX_E2E_HTTPS_MODEL_GATEWAY_URL": "https://models.team.example",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    calls = calls_path.read_text(encoding="utf-8").splitlines()
    assert any(" apply -f -" in call for call in calls)
    assert any(
        "rollout status deployment/astrabox-e2e-https-model-gateway" in call for call in calls
    )


@pytest.mark.parametrize(
    "gateway_url",
    ["http://models.team.example", "https://10.0.0.1", "https://models.team.example:8443"],
)
def test_https_gateway_refuses_shapes_the_credential_spec_cannot_use(
    tmp_path: Path, gateway_url: str
) -> None:
    done = subprocess.run(
        ["bash", "-c", _RENDER_HARNESS],
        cwd=REPO,
        env={
            "PATH": "/usr/bin:/bin",
            "KUBECONFIG_PATH": str(tmp_path / "kubeconfig"),
            "SCRIPT": str(SCRIPT),
            "ASTRABOX_LITELLM_BASE_URL": "https://model-gateway.test",
            "ASTRABOX_MCP_PROXY_BASE_URL": "https://callback.test",
            "ASTRABOX_E2E_HTTPS_MODEL_GATEWAY_URL": gateway_url,
        },
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "HTTPS FQDN on port 443" in done.stderr or "not an IP address" in done.stderr
