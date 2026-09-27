"""The installer's file-writing logic, which a user's first run depends on.

The installer generates the same deployment secrets a checkout generates with
``scripts/ensure_local_database_secrets.py``, without needing Python on the
host, writes the model settings the bundled gateway reads, and keeps an
installation's settings across upgrades. Each of these is read back by another
program — PostgreSQL through Compose secrets, the deployment's entry point and
LiteLLM through the settings file — so the checks run those readers' rules
rather than comparing the installer's output with a copy of itself.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from astrabox.deploy import onebox

_REPO_ROOT = Path(__file__).resolve().parents[1]
_INSTALLER = _REPO_ROOT / "scripts/install.sh"
_GATEWAY_CONFIG = _REPO_ROOT / "containers/litellm/config.yaml"
_SECRET_NAMES = (
    "postgres_admin_password",
    "astrabox_password",
    "litellm_password",
    "casdoor_password",
    "oidc_client_secret",
    "oidc_api_client_secret",
    "casdoor_admin_password",
    "casdoor_builtin_admin_password",
)
#: Every name the installer or the deployment entry point may set for a model
#: service; each case starts from none of them.
_MODEL_ENVIRONMENT = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_BASE_URL",
)


def _fake_docker(directory: Path, *, volume_exists: bool) -> Path:
    """A ``docker`` the installer can call without a daemon."""
    binaries = directory / "bin"
    binaries.mkdir(parents=True, exist_ok=True)
    script = binaries / "docker"
    script.write_text(
        textwrap.dedent(
            f"""
            #!/usr/bin/env bash
            case "$1 $2" in
              "volume inspect") exit {0 if volume_exists else 1} ;;
            esac
            exit 0
            """
        ).lstrip(),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return binaries


def _run(
    snippet: str,
    *,
    tmp_path: Path,
    volume_exists: bool = False,
) -> subprocess.CompletedProcess[str]:
    binaries = _fake_docker(tmp_path, volume_exists=volume_exists)
    return subprocess.run(
        ["bash", "-c", f'source "{_INSTALLER}"\n{textwrap.dedent(snippet)}'],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": f"{binaries}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def _load_checkout_generator():
    specification = importlib.util.spec_from_file_location(
        "ensure_local_database_secrets",
        _REPO_ROOT / "scripts/ensure_local_database_secrets.py",
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _settings(env_file: Path) -> dict[str, str]:
    """The settings file as Compose reads it: single quotes are not part of a value."""
    written: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            written[key] = value.removeprefix("'").removesuffix("'")
    return written


def test_generated_secrets_are_the_ones_a_checkout_generates(tmp_path: Path) -> None:
    secrets = tmp_path / "database-secrets"
    result = _run(f'ensure_secrets "{secrets}" astrabox-postgres', tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr

    generator = _load_checkout_generator()
    assert sorted(path.name for path in secrets.iterdir()) == sorted(_SECRET_NAMES)
    for name in _SECRET_NAMES:
        # The checkout's reader validates format and refuses anything else, so
        # accepting these files proves the two generators agree.
        assert generator._existing_secret(secrets / name)
        assert stat.S_IMODE((secrets / name).stat().st_mode) == 0o604
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o700

    kept = {name: (secrets / name).read_text(encoding="ascii") for name in _SECRET_NAMES}
    assert _run(f'ensure_secrets "{secrets}" astrabox-postgres', tmp_path=tmp_path).returncode == 0
    assert {
        name: (secrets / name).read_text(encoding="ascii") for name in _SECRET_NAMES
    } == kept, "an upgrade must not rotate the database passwords behind PostgreSQL"


def test_missing_secrets_beside_an_existing_database_stop_the_installation(
    tmp_path: Path,
) -> None:
    result = _run(
        f'ensure_secrets "{tmp_path}/database-secrets" astrabox-postgres',
        tmp_path=tmp_path,
        volume_exists=True,
    )

    assert result.returncode != 0
    assert "PostgreSQL volume" in result.stderr
    assert not (tmp_path / "database-secrets").exists(), (
        "a refused installation must not leave passwords that do not open the database"
    )


def test_the_settings_file_keeps_values_verbatim_and_lines_it_did_not_write(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    result = _run(
        f"""
        create_env_file "{env_file}"
        printf "%s\\n" "MY_OWN_SETTING='keep me'" >> "{env_file}"
        env_set "{env_file}" ANTHROPIC_API_KEY 'sk-a$b#c'
        env_set "{env_file}" ASTRABOX_IMAGE_TAG 0.1.0
        env_set "{env_file}" ASTRABOX_IMAGE_TAG 0.2.0
        env_get "{env_file}" ANTHROPIC_API_KEY
        """,
        tmp_path=tmp_path,
    )
    assert result.returncode == 0, result.stderr

    content = env_file.read_text(encoding="utf-8")
    assert result.stdout == "sk-a$b#c"
    assert "MY_OWN_SETTING='keep me'" in content
    assert content.count("ASTRABOX_IMAGE_TAG=") == 1
    assert "ASTRABOX_IMAGE_TAG='0.2.0'" in content
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def _gateway_route(model: str) -> tuple[dict[str, str], str]:
    """The bundled route LiteLLM serves ``model`` from, and the upstream model id.

    LiteLLM serves an exact ``model_name`` before a wildcard pattern, and a
    pattern's ``*`` carries the matched text into ``litellm_params.model``.
    """
    routes = yaml.safe_load(_GATEWAY_CONFIG.read_text(encoding="utf-8"))["model_list"]
    exact = [route for route in routes if route["model_name"] == model]
    if exact:
        return exact[0]["litellm_params"], exact[0]["litellm_params"]["model"]
    matches = [
        route
        for route in routes
        if "*" in route["model_name"] and fnmatch.fnmatchcase(model, route["model_name"])
    ]
    assert len(matches) == 1, f"{model!r} matches {len(matches)} bundled routes"
    prefix, suffix = matches[0]["model_name"].split("*", 1)
    matched = model[len(prefix) : len(model) - len(suffix)]
    params = matches[0]["litellm_params"]
    return params, params["model"].replace("*", matched)


def _resolved(value: str | None) -> str | None:
    """A route value as LiteLLM reads it: ``os.environ/NAME`` names a variable."""
    if value is not None and value.startswith("os.environ/"):
        return os.environ.get(value.removeprefix("os.environ/")) or None
    return value


@pytest.mark.parametrize(
    ("provider", "model", "base_url", "upstream_base", "upstream_model"),
    [
        ("anthropic", "claude-sonnet-4-5", "", None, "anthropic/claude-sonnet-4-5"),
        # The offered default. The route is pinned for both of DeepSeek's wires;
        # `GET https://api.deepseek.com/v1/models` lists the ids DeepSeek serves.
        (
            "deepseek",
            "deepseek-flash",
            "",
            "https://api.deepseek.com/anthropic",
            "anthropic/deepseek-flash",
        ),
        (
            "anthropic-compatible",
            "glm-5",
            "https://models.example.com/anthropic",
            "https://models.example.com/anthropic",
            "anthropic/glm-5",
        ),
        (
            "openai-compatible",
            "qwen3-max",
            "https://models.example.com/v1",
            "https://models.example.com/v1",
            "openai/qwen3-max",
        ),
    ],
)
def test_each_model_service_is_reached_with_the_key_url_and_model_it_was_given(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model: str,
    base_url: str,
    upstream_base: str | None,
    upstream_model: str,
) -> None:
    """Follow the written settings through the entry point to the gateway route.

    The deployment's entry point rewrites the default model before LiteLLM
    starts (``onebox.ensure_litellm_provider_wiring``), and the gateway picks a
    route by that name. A setting written under a name the route does not read,
    or a model name no route serves, gives an installation that starts and then
    fails its first Agent turn.
    """
    env_file = tmp_path / ".env"
    result = _run(
        f"""
        create_env_file "{env_file}"
        provider={provider}
        model_key=sk-installed-key
        model_name={model}
        model_base_url={base_url}
        validate_model_settings
        write_model_settings "{env_file}"
        """,
        tmp_path=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    written = _settings(env_file)
    assert written.pop("ASTRABOX_INSTALL_MODEL_PROVIDER") == provider

    for name in _MODEL_ENVIRONMENT:
        # Set before deleting, so the undo also removes what the entry point
        # writes into os.environ below.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    for name, value in written.items():
        monkeypatch.setenv(name, value)
    onebox.ensure_litellm_provider_wiring()

    route, routed_model = _gateway_route(os.environ["ANTHROPIC_MODEL"])
    assert routed_model == upstream_model
    assert _resolved(route.get("api_base")) == upstream_base
    assert _resolved(route["api_key"]) == "sk-installed-key"


def test_choosing_another_service_removes_the_previous_services_settings(
    tmp_path: Path,
) -> None:
    """A leftover base URL would send the new key to the old service's host."""
    env_file = tmp_path / ".env"
    result = _run(
        f"""
        create_env_file "{env_file}"
        printf "%s\\n" "DEEPSEEK_API_KEY='set by the operator'" >> "{env_file}"
        provider=anthropic-compatible model_key=sk-first model_name=glm-5
        model_base_url=https://first.example.com
        write_model_settings "{env_file}"
        provider=openai-compatible model_key=sk-second model_name=qwen3-max
        model_base_url=https://second.example.com/v1
        write_model_settings "{env_file}"
        """,
        tmp_path=tmp_path,
    )
    assert result.returncode == 0, result.stderr

    assert _settings(env_file) == {
        "DEEPSEEK_API_KEY": "set by the operator",
        "ASTRABOX_INSTALL_MODEL_PROVIDER": "openai-compatible",
        "OPENAI_COMPATIBLE_BASE_URL": "https://second.example.com/v1",
        "OPENAI_COMPATIBLE_API_KEY": "sk-second",
        "ANTHROPIC_MODEL": "openai-compatible/qwen3-max",
    }


