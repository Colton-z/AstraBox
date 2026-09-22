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
import os
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
