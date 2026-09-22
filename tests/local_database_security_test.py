from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from astrabox.deploy import onebox

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import rotate_local_database_secrets as database_rotation  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
SECRET_SCRIPT = ROOT / "scripts" / "ensure_local_database_secrets.py"
SECRET_NAMES = (
    "postgres_admin_password",
    "astrabox_password",
    "litellm_password",
    "casdoor_password",
)
GENERATED_SECRET_NAMES = (
    *SECRET_NAMES,
    "oidc_client_secret",
    "oidc_api_client_secret",
    "casdoor_admin_password",
)


def _generate_secrets(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SECRET_SCRIPT), "--directory", str(directory)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_local_database_secrets_are_random_private_and_persistent(tmp_path: Path) -> None:
    secret_dir = tmp_path / "database-secrets"

    first = _generate_secrets(secret_dir)
    assert first.returncode == 0, first.stderr
    values = {
        name: (secret_dir / name).read_text().strip()
        for name in GENERATED_SECRET_NAMES
    }

    assert len(set(values.values())) == len(GENERATED_SECRET_NAMES)
    assert all(len(value) == 64 for value in values.values())
    assert all(set(value) <= set("0123456789abcdef") for value in values.values())
    assert stat.S_IMODE(secret_dir.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE((secret_dir / name).stat().st_mode) == 0o604
        for name in GENERATED_SECRET_NAMES
    )
    assert not any(value in first.stdout or value in first.stderr for value in values.values())

    second = _generate_secrets(secret_dir)
    assert second.returncode == 0, second.stderr
    assert values == {
        name: (secret_dir / name).read_text().strip()
        for name in GENERATED_SECRET_NAMES
    }


def test_local_database_secret_generator_refuses_a_symlink(tmp_path: Path) -> None:
    secret_dir = tmp_path / "database-secrets"
    secret_dir.mkdir()
    target = tmp_path / "outside"
    target.write_text("do-not-overwrite")
    (secret_dir / "astrabox_password").symlink_to(target)

    result = _generate_secrets(secret_dir)

    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()
    assert target.read_text() == "do-not-overwrite"