def test_a_model_service_without_a_key_is_refused_before_the_stack_starts(
    tmp_path: Path,
) -> None:
    result = _run(
        """
        provider=anthropic
        model_key=
        model_name=claude-sonnet-4-5
        validate_model_settings
        """,
        tmp_path=tmp_path,
    )

    assert result.returncode != 0
    assert "needs an API key" in result.stderr


def test_a_server_that_cannot_start_ends_the_wait_with_its_log(tmp_path: Path) -> None:
    """A crash-looping server answers no HTTP request, so the poll must read it.

    Without this the installer waits out its whole readiness budget while the
    container prints the reason it exited on every restart.
    """
    result = _run(
        """
        compose() {
          case "$1" in
            ps) printf 'container-id\n' ;;
            logs) printf 'FATAL: the port range overlaps the ephemeral range\n' ;;
          esac
        }
        docker() { printf 'restarting\n'; }
        http_status() { printf '000'; }
        install_dir=/nonexistent
        wait_until_ready http://127.0.0.1:1
        """,
        tmp_path=tmp_path,
    )

    assert result.returncode != 0
    assert "server container is restarting" in result.stderr
    assert "FATAL: the port range overlaps the ephemeral range" in result.stderr


# ── Docker versions ──────────────────────────────────────────────────────────


