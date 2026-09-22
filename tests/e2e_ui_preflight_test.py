"""The browser round's preflight, fed the failures it exists to catch.

A preflight that only ever runs against a healthy machine proves the machine
is healthy, not that the check can see anything. Each case below hands one
check the exact broken state it guards — no Playwright install, a missing
sign-in secret, a console that answers wrong, a database container that is not
there — and reads the code it raises. The last case runs the whole preflight
against a machine where nothing is wrong, so the reds above are evidence about
the state and not about the check refusing everything.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PREFLIGHT = _REPO_ROOT / "tests" / "e2e-ui" / "preflight.mjs"


def _run(script: str, env: dict[str, str]) -> dict[str, object]:
    """Run one preflight call in node and return what it reported."""

    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=_PREFLIGHT.parent,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=env,
    )
    if completed.returncode != 0:
        pytest.fail(
            f"preflight harness crashed: {completed.stdout}{completed.stderr}",
            pytrace=False,
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


_REPORT = textwrap.dedent(
    """\
    const report = async (fn) => {
      try {
        await fn();
        console.log(JSON.stringify({ ok: true }));
      } catch (error) {
        console.log(JSON.stringify({
          ok: false,
          code: error.code ?? null,
          message: String(error.message ?? error),
        }));
      }
    };
    """
)


def _installed_e2e_dir(tmp_path: Path) -> Path:
    """A directory shaped like an installed e2e-ui, and nothing more.

    The control case must not pass because this particular machine happens to
    have run ``npm install``; it has to pass because the checks agree that
    nothing is wrong.
    """

    root = tmp_path / "e2e-ui"
    binary = root / "node_modules" / ".bin" / "playwright"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    manifest = root / "node_modules" / "@playwright" / "test" / "package.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"name":"@playwright/test"}\n', encoding="utf-8")
    return root


def _healthy_env(tmp_path: Path, node_toolchain_env: dict[str, str]) -> dict[str, str]:
    secret = tmp_path / "oidc-password"
    secret.write_text("hunter2\n", encoding="utf-8")
    return {
        **node_toolchain_env,
        "ASTRABOX_E2E_CONSOLE_URL": "http://127.0.0.1:8000",
        "ASTRABOX_E2E_OIDC_ISSUER": "http://127.0.0.1:8000/casdoor",
        "ASTRABOX_E2E_OIDC_USERNAME": "e2e",
        "ASTRABOX_E2E_OIDC_PASSWORD_FILE": str(secret),
        "ASTRABOX_E2E_STORAGE_STATE": str(tmp_path / "state.json"),
    }


def test_missing_playwright_install_is_named_before_the_round(
    tmp_path: Path,
    node_toolchain_env: dict[str, str],
) -> None:
    empty = tmp_path / "e2e-without-install"
    empty.mkdir()
    result = _run(
        _REPORT
        + f"""
        const {{ assertPlaywrightInstalled }} = await import('{_PREFLIGHT.as_uri()}');
        await report(() => assertPlaywrightInstalled({json.dumps(str(empty))}));
        """,
        node_toolchain_env,
    )
    assert result["ok"] is False
    assert result["code"] == "E2E_NODE_MODULES_UNAVAILABLE"
    # The repair belongs in the message: a round is usually started by someone
    # who did not set the machine up.
    assert "npm --prefix" in str(result["message"])


def test_a_missing_sign_in_secret_is_named_before_a_browser_launches(
    tmp_path: Path,
    node_toolchain_env: dict[str, str],
) -> None:
    env = _healthy_env(tmp_path, node_toolchain_env)
    env.pop("ASTRABOX_E2E_OIDC_PASSWORD_FILE")
    result = _run(
        _REPORT
        + f"""
        const {{ assertRequiredEnv }} = await import('{_PREFLIGHT.as_uri()}');
        await report(() => assertRequiredEnv(process.env));
        """,
        env,
    )
    assert result["ok"] is False
    assert result["code"] == "E2E_ENV_INCOMPLETE"
    assert "ASTRABOX_E2E_OIDC_PASSWORD_FILE" in str(result["message"])


def test_an_empty_secret_file_is_not_mistaken_for_a_password(
    tmp_path: Path,
    node_toolchain_env: dict[str, str],
) -> None:
    env = _healthy_env(tmp_path, node_toolchain_env)
    Path(env["ASTRABOX_E2E_OIDC_PASSWORD_FILE"]).write_text("", encoding="utf-8")
    result = _run(
        _REPORT
        + f"""
        const {{ assertRequiredEnv }} = await import('{_PREFLIGHT.as_uri()}');
        await report(() => assertRequiredEnv(process.env));
        """,
        env,
    )
    assert result["ok"] is False
    assert result["code"] == "E2E_OIDC_SECRET_UNREADABLE"


def test_an_unreachable_console_is_separated_from_an_unhealthy_one(
    tmp_path: Path,
    node_toolchain_env: dict[str, str],
) -> None:
    # Two different repairs — start the deployment, versus read its logs — so
    # they must not arrive as one code.
    env = _healthy_env(tmp_path, node_toolchain_env)
    result = _run(
        _REPORT
        + f"""
        const {{ assertConsoleReachable }} = await import('{_PREFLIGHT.as_uri()}');
        const refuse = async () => {{ throw new Error('ECONNREFUSED'); }};
        await report(() => assertConsoleReachable(process.env, refuse));
        """,
        env,
    )
    assert result["ok"] is False
    assert result["code"] == "E2E_CONSOLE_UNREACHABLE"

    result = _run(
        _REPORT
        + f"""
        const {{ assertConsoleReachable }} = await import('{_PREFLIGHT.as_uri()}');
        const unhealthy = async () => ({{ ok: false, status: 503 }});
        await report(() => assertConsoleReachable(process.env, unhealthy));
        """,
        env,
    )
    assert result["ok"] is False
    assert result["code"] == "E2E_CONSOLE_UNHEALTHY"
    assert "503" in str(result["message"])


def test_an_absent_database_container_is_named_rather_than_assumed(
    tmp_path: Path,
    node_toolchain_env: dict[str, str],
) -> None:
    env = _healthy_env(tmp_path, node_toolchain_env)
    env["ASTRABOX_E2E_POSTGRES_CONTAINER"] = "astrabox-postgres-that-is-not-running"
    result = _run(
        _REPORT
        + f"""
        const {{ assertOracleReachable }} = await import('{_PREFLIGHT.as_uri()}');
        const missing = () => ({{ status: 1, stderr: 'No such container' }});
        await report(() => assertOracleReachable(process.env, missing));
        """,
        env,
    )
    assert result["ok"] is False
    assert result["code"] == "E2E_ORACLE_UNAVAILABLE"
    # The name matters: the usual cause is an oracle pointed at a different
    # database than the server writes, which reads as a product disagreement.
    assert "astrabox-postgres-that-is-not-running" in str(result["message"])


def test_a_machine_with_nothing_wrong_passes_every_check(
    tmp_path: Path,
    node_toolchain_env: dict[str, str],
) -> None:
    # Without this, each red above would only show the check can throw.
    env = _healthy_env(tmp_path, node_toolchain_env)
    installed = _installed_e2e_dir(tmp_path)
    result = _run(
        _REPORT
        + f"""
        const {{ runE2ePreflight }} = await import('{_PREFLIGHT.as_uri()}');
        await report(() => runE2ePreflight({{
          e2eDir: {json.dumps(str(installed))},
          env: process.env,
          fetchImpl: async () => ({{ ok: true, status: 200 }}),
          runner: () => ({{ status: 0, stdout: '1' }}),
        }}));
        """,
        env,
    )
    assert result == {"ok": True}
