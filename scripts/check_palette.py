"""Colour comes from the palette, not from whatever Tailwind ships by default.

`styles.css` defines what every hue in this product MEANS — `--mint` is
completed, `--crimson` is failed, `--citrine` is waiting, `--teal` is running,
`--astra` is the primary action and selection (docs/frontend-design.md §7). A
component that reaches for `text-green-500` or `#f85149` instead is not picking
a colour, it is picking a DIFFERENT colour: Tailwind's green is not the mint the
rest of the page uses, so two things meaning "done" render in two greens on the
same screen, and neither is the one the palette chose.

The tool rows in a conversation carried `text-green-500`, `text-red-500` and a
`bg-blue-500/15` chip — three hues from outside the system, on the surface a
user spends their time on.

A baseline rather than a clean sweep: seventy-six of these existed when the
check was written, and a gate that cannot go green is a gate nobody runs. It
fails on anything NEW, and on a file whose count goes UP. Bringing a file to
zero removes it from the baseline, which is how the number comes down.

Four residents are not debt, and are baselined rather than special-cased in the
pattern — a rule with exceptions in it stops being decidable:

  · `AstraConsole` draws the mark's mask in pure white and pure black. Those
    are mask CHANNELS — what they select, not what anything is painted; no
    reader ever sees either value.

  · `ai-elements/tool.tsx` and `ai-elements/file-tree.tsx` are vendored from the
    AI SDK registry and held byte-identical to it
    (`scripts/check_upstream.py`, docs/maintainers/upstream-drift-ledger.md), so
    the seven ramp names in them are upstream's text, which this repository does
    not edit. What they RESOLVE to is a local decision: `styles.css` redefines
    those six ramp steps in `@theme inline` onto the palette, so the tool states
    paint citrine / teal / mint / plasma / crimson and the folder icon paints
    muted — the meanings §7 assigns. This check reads SOURCE, so it counts a
    name the page never paints; the row is here to say the name is accounted
    for, not that the colour is wrong. Both rows leave the baseline the day
    upstream stops spelling a hue into a component.

  · `ai-elements/terminal.tsx` is vendored the same way and spells a fixed
    dark terminal out of one neutral ramp — a ground, a rule, and control
    text. Its row is accounted for DIFFERENTLY from the two above, and the
    difference is the point: `@theme inline` does not redirect that ramp, so
    nothing makes the file correct by construction. What makes the PAGE
    correct is `components/TerminalPanel.tsx`, the only thing that composes
    it, which overrides each of those where it composes them — and a test in
    `TerminalPanel.test.tsx` holds the property that follows: no element the
    panel renders wears a colour from that ramp. Read that test as the other
    half of this row. This one counts names in a file nobody edits; that one
    checks what a reader is shown. Redirecting the ramp in `styles.css` would
    make it construction rather than assertion, and was not done because that
    ramp carries no meaning in §7 to redirect it ONTO — unlike yellow-600,
    which means "waiting".

Baseline entries are removed when their source count reaches zero. For the
terminal entry, `TerminalPanel.test.tsx` also checks rendered properties because
source-name scanning cannot prove the composed result.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "frontend" / "src"
BASELINE = REPO_ROOT / "scripts" / "palette_baseline.json"

# Tailwind's default ramps, and any hand-written hex. `styles.css` is where the
# palette is DEFINED, so it is the one file allowed to spell a colour out.
_RAMPS = (
    "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|"
    "teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose"
)
RAW_COLOUR = re.compile(rf"(?:\b(?:{_RAMPS})-(?:50|[1-9]00|950)\b)|(?:#[0-9a-fA-F]{{3,8}}\b)")

# A test renders colours to assert on them; it ships nothing to a reader.
#
# `.ts` as well as `.tsx`: a class list is a string, and the modules that map a
# backend state to one are plain TypeScript. Reading only `.tsx` left forty raw
# ramp values — every one of them a status colour — outside the gate that
# exists to keep status colour on the palette.
def _sources() -> list[Path]:
    return sorted(
        p
        for suffix in ("*.tsx", "*.ts")
        for p in SOURCE.rglob(suffix)
        if ".test." not in p.name
    )


def count_by_file() -> dict[str, int]:
    counts: Counter[str] = Counter()
    for path in _sources():
        found = RAW_COLOUR.findall(path.read_text(encoding="utf-8"))
        if found:
            counts[str(path.relative_to(SOURCE))] = len(found)
    return dict(counts)


def self_check() -> str | None:
    """Fail on a planted colour, so a clean report means the check ran."""
    planted = 'className="text-green-500 bg-[#f85149]"'
    found = RAW_COLOUR.findall(planted)
    if sorted(found) != ["#f85149", "green-500"]:
        return f"the pattern does not detect a planted colour: {found}"
    if RAW_COLOUR.findall('className="text-mint bg-crimson-tint"'):
        return "the pattern flags a token, which would make every file a violation"
    return None


def main() -> int:
    broken = self_check()
    if broken:
        print(f"check_palette is not working: {broken}", file=sys.stderr)
        return 1

    current = count_by_file()
    baseline: dict[str, int] = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}

    if "--update" in sys.argv:
        BASELINE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"palette baseline written — {sum(current.values())} in {len(current)} files")
        return 0

    regressions = [
        f"{path}: {count} raw colours, baseline allows {baseline.get(path, 0)}"
        for path, count in sorted(current.items())
        if count > baseline.get(path, 0)
    ]
    if regressions:
        print(
            "Raw colours outside the palette. Use a "
            "token — --mint / --crimson / --citrine / --teal / --astra and their "
            "tints — or `styles.css` if a new one is genuinely needed:",
            file=sys.stderr,
        )
        for line in regressions:
            print(f"  - {line}", file=sys.stderr)
        return 1

    total = sum(current.values())
    allowed = sum(baseline.values())
    print(
        f"palette OK — {total} raw colours in {len(current)} files "
        f"(baseline {allowed}); nothing new"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