def _check_versions(
    tmp_path: Path, *, compose: str, engine: str, api: str
) -> subprocess.CompletedProcess[str]:
    """``check_docker_versions`` against a daemon and plugin reporting these versions."""
    return _run(
        f"""
        docker() {{
          case "$*" in
            "compose version --short") printf '%s\\n' '{compose}' ;;
            "version --format {{{{.Server.Version}}}}") printf '%s\\n' '{engine}' ;;
            "version --format {{{{.Server.APIVersion}}}}") printf '%s\\n' '{api}' ;;
            *) return 1 ;;
          esac
        }}
        check_docker_versions
        """,
        tmp_path=tmp_path,
    )


@pytest.mark.parametrize(
    ("engine", "api"), [("25.0.5", "1.44"), ("24.0.9", "1.43"), ("20.10.24", "1.41")]
)
def test_a_docker_engine_older_than_the_minimum_is_refused(
    tmp_path: Path, engine: str, api: str
) -> None:
    result = _check_versions(tmp_path, compose="2.29.7", engine=engine, api=api)

    assert result.returncode != 0
    assert f"found Docker Engine {engine} (API {api})" in result.stderr
    assert "Docker Engine 26.0 (API 1.45) or later is required" in result.stderr


@pytest.mark.parametrize(
    ("compose", "engine", "api"),
    [
        ("2.17.0", "26.0.0", "1.45"),
        ("v2.40.3", "29.8.1", "1.52"),
        ("2.29.7-desktop.1", "27.3.1", "1.47"),
    ],
)
def test_the_minimum_versions_and_later_are_accepted(
    tmp_path: Path, compose: str, engine: str, api: str
) -> None:
    result = _check_versions(tmp_path, compose=compose, engine=engine, api=api)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("compose", ["2.16.0", "2.9.0", "1.29.2"])
