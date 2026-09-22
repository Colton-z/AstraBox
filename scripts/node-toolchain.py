#!/usr/bin/env python3
"""Run a command with the repository's checksum-pinned Node.js toolchain."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
PIN_FILE = REPO_ROOT / ".nvmrc"
DEFAULT_CACHE_ROOT = REPO_ROOT / ".astrabox/toolchains/node"
ARTIFACT_SHA256 = {
    ("darwin", "arm64"): "6e577fd0d9db776db82306629e441a9dace416702622aebdd171c9dfaa41f4d2",
    ("darwin", "x64"): "fe9c6dbf9c8e1b4443803d75e2a20366e420dae650c747dbb116b22975751baf",
    ("linux", "arm64"): "d5f973ce975e4bd03e6c2038260f7e9201615aa8e1ee293c72f8dcc2a6d9fddb",
    ("linux", "x64"): "b2b76660fa4ded4e0b2a41ee3c0c651cd52ea8170ead91ebac1e147ac3d55643",
}


class ToolchainError(RuntimeError):
    """The pinned Node.js toolchain could not be selected or installed."""


@dataclass(frozen=True)
class NodeToolchain:
    node: Path
    npm: Path
    npx: Path
    version: str

    @property
    def bin_dir(self) -> Path:
        return self.node.parent


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ToolchainError(message)


def pinned_version(pin_file: Path = PIN_FILE) -> str:
    try:
        value = pin_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ToolchainError(f"cannot read Node.js pin {pin_file}: {exc}") from exc
    require(
        re.fullmatch(r"[1-9][0-9]*\.[0-9]+\.[0-9]+", value) is not None,
        f"invalid Node.js version in {pin_file}: {value!r}",
    )
    return value


def platform_key(
    system: str | None = None,
    machine: str | None = None,
) -> tuple[str, str]:
    system_value = (system or platform.system()).lower()
    machine_value = (machine or platform.machine()).lower()
    systems = {"linux": "linux", "darwin": "darwin"}
    machines = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "amd64": "x64",
        "x86_64": "x64",
    }
    require(
        system_value in systems and machine_value in machines,
        f"unsupported Node.js host: system={system_value} machine={machine_value}",
    )
    return systems[system_value], machines[machine_value]


def _version(node: Path) -> str | None:
    try:
        completed = subprocess.run(
            [str(node), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env={**os.environ, "PATH": f"{node.parent}{os.pathsep}{os.environ.get('PATH', '')}"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip().removeprefix("v")
    return value if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value) else None


def inspect_node(node: Path, expected_version: str) -> NodeToolchain | None:
    node = node.expanduser().resolve()
    npm = node.parent / "npm"
    npx = node.parent / "npx"
    if not all(path.is_file() and os.access(path, os.X_OK) for path in (node, npm, npx)):
        return None
    if _version(node) != expected_version:
        return None
    return NodeToolchain(node=node, npm=npm, npx=npx, version=expected_version)


def installed_candidates(
    *,
    version: str,
    cache_root: Path,
    environment: dict[str, str] | None = None,
) -> list[Path]:
    environment = environment or dict(os.environ)
    candidates: list[Path] = []
    if current := shutil.which("node", path=environment.get("PATH", "")):
        candidates.append(Path(current))
    home = Path(environment.get("HOME", str(Path.home())))
    nvm_root = Path(environment.get("NVM_DIR", str(home / ".nvm")))
    candidates.append(nvm_root / f"versions/node/v{version}/bin/node")
    candidates.append(cache_root / f"v{version}/bin/node")
    return list(dict.fromkeys(candidates))


def find_installed(
    *,
    version: str,
    cache_root: Path,
    environment: dict[str, str] | None = None,
) -> NodeToolchain | None:
    for candidate in installed_candidates(
        version=version,
        cache_root=cache_root,
        environment=environment,
    ):
        if toolchain := inspect_node(candidate, version):
            return toolchain
    return None


def find_managed(*, version: str, cache_root: Path) -> NodeToolchain | None:
    return inspect_node(cache_root / f"v{version}/bin/node", version)


def _safe_archive_members(archive: tarfile.TarFile, prefix: str) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    require(bool(members), "downloaded Node.js archive is empty")
    expected_root = PurePosixPath(prefix)
    for member in members:
        path = PurePosixPath(member.name)
        require(
            not path.is_absolute()
            and ".." not in path.parts
            and path.parts
            and path.parts[0] == expected_root.name,
            f"downloaded Node.js archive has an unsafe path: {member.name}",
        )
    return members


def _download(url: str, output: BinaryIO) -> None:
    with urllib.request.urlopen(url, timeout=30) as response:
        require(getattr(response, "status", 200) == 200, f"Node.js download returned HTTP {response.status}")
        shutil.copyfileobj(response, output, length=1024 * 1024)


def install_node(
    *,
    version: str,
    cache_root: Path,
    key: tuple[str, str],
    downloader: Callable[[str, BinaryIO], None] = _download,
    expected_sha256: str | None = None,
) -> NodeToolchain:
    system_name, architecture = key
    digest = expected_sha256 or ARTIFACT_SHA256.get(key)
    require(digest is not None, f"no Node.js checksum for {system_name}-{architecture}")
    filename = f"node-v{version}-{system_name}-{architecture}.tar.gz"
    url = f"https://nodejs.org/dist/v{version}/{filename}"
    final_root = cache_root / f"v{version}"
    cache_root.mkdir(parents=True, exist_ok=True)
    lock_path = cache_root / f"v{version}.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if toolchain := inspect_node(final_root / "bin/node", version):
            return toolchain
        with tempfile.TemporaryDirectory(prefix=f"node-v{version}-", dir=cache_root) as temporary:
            temporary_root = Path(temporary)
            archive_path = temporary_root / filename
            with archive_path.open("w+b") as output:
                try:
                    downloader(url, output)
                except (OSError, ToolchainError) as exc:
                    raise ToolchainError(
                        f"cannot download pinned Node.js {version} from {url}: {exc}; "
                        f"install it with `nvm install {version}` and retry"
                    ) from exc
                output.flush()
                output.seek(0)
                actual = hashlib.file_digest(output, "sha256").hexdigest()
            require(
                actual == digest,
                f"Node.js archive checksum mismatch: expected={digest} actual={actual}",
            )
            extracted = temporary_root / "extracted"
            extracted.mkdir()
            prefix = filename.removesuffix(".tar.gz")
            with tarfile.open(archive_path, mode="r:gz") as archive:
                members = _safe_archive_members(archive, prefix)
                archive.extractall(extracted, members=members, filter="data")
            candidate_root = extracted / prefix
            require(candidate_root.is_dir(), "downloaded Node.js archive has no toolchain root")
            if final_root.exists():
                invalid_root = cache_root / f"v{version}.invalid.{os.getpid()}"
                os.replace(final_root, invalid_root)
                shutil.rmtree(invalid_root)
            os.replace(candidate_root, final_root)
        toolchain = inspect_node(final_root / "bin/node", version)
        require(toolchain is not None, "installed Node.js toolchain failed its version check")
        return toolchain


def resolve_toolchain(
    *,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    allow_download: bool = True,
    managed: bool = False,
    environment: dict[str, str] | None = None,
) -> NodeToolchain:
    version = pinned_version()
    toolchain = (
        find_managed(version=version, cache_root=cache_root)
        if managed
        else find_installed(
            version=version,
            cache_root=cache_root,
            environment=environment,
        )
    )
    if toolchain:
        return toolchain
    require(
        allow_download,
        f"Node.js {version} is not installed; run `nvm install {version}` or omit --no-download",
    )
    return install_node(
        version=version,
        cache_root=cache_root,
        key=platform_key(),
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    value.add_argument("--no-download", action="store_true")
    value.add_argument(
        "--managed",
        action="store_true",
        help="Use only the checksum-pinned cache, ignoring ambient PATH and NVM",
    )
    output = value.add_mutually_exclusive_group()
    output.add_argument("--print-bin", action="store_true")
    output.add_argument("--print-node", action="store_true")
    output.add_argument("--check", action="store_true")
    value.add_argument("command", nargs=argparse.REMAINDER)
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        toolchain = resolve_toolchain(
            cache_root=args.cache_root.resolve(),
            allow_download=not args.no_download,
            managed=args.managed,
        )
    except ToolchainError as exc:
        print(f"NODE TOOLCHAIN FAILED: {exc}", file=sys.stderr)
        return 69
    if args.print_bin:
        print(toolchain.bin_dir)
        return 0
    if args.print_node:
        print(toolchain.node)
        return 0
    if args.check:
        print(
            json.dumps(
                {
                    "bin": str(toolchain.bin_dir),
                    "node": str(toolchain.node),
                    "npm": str(toolchain.npm),
                    "npx": str(toolchain.npx),
                    "state": "PASS",
                    "version": toolchain.version,
                },
                sort_keys=True,
            )
        )
        return 0
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser().error("provide a command or one of --print-bin/--print-node/--check")
    environment = dict(os.environ)
    environment["PATH"] = f"{toolchain.bin_dir}{os.pathsep}{environment.get('PATH', '')}"
    try:
        os.execvpe(command[0], command, environment)
    except OSError as exc:
        print(f"NODE TOOLCHAIN FAILED: cannot execute {command[0]}: {exc}", file=sys.stderr)
        return 69


if __name__ == "__main__":
    raise SystemExit(main())
