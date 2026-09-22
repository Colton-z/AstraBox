"""Claude's vocabulary must not return to the platform half of the seam.

The interaction seam splits one rule across two trees.  This gate closes the
BACKEND PLATFORM half only: no Claude tool name and no Claude permission-mode
name may appear in ``astrabox/core/service/orchestrator`` outside the adapter
files that own those semantics.  Those are the modules that decide what an
interaction *is*, so a vendor literal there is the exact failure the seam was
rebuilt to remove — core inferring meaning from a name instead of reading the
adapter's declared contract.  Not covered here, deliberately:

* the frontend, which reads ``presentation`` and has its own checks;
* generic mode words such as ``plan`` and ``default``, which are ordinary
  English in this tree and would make the scan a source of false alarms;
* the adapter files themselves, where every one of these names belongs;
* the named data sites below — a mode literal that is validated product
  DATA, not a branch on vendor meaning. Each is pinned to its file and
  fails this gate when it moves or disappears, so the list cannot rot.

The scan is plain substring over raw source, comments included: a comment that
names ``AskUserQuestion`` is how the platform learns the vendor's vocabulary
back, and it goes stale silently.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PLATFORM_ROOT = _ROOT / "astrabox/core/service/orchestrator"

#: Claude tool names and Claude permission-mode names, exact.
_ENGINE_VOCABULARY = (
    "AskUserQuestion",
    "ExitPlanMode",
    "bypassPermissions",
    "acceptEdits",
    "dontAsk",
)

#: Adapter-owned files, relative to the orchestrator package.  These implement
#: Claude's semantics — the codec that maps its tools onto presentations, the
#: client and runtime that speak to its CLI, its frame translation, and the
#: in-box runner.  Hermes's own files are excluded on the same grounds.
_ADAPTER_OWNED = frozenset(
    {
        "engine/claude_interaction_codec.py",
        "engine/claude_code_client.py",
        "engine/claude_code_runtime.py",
        "engine/claude_code_background.py",
        "engine/claude_message_blocks.py",
        "engine/claude_code.py",
        "engine/frame_translator.py",
        "engine/tool_result_semantics.py",
        "sandbox_runner.py",
    }
)


def is_adapter_owned(relative_path: str) -> bool:
    return relative_path in _ADAPTER_OWNED or relative_path.startswith("engine/hermes")


def scan_for_engine_vocabulary(
    root: Path,
    *,
    is_excluded: Callable[[str], bool],
) -> list[str]:
    """Return one ``path:line: literal`` finding per vendor literal occurrence."""
    findings: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative_path = path.relative_to(root).as_posix()
        if is_excluded(relative_path):
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            for literal in _ENGINE_VOCABULARY:
                if literal in line:
                    findings.append(f"{relative_path}:{number}: {literal}")
    return findings


def _plant(root: Path, relative_path: str, body: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_the_scan_flags_a_planted_literal(tmp_path: Path) -> None:
    """Fire the gate at a known violation before trusting a clean run.

    A checker that has never been observed to fire proves only that the tree
    parsed.  This plants one literal in a comment, one in code, one file with
    none, and one inside an excluded path — so a green run over the real tree
    means the scan reads files, matches comments, and honours the exclusion.
    """

    _plant(tmp_path, "clean.py", "PRESENTATION = 'form'\n# the adapter decides\n")
    _plant(tmp_path, "commented.py", "# AskUserQuestion / ExitPlanMode\nX = 1\n")
    _plant(tmp_path, "coded.py", "MODE = 'bypassPermissions'\n")
    _plant(tmp_path, "engine/hermes_client.py", "MODE = 'acceptEdits'\n")

    findings = scan_for_engine_vocabulary(tmp_path, is_excluded=is_adapter_owned)

    assert findings == [
        "coded.py:1: bypassPermissions",
        "commented.py:1: AskUserQuestion",
        "commented.py:1: ExitPlanMode",
    ]


def test_the_scan_reaches_the_real_platform_modules() -> None:
    """A moved package would empty the walk and pass this gate silently."""

    scanned = [
        path
        for path in sorted(_PLATFORM_ROOT.rglob("*.py"))
        if not is_adapter_owned(path.relative_to(_PLATFORM_ROOT).as_posix())
    ]
    assert len(scanned) > 100
    assert any(path.name == "engine_turn.py" for path in scanned)
    assert any(path.name == "interaction_contract.py" for path in scanned)


def test_platform_modules_carry_no_engine_vocabulary() -> None:
    findings = scan_for_engine_vocabulary(_PLATFORM_ROOT, is_excluded=is_adapter_owned)
    assert findings == [], (
        "Claude tool names and permission modes belong to the Claude adapter; "
        "a platform module that names one is deciding meaning core must read "
        "from the declared contract instead:\n"
        + "\n".join(
            f"  astrabox/core/service/orchestrator/{item}" for item in findings
        )
    )


def test_runtime_config_resolver_has_no_engine_specific_builder_surface() -> None:
    from astrabox.core.service.orchestrator.runtime.config_resolver import (
        RuntimeConfigResolver,
    )

    engine_owned = sorted(
        name
        for name in vars(RuntimeConfigResolver)
        if name.startswith("build_claude") or name.startswith("apply_claude")
    )
    assert engine_owned == [], (
        "engine launch options belong to the selected adapter, not the shared "
        f"runtime config resolver: {engine_owned}"
    )
