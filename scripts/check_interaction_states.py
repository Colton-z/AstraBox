"""Touching a control may repaint it. It may not change what size or shape it is.

This is the half of the visual grammar that the walking audit
(``tests/e2e-ui/specs/visual-grammar.audit.spec.ts``) cannot reach: every
check there reads a page at rest, while the defects that prompted it — a button
whose text shrank when pressed, a row that changed shape when tabbed to — are
only visible under a pointer or a Tab key. Two attempts to catch them by driving
a browser were removed, because neither could be made to fail with the defect
deliberately present: ``:focus-visible`` does not match programmatic focus, and
a hover has to be aimed at the right element out of hundreds.

The stylesheet answers the same question by reading. A hover, focus or active
rule that sets a font size, a padding, a border width or a scale changes how big
the thing under the cursor is; one that sets a radius reshapes it instead of the
ring around it, because an outline already follows the element's own corners.

Size and shape only — MOVEMENT is allowed. A button that sinks a pixel when
pressed is feedback, and it is written once on the base button so every button
does it. What reads as broken is a control that is not the size it was: the
scale that prompted this check was on exactly one control in the app, so
pressing that one thing shrank its contents and nothing else ever did.

Reads the BUILT stylesheet, not the source. A utility is only real once Tailwind
has emitted it, so ``active:scale-95`` written in a ``className`` is invisible to
any search of the CSS sources. ``make test-web`` builds before it checks.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILT_CSS = REPO_ROOT / "frontend" / "dist" / "assets"

# A declaration block whose selector names an interaction state.
INTERACTION = re.compile(
    r"([^{}]*:(?:hover|active|focus|focus-visible|focus-within)[^{}]*)\{([^{}]*)\}"
)

_BOX_SIDES = ("", "-top", "-bottom", "-left", "-right", "-inline", "-block")

# Properties that change how big an element is, or what shape.
SIZE_AND_SHAPE = (
    {
        "font-size",
        "font-weight",
        "font-family",
        "font-stretch",
        "letter-spacing",
        "line-height",
        "width",
        "height",
        "gap",
        "border-radius",
        "scale",
    }
    | {f"padding{side}" for side in _BOX_SIDES}
    | {f"margin{side}" for side in _BOX_SIDES}
    | {f"border{side}-width" for side in _BOX_SIDES}
    | {f"--tw-scale-{axis}" for axis in "xyz"}
)

# Enough of the real stylesheet's interaction rules that finding none means the
# input was not parsed rather than that the rules are clean.
MIN_INTERACTION_RULES = 50


def resizing_rules(css: str) -> list[str]:
    """Every interaction rule in ``css`` that sets a size or shape property."""
    offenders = []
    for selector, body in INTERACTION.findall(css):
        changed = sorted(
            {
                declaration.split(":", 1)[0].strip()
                for declaration in body.split(";")
                if ":" in declaration and declaration.split(":", 1)[0].strip() in SIZE_AND_SHAPE
            }
        )
        if changed:
            offenders.append(f"{' '.join(selector.split())} changes {', '.join(changed)}")
    return offenders


def self_check() -> str | None:
    """Fail on a planted defect, so a clean report means the check ran.

    A regex over a file it cannot parse reports every page as clean, which is
    the failure mode this exists to rule out.
    """
    planted = ".probe:hover{padding:9px;font-size:11px}\n.probe2:active{--tw-scale-x:95%}"
    found = resizing_rules(planted)
    expected = [
        ".probe:hover changes font-size, padding",
        ".probe2:active changes --tw-scale-x",
    ]
    if found != expected:
        return f"the check does not detect a planted rule: expected {expected}, got {found}"
    return None


def main() -> int:
    broken = self_check()
    if broken:
        print(f"check_interaction_states is not working: {broken}", file=sys.stderr)
        return 1

    sheets = sorted(BUILT_CSS.glob("*.css")) if BUILT_CSS.is_dir() else []
    if not sheets:
        print(
            f"no built stylesheet under {BUILT_CSS.relative_to(REPO_ROOT)} — this check reads "
            f"what Tailwind emitted, which exists only after a build. Run `make test-web`.",
            file=sys.stderr,
        )
        return 1

    css = "\n".join(sheet.read_text(encoding="utf-8") for sheet in sheets)
    rules = INTERACTION.findall(css)
    if len(rules) < MIN_INTERACTION_RULES:
        print(
            f"only {len(rules)} interaction rules found in {len(sheets)} stylesheet(s) — "
            f"expected at least {MIN_INTERACTION_RULES}. The stylesheet was not parsed, so "
            f"nothing was checked.",
            file=sys.stderr,
        )
        return 1

    offenders = resizing_rules(css)
    if offenders:
        print(
            "Interaction states that resize or reshape what they apply to; a "
            "hover, focus or active state may change colour and ring, not size or shape:",
            file=sys.stderr,
        )
        for offender in offenders:
            print(f"  - {offender}", file=sys.stderr)
        return 1

    print(f"interaction states OK — {len(rules)} rules, none changes size or shape")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
