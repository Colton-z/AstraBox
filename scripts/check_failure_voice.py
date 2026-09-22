#!/usr/bin/env python3
"""A failure is said one way, and it is announced.

`components/shell/ErrorNote.tsx` is the small inline failure. It does not draw a
box: it presets the vendored `components/ui/alert.tsx`, which carries
`role="alert"` so assistive technology hears the failure arrive, and adds the
failure tint and `data-slot="verbatim"`, so the rules about how this product
writes do not judge a backend's own words. Nine console pages were folded into
it. Within a day three more copies had appeared — one in a panel written that
morning — because nothing stopped them.

Two signals, and they are not equally decidable, so they are not enforced the
same way.

**Hand-written `role="alert"` fails outright.** Nobody types that attribute
except to rebuild this note; the shared one already carries it. Zero tolerance,
and no baseline, because there is nothing to weigh up.

**A sentence in the failure colour is counted against a baseline.** The signal is
a className carrying both the failure colour and a type size — an icon carries
`size-*` instead, which is why the tell is the pair. It cannot be a hard rule:
a field's validation message, a full-bleed banner and a tool's `<pre>` of stderr
are all sentences in that colour and none of them is this note. So the existing
ones are recorded and anything NEW has to argue for itself, the same trade
`check_palette.py` makes and for the same reason — a gate that cannot go green
is a gate nobody runs.

Run by `make test-web`. Read from the sources rather than the build: both signals
are authored text, and unlike a Tailwind utility neither can be dropped or
rewritten on the way to the stylesheet.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "src"
BASELINE = Path(__file__).resolve().parent / "failure_voice_baseline.json"

# The full error card a list page shows when its one fetch cannot render. It is
# the one place outside the vendored components that spells the role by hand.
# `ErrorNote` needs no exemption: the Alert it presets brings the role with it.
ALLOWED = {
    "manage/console/ConsoleEmptyState.tsx",
}
ALLOWED_DIRS = ("components/ui/",)

FAILURE_COLOUR = re.compile(r"text-(?:destructive|crimson)\b")
TYPE_SIZE = re.compile(r"\b(?:t-copy(?:-sm)?|text-(?:xs|sm|base|10|11|12|13|15))\b")
CLASSNAME = re.compile(r'className=(?:"([^"]*)"|\{`([^`]*)`\}|\{cn\(([^)]*)\))')
COMMENT = re.compile(r"^\s*(?://|\*|/\*)")


def exempt(rel: str) -> bool:
    return rel in ALLOWED or any(rel.startswith(d) for d in ALLOWED_DIRS)


def main() -> int:
    hand_rolled: list[str] = []
    sentences: Counter[str] = Counter()
    where: dict[str, list[str]] = {}

    for path in sorted(SRC.rglob("*.tsx")):
        rel = path.relative_to(SRC).as_posix()
        if exempt(rel) or rel.endswith(".test.tsx"):
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            # A comment naming the attribute is documentation, not a second note.
            if COMMENT.match(line):
                continue
            if 'role="alert"' in line:
                hand_rolled.append(f'{rel}:{number}: role="alert" outside ErrorNote')
            for match in CLASSNAME.finditer(line):
                classes = next(g for g in match.groups() if g is not None)
                if FAILURE_COLOUR.search(classes) and TYPE_SIZE.search(classes):
                    sentences[rel] += 1
                    where.setdefault(rel, []).append(str(number))

    # Each row carries its reason: a baseline that only counts teaches the next
    # reader nothing about why the shape beneath it is a different shape.
    raw = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    baseline: dict[str, int] = {
        rel: (entry["count"] if isinstance(entry, dict) else entry) for rel, entry in raw.items()
    }
    over = {
        rel: (count, baseline.get(rel, 0))
        for rel, count in sentences.items()
        if count > baseline.get(rel, 0)
    }

    if not hand_rolled and not over:
        total = sum(sentences.values())
        print(
            f"failure voice OK — no hand-rolled alert; {total} coloured sentence(s) "
            f"in {len(sentences)} file(s) (baseline {sum(baseline.values())})"
        )
        return 0

    for finding in hand_rolled:
        print(f"  {finding}")
    for rel, (count, allowed) in sorted(over.items()):
        print(f"  {rel}: {count} coloured sentence(s), baseline {allowed} — lines {', '.join(where[rel])}")
    print(
        "\nUse `ErrorNote` from @/components/shell for a failure next to the thing"
        "\nthat failed. It presets ui/alert.tsx, so it brings the border, the colour,"
        "\nrole=\"alert\" and the verbatim mark. A"
        "\nfailure that needs a heading is the same note with a `title`. A validation message,"
        "\na full-bleed banner and a <pre> of stderr are different shapes — if this is"
        "\none of those, say so in the baseline."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
