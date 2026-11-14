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

import importlib.util
import shlex
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_INSTALLER = _REPO_ROOT / "scripts/install.sh"
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
#: What the fake ``docker info`` reports as the Docker host's memory.
_HOST_MEMORY_BYTES = 32 * 2**30


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
              "info --format") echo {_HOST_MEMORY_BYTES} ;;
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


# ── settings to review ───────────────────────────────────────────────────────


def test_a_first_install_goes_through_the_settings_that_depend_on_the_host(
    tmp_path: Path,
) -> None:
    """The installer asks about exactly the registry's review settings, in its reference file.

    A value from the environment is taken without a question, a setting the
    operator already wrote is kept, an answer that differs from the default is
    written, and Enter leaves the setting out so the server's default stands.
    """
    from astrabox.config.env_registry import SETUP_REVIEW

    env_file = tmp_path / ".env"
    reference = _REPO_ROOT / "containers/settings.reference.env"
    asked = [
        name
        for name in SETUP_REVIEW
        if name not in ("ASTRABOX_SANDBOX_QUOTA_MEMORY", "ASTRABOX_SANDBOX_LEASE_SECONDS")
    ]
    # Yes to going through them, then one answer per question: a value for the
    # box's CPU, Enter for every other.
    answers = ["y"] + ["8" if name == "ASTRABOX_SANDBOX_CPU_LIMIT" else "" for name in asked]
    (tmp_path / "answers").write_text("".join(f"{answer}\n" for answer in answers), encoding="utf-8")
    result = _run(
        f"""
        create_env_file "{env_file}"
        env_set "{env_file}" ASTRABOX_SANDBOX_LEASE_SECONDS 86400
        export ASTRABOX_SANDBOX_QUOTA_MEMORY=24Gi
        exec 3<"{tmp_path / 'answers'}"
        have_tty=1
        configure_review_settings "{env_file}" "{reference}"
        IFS= read -r unread <&3 && echo "unread answer: $unread"
        have_tty=1
        configure_review_settings "{env_file}" "{reference}"
        show_review_settings "{env_file}" "{reference}"
        """,
        tmp_path=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "unread answer" not in result.stdout

    assert _settings(env_file) == {
        "ASTRABOX_SANDBOX_LEASE_SECONDS": "86400",
        "ASTRABOX_SANDBOX_QUOTA_MEMORY": "24Gi",
        "ASTRABOX_SANDBOX_CPU_LIMIT": "8",
        "ASTRABOX_INSTALL_SETTINGS_REVIEWED": "1",
    }
    # The questions name each remaining setting once; the second run asks none.
    for name in asked:
        assert result.stderr.count(f"{name} [") == 1, result.stderr
    for name in SETUP_REVIEW:
        assert name in result.stdout, result.stdout


def test_a_first_install_decides_how_much_memory_sandboxes_hold(tmp_path: Path) -> None:
    """A first install does not leave the sandboxes' memory to the server's default.

    That default is the Docker host's memory less 4 GiB, and the host's memory
    says nothing about what else runs on it. Without a terminal the installer
    stops and names the setting; from a terminal it shows the host's memory,
    offers that default, refuses a size without its unit, and writes the
    answer. A later run asks nothing. An unattended upgrade of an installation
    that never decided is not stopped: its server warns instead.
    """
    env_file = tmp_path / ".env"
    reference = _REPO_ROOT / "containers/settings.reference.env"
    upgraded = _run(
        f"""
        create_env_file "{env_file}"
        have_tty=0
        previous_version=0.1.14
        configure_review_settings "{env_file}" "{reference}"
        """,
        tmp_path=tmp_path,
    )
    assert upgraded.returncode == 0, upgraded.stderr
    assert "ASTRABOX_SANDBOX_QUOTA_MEMORY" not in _settings(env_file)
    env_file.unlink()

    refused = _run(
        f"""
        create_env_file "{env_file}"
        have_tty=0
        configure_review_settings "{env_file}" "{reference}"
        """,
        tmp_path=tmp_path,
    )
    assert refused.returncode != 0
    assert "ASTRABOX_SANDBOX_QUOTA_MEMORY=24Gi" in refused.stderr, refused.stderr
    assert "ASTRABOX_SANDBOX_QUOTA_MEMORY" not in _settings(env_file)
    assert "ASTRABOX_INSTALL_SETTINGS_REVIEWED" not in _settings(env_file)

    # A bare number, which the server would read as bytes, then 20Gi; then no
    # to going through the other settings.
    (tmp_path / "answers").write_text("20\n20Gi\nn\n", encoding="utf-8")
    asked = _run(
        f"""
        exec 3<"{tmp_path / 'answers'}"
        have_tty=1
        configure_review_settings "{env_file}" "{reference}"
        configure_review_settings "{env_file}" "{reference}"
        """,
        tmp_path=tmp_path,
    )
    assert asked.returncode == 0, asked.stderr
    assert asked.stderr.count("This Docker host has 32 GiB of memory") == 1, asked.stderr
    assert asked.stderr.count("ASTRABOX_SANDBOX_QUOTA_MEMORY [28Gi]") == 2, asked.stderr
    assert asked.stderr.count("Enter a size with its unit") == 1, asked.stderr
    assert _settings(env_file)["ASTRABOX_SANDBOX_QUOTA_MEMORY"] == "20Gi"


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
