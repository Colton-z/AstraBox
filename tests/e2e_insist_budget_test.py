"""`insist` must actually ask again, and its callers must be able to afford it.

Two exclusive specs failed as "the model declined" when the second ask had
simply never happened: the gated Write in chat-permission-modes and the held
live turn in hibernate-wake-noop-during-live-turn. Both passed the natural
`budgetMs: probeMs * 2`, and the guard required a WHOLE further `probeMs` to
remain — which a probe that runs to its own deadline never leaves. So "one, and
one more" was unreachable in exactly the case it exists for, and the failure
message blamed the model.

These are source assertions. `tests/e2e-ui/fixtures/` has no unit harness — it
is typechecked, not executed, outside a live lane — so what is pinned here is
the shape of the arithmetic, not its behaviour. The behaviour is proved by the
lane.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSIST = REPO / "tests/e2e-ui/fixtures/insist.ts"
SPECS = REPO / "tests/e2e-ui/specs"


def test_the_guard_stops_on_an_exhausted_budget_not_on_a_short_one() -> None:
    """`now + probeMs > deadline` is the form that starved the second ask."""
    source = INSIST.read_text(encoding="utf-8")
    assert "Date.now() >= deadline" in source
    assert "Date.now() + options.probeMs > deadline" not in source, (
        "requiring room for a whole further probe makes the retry unreachable"
    )


SCALED = re.compile(
    r"budgetMs:\s*(?P<budget>[A-Z_\d]+)\s*\*\s*(?P<factor>\d+)\s*,[^\n]*\n\s*probeMs:\s*(?P<probe>[A-Z_\d]+)\s*,",
)
LITERAL = re.compile(
    r"budgetMs:\s*(?P<budget>[\d_]+)\s*,[^\n]*\n\s*probeMs:\s*(?P<probe>[\d_]+)\s*,",
)


def test_every_caller_leaves_room_for_the_ask_it_asked_for() -> None:
    """A budget under two probes is a caller that only ever asks once.

    Both spellings, because the property is about the numbers and not about how
    they are written: a caller that names its milliseconds inline escapes a
    check that only understands `CONST * N`, which is how the one spec whose
    probe ate its own retry budget went unexamined.

    Named per spec rather than aggregated: when this fails, the reader needs to
    know which spec quietly lost its retry, not that some spec did.
    """
    seen = 0
    for path in sorted(SPECS.glob("*.ts")):
        source = path.read_text(encoding="utf-8")
        for match in SCALED.finditer(source):
            seen += 1
            assert match["budget"] == match["probe"], (
                f"{path.name}: budget and probe must be the same measure to compare"
            )
            assert int(match["factor"]) >= 2, (
                f"{path.name}: a budget of one probe can never fund a second ask"
            )
        for match in LITERAL.finditer(source):
            seen += 1
            budget = int(match["budget"].replace("_", ""))
            probe = int(match["probe"].replace("_", ""))
            assert budget >= probe * 2, (
                f"{path.name}: budgetMs={budget} funds only {budget // probe} "
                f"probe(s) of {probe}ms, so the retry never happens"
            )
    assert seen >= 3, "the callers this pins should not have disappeared silently"


def test_insist_still_says_how_many_times_it_asked() -> None:
    """The count is what exposed this: "asked 1 time(s)" against attempts=2."""
    source = INSIST.read_text(encoding="utf-8")
    assert "asked ${declined.length} time(s)" in source