def test_a_compose_plugin_older_than_the_minimum_is_refused(tmp_path: Path, compose: str) -> None:
    # 2.9.0 sorts after 2.17.0 as text; the parts are numbers.
    result = _check_versions(tmp_path, compose=compose, engine="29.8.1", api="1.52")

    assert result.returncode != 0
    assert f"Docker Compose 2.17.0 or later is required; found {compose}" in result.stderr


# ── team login ───────────────────────────────────────────────────────────────


def _installation(tmp_path: Path) -> Path:
    """An installed bundle's directory, holding the overlay team login runs."""
    install_dir = tmp_path / "astrabox"
    (install_dir / "containers").mkdir(parents=True)
    (install_dir / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    shutil.copy(
        _REPO_ROOT / "containers/compose.sso.yaml",
        install_dir / "containers/compose.sso.yaml",
    )
    return install_dir


def _configure_team_login(
    install_dir: Path, tmp_path: Path, settings: str = "", answers: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the installer's team-login step as ``main`` does.

    ``answers`` stands for a terminal: the installer reads its questions'
    answers on descriptor 3. Without it the run has no terminal.
    """
    terminal = (
        f"have_tty=1; exec 3< <(printf '%s' {shlex.quote(answers)})"
        if answers is not None
        else "have_tty=0"
    )
    return _run(
        f"""
        install_dir="{install_dir}"
        {terminal}
        {settings}
        create_env_file "{install_dir}/containers/.env"
        configure_team_login "{install_dir}/containers/.env"
        """,
        tmp_path=tmp_path,
    )


_PUBLIC_URLS = """
export ASTRABOX_INSTALL_TEAM_LOGIN=casdoor
export ASTRABOX_CONSOLE_ORIGIN=https://astrabox.example.com
export ASTRABOX_OIDC_ISSUER=https://login.example.com
"""


def test_team_login_runs_the_overlay_with_every_later_compose_command(
    tmp_path: Path,
) -> None:
    """Compose reads COMPOSE_FILE from the settings file in containers/.

    The installer starts the stack with it, and so does every `docker compose`
    command an operator later runs there; a login that only the installer's
    own start included would disappear on the operator's next `up`.
    """
    install_dir = _installation(tmp_path)
    result = _configure_team_login(
        install_dir, tmp_path, "export ASTRABOX_INSTALL_TEAM_LOGIN=casdoor"
    )
    assert result.returncode == 0, result.stderr

    written = _settings(install_dir / "containers/.env")
    compose_files = written["COMPOSE_FILE"].split(":")
    assert compose_files == ["compose.yaml", "compose.sso.yaml"]
    # On this host only, the overlay's loopback defaults are the addresses.
    for name in (
        "ASTRABOX_CONSOLE_ORIGIN",
        "ASTRABOX_OIDC_ISSUER",
        "ASTRABOX_OIDC_REDIRECT_URL",
        "ASTRABOX_ALLOWED_HOSTS",
    ):
        assert name not in written


def test_public_urls_reach_the_server_and_casdoor_as_one_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behind a TLS proxy the server sees plain HTTP from the proxy.

    Login then works only when the server accepts the public Host, names to
    Casdoor the very callback Casdoor registered for the console, and marks
    its cookies Secure. Each is checked with the reader's own rule.
    """
    install_dir = _installation(tmp_path)
    result = _configure_team_login(install_dir, tmp_path, _PUBLIC_URLS)
    assert result.returncode == 0, result.stderr
    written = _settings(install_dir / "containers/.env")
    assert written["ASTRABOX_CONSOLE_ORIGIN"] == "https://astrabox.example.com"
    assert written["ASTRABOX_OIDC_ISSUER"] == "https://login.example.com"

    for name, value in written.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ASTRABOX_OIDC_CLIENT_ID", "astrabox-console")
    monkeypatch.setenv("ASTRABOX_OIDC_CLIENT_SECRET", "0" * 64)

    from starlette.requests import Request

    from astrabox.api.routes.auth import _cookie_secure, _redirect_uri
    from astrabox.identity.oidc import OidcProviderConfig
    from astrabox.web.trusted_host_middleware import _allowed_hosts, _host_from_scope

    def arriving(host: str) -> dict[str, object]:
        return {
            "type": "http",
            "scheme": "http",
            "method": "GET",
            "path": "/api/v1/auth/login",
            "root_path": "",
            "query_string": b"",
            "server": ("172.18.0.2", 8000),
            "headers": [(b"host", host.encode())],
        }

    for host in ("astrabox.example.com", "127.0.0.1:8088"):
        assert _host_from_scope(arriving(host)) in _allowed_hosts(), host

    config = OidcProviderConfig.load()
    request = Request(arriving("astrabox.example.com"))
    template = (_REPO_ROOT / "containers/casdoor/init_data.json").read_text(encoding="utf-8")
    seeded = json.loads(
        template.replace("__ASTRABOX_CONSOLE_ORIGIN__", written["ASTRABOX_CONSOLE_ORIGIN"])
    )
    registered = next(
        application["redirectUris"]
        for application in seeded["applications"]
        if application["name"] == "astrabox-console"
    )
    assert _redirect_uri(request, config) in registered
    assert _cookie_secure(request, config)
    assert config.issuer == "https://login.example.com"


def test_re_running_without_settings_keeps_team_login_and_its_urls(
    tmp_path: Path,
) -> None:
    install_dir = _installation(tmp_path)
    assert _configure_team_login(install_dir, tmp_path, _PUBLIC_URLS).returncode == 0
    first = _settings(install_dir / "containers/.env")
    assert first["COMPOSE_FILE"] == "compose.yaml:compose.sso.yaml"

    result = _configure_team_login(install_dir, tmp_path)

    assert result.returncode == 0, result.stderr
    assert _settings(install_dir / "containers/.env") == first


@pytest.mark.parametrize(
    ("current", "expected"),
    [("none", None), ("casdoor", "compose.yaml:compose.sso.yaml")],
)
def test_the_question_defaults_to_the_current_choice(
    tmp_path: Path, current: str, expected: str | None
) -> None:
    """Pressing Enter never changes whether a deployment has a login."""
    install_dir = _installation(tmp_path)
    if current == "casdoor":
        setup = _configure_team_login(
            install_dir, tmp_path, "export ASTRABOX_INSTALL_TEAM_LOGIN=casdoor"
        )
        assert setup.returncode == 0, setup.stderr

    result = _configure_team_login(install_dir, tmp_path, answers="\n")

    assert result.returncode == 0, result.stderr
    assert _settings(install_dir / "containers/.env").get("COMPOSE_FILE") == expected


def test_turning_team_login_off_is_refused_while_other_computers_reach_it(
    tmp_path: Path,
) -> None:
    install_dir = _installation(tmp_path)
    assert _configure_team_login(install_dir, tmp_path, _PUBLIC_URLS).returncode == 0

    result = _configure_team_login(
        install_dir, tmp_path, "export ASTRABOX_INSTALL_TEAM_LOGIN=none"
    )

    assert result.returncode != 0
    assert "without login" in result.stderr
    assert "COMPOSE_FILE" in _settings(install_dir / "containers/.env")


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        (
            "export ASTRABOX_INSTALL_TEAM_LOGIN=casdoor "
            "ASTRABOX_CONSOLE_ORIGIN=https://astrabox.example.com",
            "needs both public URLs",
        ),
        (
            "export ASTRABOX_INSTALL_TEAM_LOGIN=casdoor "
            "ASTRABOX_CONSOLE_ORIGIN=https://astrabox.example.com/ "
            "ASTRABOX_OIDC_ISSUER=https://login.example.com",
            "no path or trailing slash",
        ),
        (
            "export ASTRABOX_INSTALL_TEAM_LOGIN=none "
            "ASTRABOX_OIDC_ISSUER=https://login.example.com",
            "takes effect only with team login",
        ),
        ("export ASTRABOX_INSTALL_TEAM_LOGIN=yes", "Use casdoor or none"),
    ],
)
def test_team_login_settings_that_cannot_log_anyone_in_are_refused(
    tmp_path: Path, settings: str, reason: str
) -> None:
    install_dir = _installation(tmp_path)

    result = _configure_team_login(install_dir, tmp_path, settings)

    assert result.returncode != 0
    assert reason in result.stderr
    assert "COMPOSE_FILE" not in _settings(install_dir / "containers/.env")