def test_local_database_secret_generator_refuses_a_symlink_directory(
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "database-secrets"
    link.symlink_to(target, target_is_directory=True)

    result = _generate_secrets(link)

    assert result.returncode != 0
    assert "directory symlink" in result.stderr.lower()
    assert not any(target.iterdir())


class _RotationRunner:
    def __init__(self, *, psql_returncode: int = 0) -> None:
        self.psql_returncode = psql_returncode
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        stdin = kwargs.get("input")
        self.calls.append((list(args), stdin if isinstance(stdin, str) else None))
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(args, 0, "postgres|true\n", "")
        return subprocess.CompletedProcess(args, self.psql_returncode, "", "hidden")


def test_database_rotation_updates_all_files_and_uses_psql_stdin(tmp_path: Path) -> None:
    secret_dir = tmp_path / "database-secrets"
    assert _generate_secrets(secret_dir).returncode == 0
    old_values = {name: (secret_dir / name).read_text().strip() for name in SECRET_NAMES}
    runner = _RotationRunner()

    database_rotation.rotate_local_database_secrets(
        secret_dir,
        "astrabox-postgres-1",
        run=runner,
    )

    new_values = {name: (secret_dir / name).read_text().strip() for name in SECRET_NAMES}
    assert all(new_values[name] != old_values[name] for name in SECRET_NAMES)
    assert len(set(new_values.values())) == len(SECRET_NAMES)
    assert all(stat.S_IMODE((secret_dir / name).stat().st_mode) == 0o604 for name in SECRET_NAMES)
    assert not (secret_dir / ".rotation-pending").exists()
    psql_args, psql_stdin = runner.calls[-1]
    assert psql_args[:3] == ["docker", "exec", "-i"]
    assert psql_stdin is not None
    assert all(value in psql_stdin for value in new_values.values())
    assert all(value not in " ".join(psql_args) for value in new_values.values())


def test_database_rotation_failure_keeps_active_files_and_resumable_candidates(
    tmp_path: Path,
) -> None:
    secret_dir = tmp_path / "database-secrets"
    assert _generate_secrets(secret_dir).returncode == 0
    old_values = {name: (secret_dir / name).read_text().strip() for name in SECRET_NAMES}
    first_runner = _RotationRunner(psql_returncode=9)

    with pytest.raises(database_rotation.RotationError, match="active secret files were not changed"):
        database_rotation.rotate_local_database_secrets(
            secret_dir,
            "astrabox-postgres-1",
            run=first_runner,
        )

    assert old_values == {
        name: (secret_dir / name).read_text().strip() for name in SECRET_NAMES
    }
    pending = secret_dir / ".rotation-pending"
    pending_values = {name: (pending / name).read_text().strip() for name in SECRET_NAMES}
    second_runner = _RotationRunner()
    database_rotation.rotate_local_database_secrets(
        secret_dir,
        "astrabox-postgres-1",
        run=second_runner,
    )
    assert pending_values == {
        name: (secret_dir / name).read_text().strip() for name in SECRET_NAMES
    }
    assert first_runner.calls[-1][1] == second_runner.calls[-1][1]


def test_database_rotation_refuses_wrong_container_before_writing_candidates(
    tmp_path: Path,
) -> None:
    secret_dir = tmp_path / "database-secrets"

    def wrong_service(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "server|true\n", "")

    with pytest.raises(database_rotation.RotationError, match="not the maintained"):
        database_rotation.rotate_local_database_secrets(
            secret_dir,
            "astrabox-server-1",
            run=wrong_service,
        )

    assert not (secret_dir / ".rotation-pending").exists()


def test_database_rotation_refuses_an_active_secret_symlink(tmp_path: Path) -> None:
    secret_dir = tmp_path / "database-secrets"
    secret_dir.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("do-not-overwrite")
    (secret_dir / "astrabox_password").symlink_to(outside)

    with pytest.raises(database_rotation.RotationError, match="secret symlink"):
        database_rotation.rotate_local_database_secrets(
            secret_dir,
            "astrabox-postgres-1",
            run=_RotationRunner(),
        )

    assert outside.read_text() == "do-not-overwrite"


def test_onebox_builds_database_urls_from_service_scoped_secret_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    astrabox_password = tmp_path / "astrabox"
    litellm_password = tmp_path / "litellm"
    astrabox_password.write_text("astra/box:secret")
    litellm_password.write_text("lite@llm?secret")
    # Empty values make the launcher derive both URLs while ensuring pytest's
    # monkeypatch restores them after the launcher writes directly to os.environ.
    monkeypatch.setenv("ASTRABOX_DB_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ASTRABOX_DB_PASSWORD_FILE", str(astrabox_password))
    monkeypatch.setenv("LITELLM_DATABASE_PASSWORD_FILE", str(litellm_password))
    monkeypatch.setenv("ASTRABOX_DB_HOST", "postgres.internal")
    monkeypatch.setenv("LITELLM_DATABASE_HOST", "postgres.internal")

    onebox.ensure_database_wiring()

    assert os.environ["ASTRABOX_DB_URL"] == (
        "postgresql+asyncpg://astrabox:astra%2Fbox%3Asecret@"
        "postgres.internal:5432/astrabox"
    )
    assert os.environ["DATABASE_URL"] == (
        "postgresql://litellm:lite%40llm%3Fsecret@"
        "postgres.internal:5432/litellm"
    )


def test_explicit_database_urls_win_over_missing_secret_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_DB_URL", "postgresql://operator/astrabox")
    monkeypatch.setenv("DATABASE_URL", "postgresql://operator/litellm")
    monkeypatch.setenv("ASTRABOX_DB_PASSWORD_FILE", "/missing/astrabox")
    monkeypatch.setenv("LITELLM_DATABASE_PASSWORD_FILE", "/missing/litellm")

    onebox.ensure_database_wiring()

    assert os.environ["ASTRABOX_DB_URL"] == "postgresql://operator/astrabox"
    assert os.environ["DATABASE_URL"] == "postgresql://operator/litellm"


def test_a_configured_missing_database_secret_fails_before_process_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ASTRABOX_DB_PASSWORD_FILE", "/missing/astrabox")
    monkeypatch.delenv("LITELLM_DATABASE_PASSWORD_FILE", raising=False)

    with pytest.raises(onebox.OneBoxError, match="ASTRABOX_DB_PASSWORD_FILE"):
        onebox.ensure_database_wiring()


def test_compose_keeps_postgresql_off_the_sandbox_bridge_and_uses_file_secrets() -> None:
    compose = yaml.safe_load((ROOT / "containers" / "compose.yaml").read_text())
    services = compose["services"]
    postgres = services["postgres"]
    server = services["server"]
    sandbox_edge = services["sandbox-edge"]
    sandbox_dns_edge = services["sandbox-dns-edge"]

    assert postgres["ports"] == [
        "127.0.0.1:${ASTRABOX_POSTGRES_PORT:-55432}:5432"
    ]
    assert set(postgres["networks"]) == {"database"}
    assert set(server["networks"]) == {"platform", "database"}
    assert sandbox_edge["network_mode"] == "bridge"
    assert sandbox_edge["environment"]["ASTRABOX_EDGE_MODEL_UPSTREAM_PORT"] == (
        "${ASTRABOX_MODEL_GATEWAY_HOST_PORT:-80}"
    )
    assert "networks" not in sandbox_edge
    assert "ports" not in sandbox_edge
    assert not any("docker.sock" in mount for mount in sandbox_edge.get("volumes", []))
    assert sandbox_dns_edge["network_mode"] == "bridge"
    assert "networks" not in sandbox_dns_edge
    assert "ports" not in sandbox_dns_edge
    assert "secrets" not in sandbox_dns_edge
    assert not any(
        "docker.sock" in mount for mount in sandbox_dns_edge.get("volumes", [])
    )
    assert server["depends_on"]["sandbox-edge"]["condition"] == "service_started"
    assert (
        server["depends_on"]["sandbox-dns-edge"]["condition"]
        == "service_started"
    )
    assert server["environment"]["ASTRABOX_SANDBOX_SERVER_NETWORK_MODE"] == "bridge"
    assert server["environment"]["ASTRABOX_SANDBOX_SERVER_PORT_RANGE"] == (
        "${ASTRABOX_SANDBOX_SERVER_PORT_RANGE:-20000-32000}"
    )
    assert server["environment"]["ASTRABOX_SANDBOX_EGRESS_IMAGE"] == (
        "${ASTRABOX_SANDBOX_EGRESS_IMAGE:-opensandbox/egress:v1.1.7}"
    )
    assert server["environment"]["ASTRABOX_SANDBOX_EGRESS_MODE"] == (
        "${ASTRABOX_SANDBOX_EGRESS_MODE:-dns+nft}"
    )
    assert server["environment"]["ASTRABOX_SANDBOX_EDGE_SERVICE"] == "sandbox-edge"
    assert server["environment"]["ASTRABOX_SANDBOX_DNS_EDGE_SERVICE"] == (
        "sandbox-dns-edge"
    )
    assert server["environment"]["ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM_DEFAULT"] == ""
    assert server["environment"]["ASTRABOX_MCP_PROXY_BASE_URL"] == (
        "${ASTRABOX_MCP_PROXY_BASE_URL:-}"
    )
    assert server["environment"]["ASTRABOX_SANDBOX_GATEWAY_IP"] == (
        "${ASTRABOX_SANDBOX_GATEWAY_IP:-}"
    )
    assert server["environment"]["ASTRABOX_DB_PASSWORD_FILE"] == (
        "/run/secrets/astrabox_database_password"
    )
    assert server["environment"]["LITELLM_DATABASE_PASSWORD_FILE"] == (
        "/run/secrets/litellm_database_password"
    )
    assert server["environment"]["ASTRABOX_LITELLM_ADMIN_URL"] == (
        "${ASTRABOX_LITELLM_ADMIN_URL:-}"
    )
    assert server["environment"]["ASTRABOX_AUTH_SESSION_SECRET"] == (
        "${ASTRABOX_AUTH_SESSION_SECRET:-}"
    )
    for provider_passthrough in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        assert server["environment"][provider_passthrough] is None, (
            "an unset provider variable must be omitted from the container, not "
            "forwarded as an empty string"
        )
    assert set(server["secrets"]) == {
        "astrabox_database_password",
        "litellm_database_password",
    }
    assert (
        "${ASTRABOX_MODEL_GATEWAY_HOST_PORT:-80}:80"
        in server["ports"][2]
    )

    rendered = (ROOT / "containers" / "compose.yaml").read_text()
    for known_default in (
        "POSTGRES_ADMIN_PASSWORD:-postgres",
        "ASTRABOX_POSTGRES_PASSWORD:-astrabox",
        "LITELLM_POSTGRES_PASSWORD:-litellm",
        "CASDOOR_POSTGRES_PASSWORD:-casdoor",
        "astrabox:astrabox@",
        "litellm:litellm@",
    ):
        assert known_default not in rendered


def test_kubernetes_compose_overlay_clears_docker_only_edge_discovery() -> None:
    class ComposeLoader(yaml.SafeLoader):
        pass

    def construct_override(loader: yaml.SafeLoader, node: yaml.Node) -> object:
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
        return loader.construct_scalar(node)

    ComposeLoader.add_constructor("!override", construct_override)
    ComposeLoader.add_constructor("!reset", lambda loader, node: None)
    base = yaml.safe_load((ROOT / "containers" / "compose.yaml").read_text())
    overlay = yaml.load(
        (ROOT / "containers" / "compose.kubernetes.yaml").read_text(),
        Loader=ComposeLoader,
    )
    environment = {
        **base["services"]["server"]["environment"],
        **overlay["services"]["server"]["environment"],
    }

    assert environment["ASTRABOX_SANDBOX_SERVER_RUNTIME"] == "kubernetes"
    for required in (
        "ASTRABOX_MCP_PROXY_BASE_URL",
        "ASTRABOX_LITELLM_BASE_URL",
        "ASTRABOX_ALLOWED_HOSTS",
    ):
        assert ":?" in overlay["services"]["server"]["environment"][required]

    reset = overlay["services"]["server"]["environment"]
    for docker_only in (
        "ASTRABOX_SANDBOX_EDGE_SERVICE",
        "ASTRABOX_SANDBOX_DNS_EDGE_SERVICE",
        "ASTRABOX_SANDBOX_EDGE_CALLBACK_PORT",
        "ASTRABOX_SANDBOX_SERVER_NETWORK_MODE",
        "ASTRABOX_SANDBOX_SERVER_PORT_RANGE",
        "ASTRABOX_PUBLISH_HOST_IP",
        "ASTRABOX_SANDBOX_GATEWAY_IP",
        "ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM",
        "ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM_DEFAULT",
    ):
        assert reset[docker_only] is None

    server = overlay["services"]["server"]
    assert server["depends_on"] == {
        "postgres": {"condition": "service_healthy"},
        "redis": {"condition": "service_healthy"},
    }
    assert server["volumes"] == [
        "astrabox-state:/data",
        "${ASTRABOX_KUBECONFIG_HOST_PATH:?set a kubeconfig readable by uid 999}:/etc/astrabox/kubeconfig:ro",
    ]
    assert server["group_add"] == []
    assert server["extra_hosts"] == []
    assert ":?" in server["ports"][0]
    assert overlay["services"]["sandbox-edge"]["profiles"] == [
        "docker-runtime-only"
    ]
    assert overlay["services"]["sandbox-dns-edge"]["profiles"] == [
        "docker-runtime-only"
    ]


def test_sandbox_edge_forwards_only_model_and_capability_scoped_platform_routes() -> None:
    template = (ROOT / "containers" / "sandbox-edge" / "default.conf.template").read_text()

    assert "listen 80" in template
    assert "ASTRABOX_EDGE_MODEL_UPSTREAM_PORT" in template
    assert "location ^~ /api/v1/platform-mcp/" in template
    assert "/api/v1/mcp-proxy/" not in template
    assert "location ^~ /api/v1/sbxcap/" in template
    assert "ASTRABOX_EDGE_CALLBACK_UPSTREAM_PORT" in template
    assert "location / {\n        return 404;" in template
    assert "proxy_pass $" not in template

    dns_edge = (ROOT / "containers" / "coredns" / "sandbox-edge.Corefile").read_text()
    assert "forward . {$ASTRABOX_EDGE_DNS_UPSTREAM}" in dns_edge
    assert "hosts" not in dns_edge

    gateway_dns = (ROOT / "containers" / "coredns" / "Corefile").read_text()
    first_directive = next(
        line.strip()
        for line in gateway_dns.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    assert first_directive == ". {"
    assert not any(
        line.strip().startswith(".:53")
        for line in gateway_dns.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "gateway.astrabox.test" in gateway_dns


def test_source_overlay_mounts_code_and_bundled_gateway_adapters() -> None:
    overlay = yaml.safe_load(
        (ROOT / "containers" / "compose.source.yaml").read_text()
    )
    server = overlay["services"]["server"]

    assert server["volumes"] == [
        "../astrabox:/usr/local/lib/python3.12/site-packages/astrabox:ro",
        "../channel-gateway/src:/opt/astrabox/channel-gateway/src:ro",
        "./litellm/config.yaml:/opt/astrabox/litellm/config.yaml:ro",
        "../astrabox/identity/oidc.py:/opt/astrabox/litellm/astrabox_oidc.py:ro",
        "../astrabox/identity/session_signing.py:/opt/astrabox/litellm/astrabox_identity_session.py:ro",
        "../astrabox/providers/litellm_shared_auth.py:/opt/astrabox/litellm/astrabox_litellm_auth.py:ro",
        "./litellm/custom_auth.py:/opt/astrabox/litellm/custom_auth.py:ro",
        "./litellm/langfuse_session_hook.py:/opt/astrabox/litellm/langfuse_session_hook.py:ro",
        "./litellm/stream_normalization_hook.py:/opt/astrabox/litellm/stream_normalization_hook.py:ro",
    ]
    assert server["command"][:4] == ["python", "-m", "uvicorn", "astrabox.api.app:create_app"]
    assert "--reload" in server["command"]
    assert "networks" not in server
    assert "ports" not in server
    assert "environment" not in server


def test_source_and_live_e2e_launchers_reuse_the_maintained_compose_topology() -> None:
    dev = (ROOT / "scripts" / "dev.sh").read_text()
    browser_backend = (ROOT / "e2e" / "scripts" / "serve-backend.sh").read_text()
    smoke = (ROOT / "scripts" / "e2e_smoke.sh").read_text()
    makefile = (ROOT / "Makefile").read_text()

    for launcher in (dev, browser_backend):
        assert '"$REPO_ROOT/scripts/compose.sh"' in launcher
        assert "containers/compose.source.yaml" in launcher
        assert "ASTRABOX_LOCAL_DATABASE_SECRET_DIR" in launcher
        assert "ASTRABOX_MODEL_GATEWAY_HOST_PORT" in launcher
        assert "postgresql+asyncpg://astrabox:" not in launcher
        assert "postgresql://litellm:" not in launcher
        assert "http://172.17.0.1" not in launcher

    assert "exec bash e2e/scripts/serve-backend.sh" in smoke
    assert "postgresql+asyncpg://astrabox:" not in smoke
    assert "http://172.17.0.1" not in smoke
    assert "ASTRABOX_DEV_BACKEND_ONLY=1" in makefile
    assert "postgresql+asyncpg://astrabox:" not in makefile


def test_sso_overlay_keeps_casdoor_on_its_own_database_secret_and_loopback_port() -> None:
    overlay = yaml.safe_load((ROOT / "containers" / "compose.sso.yaml").read_text())
    casdoor = overlay["services"]["casdoor"]
    server = overlay["services"]["server"]

    assert set(casdoor["networks"]) == {"platform", "database"}
    assert casdoor["ports"] == [
        "127.0.0.1:${ASTRABOX_CASDOOR_HOST_PORT:-8087}:8000"
    ]
    assert casdoor["environment"]["CASDOOR_DATABASE_PASSWORD_FILE"] == (
        "/run/secrets/casdoor_database_password"
    )
    assert casdoor["secrets"] == [
        "casdoor_database_password",
        "oidc_client_secret",
        "oidc_api_client_secret",
        "casdoor_admin_password",
    ]
    assert casdoor["environment"]["CASDOOR_OIDC_CLIENT_SECRET_FILE"] == (
        "/run/secrets/oidc_client_secret"
    )
    assert casdoor["environment"]["CASDOOR_OIDC_API_CLIENT_SECRET_FILE"] == (
        "/run/secrets/oidc_api_client_secret"
    )
    assert casdoor["environment"]["CASDOOR_ADMIN_PASSWORD_FILE"] == (
        "/run/secrets/casdoor_admin_password"
    )
    assert casdoor["environment"]["ASTRABOX_CONSOLE_ORIGIN"] == (
        "${ASTRABOX_CONSOLE_ORIGIN:-http://127.0.0.1:${ASTRABOX_SERVER_HOST_PORT:-8088}}"
    )
    assert casdoor["environment"]["origin"] == (
        "${ASTRABOX_OIDC_ISSUER:-http://127.0.0.1:${ASTRABOX_CASDOOR_HOST_PORT:-8087}}"
    )
    assert casdoor["healthcheck"] == {
        "test": [
            "CMD",
            "curl",
            "-fsS",
            "http://127.0.0.1:8000/.well-known/openid-configuration",
        ],
        "interval": "5s",
        "timeout": "5s",
        "retries": 12,
        "start_period": "5s",
    }
    assert casdoor["tmpfs"] == [
        "/run/astrabox-casdoor:rw,noexec,nosuid,nodev,size=65536,mode=0700,uid=1000,gid=1000"
    ]
    assert server["depends_on"]["casdoor"]["condition"] == "service_healthy"
    assert server["environment"]["ASTRABOX_OIDC_ISSUER"] == (
        "${ASTRABOX_OIDC_ISSUER:-http://127.0.0.1:${ASTRABOX_CASDOOR_HOST_PORT:-8087}}"
    )
    assert server["environment"]["ASTRABOX_CASDOOR_ADMIN_URL"] == (
        "${ASTRABOX_CASDOOR_ADMIN_URL:-${ASTRABOX_OIDC_ISSUER:-"
        "http://127.0.0.1:${ASTRABOX_CASDOOR_HOST_PORT:-8087}}}"
    )
    assert server["environment"]["ASTRABOX_CASDOOR_API_ACCESS_URL"] == (
        "${ASTRABOX_CASDOOR_API_ACCESS_URL:-${ASTRABOX_OIDC_ISSUER:-"
        "http://127.0.0.1:${ASTRABOX_CASDOOR_HOST_PORT:-8087}}/applications/"
        "astrabox/astrabox-api}"
    )
    assert server["environment"]["ASTRABOX_OIDC_CLIENT_SECRET_FILE"] == (
        "/run/secrets/oidc_client_secret"
    )
    assert server["environment"]["ASTRABOX_OIDC_API_CLIENT_ID"] == (
        "${ASTRABOX_OIDC_API_CLIENT_ID:-astrabox-api}"
    )
    assert server["environment"]["ASTRABOX_OIDC_API_CLIENT_SECRET_FILE"] == (
        "/run/secrets/oidc_api_client_secret"
    )
    assert server["secrets"] == ["oidc_client_secret", "oidc_api_client_secret"]
    init_template = (ROOT / "containers" / "casdoor" / "init_data.json").read_text()
    assert "astrabox-local-secret-change-me" not in init_template
    assert '"password": "astrabox-admin"' not in init_template
    assert '"grantTypes": ["authorization_code", "password"]' in init_template
    assert '"grantTypes": ["client_credentials"]' in init_template
    assert '"expireInHours": 1' in init_template
    assert '"name": "astrabox:read"' in init_template
    assert '"name": "astrabox:write"' in init_template
    assert '"name": "astrabox:admin"' in init_template
    casdoor_config = (ROOT / "containers" / "casdoor" / "app.conf").read_text()
    assert "initDataNewOnly = true" in casdoor_config, (
        "Casdoor must seed only missing records so a container restart cannot "
        "overwrite an operator-rotated admin password or OIDC client secret"
    )
    assert 'initDataFile = "/run/astrabox-casdoor/init_data.json"' in casdoor_config
    entrypoint = (ROOT / "containers" / "casdoor" / "entrypoint.sh").read_text()
    assert 'init_data=/run/astrabox-casdoor/init_data.json' in entrypoint
    assert '>/init_data.json' not in entrypoint


def test_compose_wrapper_keeps_machine_readable_compose_stdout_clean() -> None:
    wrapper = (ROOT / "scripts" / "compose.sh").read_text()

    assert '--directory "$SECRET_DIR" >/dev/null' in wrapper
