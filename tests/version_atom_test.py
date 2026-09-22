"""The SDK, CLI, websockets, and runner interpreter form one image atom.

The wheel the platform imports, the wheel baked into the agent image, and the
vendor CLI that image installs sit on either side of the box boundary and are
exercised against each other. Each version appears in the platform dependency
pin and the image build argument because the host and box are separate
deployables; mismatched pins can fail on their shared wire protocol.

The SDK and CLI tests assert equality of the strings as written. Websockets is
different on purpose: the platform declares a compatibility floor while the
box pins one reproducible build at or above it. The image build additionally
checks the selected CLI against its pin; the runner explicitly supplies that
binary through the SDK's native ``cli_path`` option. The runner dependency
installer and both launch paths must use the
same supplier-provided Python interpreter.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"
_AGENT_DOCKERFILE = _ROOT / "containers" / "sandbox-claude-code" / "Dockerfile"
_RUNNER_LAUNCHER = _AGENT_DOCKERFILE.with_name("start-runner.sh")
_CLAUDE_ENGINE = _ROOT / "astrabox/core/service/orchestrator/engine/claude_code.py"
_MAKEFILE = _ROOT / "Makefile"


def _one(pattern: str, path: Path) -> str:
    """The single capture of ``pattern`` in ``path``, or a failure naming both.

    More than one match is as much a defect as none: a second spelling is a
    second place to update, which is the drift this test exists to catch.
    """
    matches = re.findall(pattern, path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    assert len(matches) == 1, (
        f"expected exactly one {pattern!r} in {path.relative_to(_ROOT)}, found {matches}"
    )
    return str(matches[0]).strip()


def test_the_agent_sdk_pin_is_the_same_string_in_the_platform_and_the_image() -> None:
    platform = _one(r'^\s*"claude-agent-sdk==([^"]+)"', _PYPROJECT)
    image = _one(r"^ARG CLAUDE_AGENT_SDK_VERSION=(\S+)", _AGENT_DOCKERFILE)
    assert platform == image, (
        "the platform imports claude-agent-sdk "
        f"{platform} while the agent image bakes {image}; an SDK upgrade is a "
        "contract migration and both sides move in the same commit"
    )


def test_the_vendor_cli_pin_is_the_same_string_in_the_build_and_the_image() -> None:
    build = _one(r"^CLAUDE_CODE_VERSION \?= (\S+)", _MAKEFILE)
    image = _one(r"^ARG CLAUDE_CODE_VERSION=(\S+)", _AGENT_DOCKERFILE)
    assert build == image, (
        f"make build-agent-image passes CLAUDE_CODE_VERSION={build} while the "
        f"image's own default is {image}; a build that omits the argument then "
        "produces a different box from one that passes it"
    )


def test_the_box_never_runs_a_websockets_older_than_the_platform_requires() -> None:
    """The third pin, and the one that is a floor against an exact version.

    `sandbox_runner.py` runs on both sides of the boundary and imports
    `websockets.asyncio.server`, so the platform declares the floor that import
    needs and the image pins one build exactly. Those are different shapes on
    purpose — the platform must tolerate whatever a deployment resolves, the box
    must be reproducible — so equality is the wrong assertion here and the
    ordering is the right one: the box may run ahead of the floor, never behind
    it. A box behind it imports a module that is not there, inside a container,
    where the failure reads as the sandbox being broken.
    """
    floor = _one(r'^\s*"websockets>=([^"]+)"', _PYPROJECT)
    image = _one(r"^ARG WEBSOCKETS_VERSION=(\S+)", _AGENT_DOCKERFILE)

    assert re.fullmatch(r"\d+(\.\d+)*", image), (
        f"the image pins websockets {image!r}; a range there makes the box a "
        "different box on every rebuild"
    )
    as_numbers = lambda text: tuple(int(part) for part in text.split("."))  # noqa: E731
    assert as_numbers(image) >= as_numbers(floor), (
        f"the platform requires websockets>={floor} and the image bakes {image}; "
        "the same source file runs on both sides, so the box must not be the "
        "older half"
    )


def test_runner_dependencies_and_launches_use_the_supplier_python() -> None:
    """Install and execute the runner under AIO's Python 3.12, never its 3.10 default."""
    interpreter = "/usr/local/bin/python3.12"
    image = _AGENT_DOCKERFILE.read_text(encoding="utf-8")
    launcher = _RUNNER_LAUNCHER.read_text(encoding="utf-8")
    engine = _CLAUDE_ENGINE.read_text(encoding="utf-8")

    assert "/usr/local/bin/pip3.12 install" in image
    assert f'{interpreter} -c "import claude_agent_sdk' in image
    assert f'{interpreter} "$ASTRABOX_INBOX_SERVER"' in launcher
    assert f"{interpreter} /opt/astrabox/sandbox_runner.py" in engine


def test_both_pins_are_exact_versions_rather_than_ranges() -> None:
    for label, value in (
        ("claude-agent-sdk", _one(r'^\s*"claude-agent-sdk==([^"]+)"', _PYPROJECT)),
        ("CLAUDE_CODE_VERSION", _one(r"^CLAUDE_CODE_VERSION \?= (\S+)", _MAKEFILE)),
        ("WEBSOCKETS_VERSION", _one(r"^ARG WEBSOCKETS_VERSION=(\S+)", _AGENT_DOCKERFILE)),
    ):
        assert re.fullmatch(r"\d+(\.\d+)*", value), (
            f"{label} is {value!r}; a range or a moving tag lets one side of the "
            "atom follow the registry while the other stays put, which is the "
            "drift the equality assertions above cannot see"
        )