def test_an_allowed_hosts_list_without_the_console_host_is_refused(
    tmp_path: Path,
) -> None:
    """The operator's own list is kept, not replaced, and must admit the console."""
    install_dir = _installation(tmp_path)
    env_file = install_dir / "containers/.env"
    create = _run(f'create_env_file "{env_file}"', tmp_path=tmp_path)
    assert create.returncode == 0, create.stderr
    with env_file.open("a", encoding="utf-8") as settings:
        settings.write("ASTRABOX_ALLOWED_HOSTS='intranet.example.com,127.0.0.1'\n")

    refused = _configure_team_login(install_dir, tmp_path, _PUBLIC_URLS)
    assert refused.returncode != 0
    assert "must list astrabox.example.com" in refused.stderr

    with env_file.open("a", encoding="utf-8") as settings:
        settings.write(
            "ASTRABOX_ALLOWED_HOSTS='intranet.example.com,astrabox.example.com,127.0.0.1'\n"
        )
    kept = _configure_team_login(install_dir, tmp_path, _PUBLIC_URLS)
    assert kept.returncode == 0, kept.stderr
    assert _settings(env_file)["ASTRABOX_ALLOWED_HOSTS"] == (
        "intranet.example.com,astrabox.example.com,127.0.0.1"
    )


def test_a_release_without_the_overlay_cannot_turn_team_login_on(
    tmp_path: Path,
) -> None:
    """The installer is served from main; the bundle may be an older release's."""
    install_dir = _installation(tmp_path)
    (install_dir / "containers/compose.sso.yaml").unlink()

    result = _configure_team_login(
        install_dir, tmp_path, "export ASTRABOX_INSTALL_TEAM_LOGIN=casdoor"
    )

    assert result.returncode != 0
    assert "AstraBox 0.1.0 bundle has no team-login overlay" in result.stderr
