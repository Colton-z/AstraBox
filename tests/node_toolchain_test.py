from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/node-toolchain.py"


def _module() -> ModuleType:
    specification = importlib.util.spec_from_file_location("node_toolchain", SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_toolchain(root: Path, version: str) -> Path:
    bin_dir = root / "bin"
    _executable(bin_dir / "node", f"#!/bin/sh\nprintf 'v{version}\\n'\n")
    _executable(bin_dir / "npm", "#!/bin/sh\nexit 0\n")
    _executable(bin_dir / "npx", "#!/bin/sh\nexit 0\n")
    return bin_dir / "node"


def _archive(version: str) -> bytes:
    payload = io.BytesIO()
    prefix = f"node-v{version}-linux-x64"
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for name, source in {
            "node": f"#!/bin/sh\nprintf 'v{version}\\n'\n",
            "npm": "#!/bin/sh\nexit 0\n",
            "npx": "#!/bin/sh\nexit 0\n",
        }.items():
            value = source.encode()
            info = tarfile.TarInfo(f"{prefix}/bin/{name}")
            info.mode = 0o755
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    return payload.getvalue()


def test_repository_has_one_exact_node_version() -> None:
    module = _module()
    version = module.pinned_version()

    assert version == "26.8.1"
    assert set(module.ARTIFACT_SHA256) == {
        ("darwin", "arm64"),
        ("darwin", "x64"),
        ("linux", "arm64"),
        ("linux", "x64"),
    }


def test_every_repository_node_consumer_uses_the_same_pin() -> None:
    version = (REPO_ROOT / ".nvmrc").read_text(encoding="utf-8").strip()
    packages = (
        REPO_ROOT / "frontend/package.json",
        REPO_ROOT / "e2e/package.json",
        REPO_ROOT / "tests/e2e-ui/package.json",
        REPO_ROOT / "website/package.json",
    )
    locks = tuple(path.with_name("package-lock.json") for path in packages)

    for path in packages:
        document = json_load(path.read_text(encoding="utf-8"))
        engines = document["engines"]
        assert isinstance(engines, dict)
        assert engines["node"] == version
    for path in locks:
        document = json_load(path.read_text(encoding="utf-8"))
        packages_value = document["packages"]
        assert isinstance(packages_value, dict)
        root_package = packages_value[""]
        assert isinstance(root_package, dict)
        engines = root_package["engines"]
        assert isinstance(engines, dict)
        assert engines["node"] == version
    for path in REPO_ROOT.glob(".github/workflows/*.yml"):
        source = path.read_text(encoding="utf-8")
        if "actions/setup-node@" in source:
            assert "node-version-file: .nvmrc" in source, path
            assert re.search(r"node-version:\s*['\"]?22", source) is None, path
    dockerfile = (REPO_ROOT / "containers/server/Dockerfile").read_text(encoding="utf-8")
    assert f"ARG NODE_VERSION={version}" in dockerfile
    assert "FROM node:${NODE_VERSION}-slim AS web" in dockerfile


def test_make_node_commands_enter_the_toolchain() -> None:
    source = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "NODE_TOOLCHAIN   ?= python3 scripts/node-toolchain.py" in source
    offenders = [
        row
        for row in source.splitlines()
        if row.startswith("\t") and re.match(r"\s*(?:node|npm|npx)\b", row)
    ]
    assert offenders == []
    pytest_recipes = [row for row in source.splitlines() if row.startswith("\t") and "$(PYTEST)" in row]
    assert pytest_recipes
    assert all("$(NODE_TOOLCHAIN) $(PYTEST)" in row for row in pytest_recipes)


def test_backend_ci_enters_the_same_node_toolchain() -> None:
    source = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    backend = source.split("\n  web:", 1)[0]

    assert "node-version-file: .nvmrc" in backend
    assert "python scripts/node-toolchain.py --no-download python -m pytest" in backend


def test_resolver_ignores_a_wrong_path_version_and_finds_exact_nvm(
    tmp_path: Path,
) -> None:
    module = _module()
    wrong = _fake_toolchain(tmp_path / "wrong", "16.14.0")
    exact = _fake_toolchain(tmp_path / "nvm/versions/node/v26.8.1", "26.8.1")

    resolved = module.resolve_toolchain(
        cache_root=tmp_path / "cache",
        allow_download=False,
        environment={
            "HOME": str(tmp_path),
            "NVM_DIR": str(tmp_path / "nvm"),
            "PATH": str(wrong.parent),
        },
    )

    assert resolved.node == exact.resolve()
    assert resolved.version == "26.8.1"


def test_managed_mode_does_not_borrow_ambient_nvm(tmp_path: Path) -> None:
    module = _module()
    _fake_toolchain(tmp_path / "nvm/versions/node/v26.8.1", "26.8.1")

    with pytest.raises(module.ToolchainError, match="is not installed"):
        module.resolve_toolchain(
            cache_root=tmp_path / "cache",
            allow_download=False,
            managed=True,
            environment={
                "HOME": str(tmp_path),
                "NVM_DIR": str(tmp_path / "nvm"),
                "PATH": "",
            },
        )


def test_installer_checks_the_archive_and_reuses_the_managed_copy(
    tmp_path: Path,
) -> None:
    module = _module()
    payload = _archive("26.8.1")
    calls: list[str] = []

    def download(url: str, output: object) -> None:
        calls.append(url)
        output.write(payload)  # type: ignore[attr-defined]

    first = module.install_node(
        version="26.8.1",
        cache_root=tmp_path / "cache",
        key=("linux", "x64"),
        downloader=download,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    second = module.install_node(
        version="26.8.1",
        cache_root=tmp_path / "cache",
        key=("linux", "x64"),
        downloader=lambda *_args: pytest.fail("cached toolchain downloaded twice"),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )

    assert first == second
    assert first.node == (tmp_path / "cache/v26.8.1/bin/node").resolve()
    assert calls == ["https://nodejs.org/dist/v26.8.1/node-v26.8.1-linux-x64.tar.gz"]


def test_installer_refuses_a_checksum_mismatch(tmp_path: Path) -> None:
    module = _module()
    payload = _archive("26.8.1")

    with pytest.raises(module.ToolchainError, match="checksum mismatch"):
        module.install_node(
            version="26.8.1",
            cache_root=tmp_path / "cache",
            key=("linux", "x64"),
            downloader=lambda _url, output: output.write(payload),
            expected_sha256="0" * 64,
        )


def test_cli_prepends_the_exact_node_directory(tmp_path: Path) -> None:
    node = _fake_toolchain(tmp_path / "nvm/versions/node/v26.8.1", "26.8.1")
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--no-download",
            sys.executable,
            "-c",
            "import json,os; print(json.dumps({'path': os.environ['PATH']}))",
        ],
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "NVM_DIR": str(tmp_path / "nvm"),
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    value = json_load(completed.stdout)["path"]
    assert isinstance(value, str)
    assert value.split(os.pathsep)[0] == str(node.parent.resolve())


def json_load(value: str) -> dict[str, object]:
    return json.loads(value)


def test_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)
