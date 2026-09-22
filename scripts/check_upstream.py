"""A vendored component is upstream's file, or it is the product's — never both.

`frontend/src/components/ui/` is the shadcn `base-nova` style and
`frontend/src/components/ai-elements/` is the AI SDK's elements registry. Both
arrive by `shadcn add`, and both are re-vendored wholesale rather than patched:
a product decision edited INTO one of those files is deleted by the next
re-vendor, and nothing on the way in marks which lines came from this
repository. That is the failure this check exists to stop. A product decision
belongs in a token, in `styles.css`, or in a wrapper that composes the vendored
component — the hues `ai-elements/tool.tsx` paints its states in are answered in
`@theme inline`, not in the file.

Read `docs/maintainers/upstream-drift-ledger.md` first; it holds the contract,
the one allowed drift class and the token decisions. This file enforces it.

WHAT IT COMPARES AGAINST, and why not the registry JSON

`https://ui.shadcn.com/r/styles/base-nova/<name>.json` is not comparable to what
is on disk. Five transforms sit between them, each driven by a setting in
`frontend/components.json`, and one of them reprints the file:

  1. `aliases`      `@/registry/base-nova/lib/utils` becomes `@/lib/utils`.
  2. `rsc: false`   a leading `"use client"` directive is dropped.
  3. `iconLibrary`  `<IconPlaceholder lucide="XIcon" tabler=… />` becomes
                    `<XIcon />` plus the `lucide-react` import.
  4. `rtl` / `menuColor` / `menuAccent`
                    the registry's `cn-*` marker classes are resolved:
                    `cn-font-heading` becomes `font-heading`, `cn-rtl-flip` and
                    `cn-menu-translucent` are dropped outright.
  5. base-ui        `<X asChild><button/></X>` becomes `<X render={<button/>}/>`.

Reimplementing those in Python would be re-deriving the CLI, and 3 through 5
reprint the syntax tree, so a byte comparison against a hand-normalised registry
string can never come clean. The CLI is the authority on its own transforms, so
this check asks IT: `shadcn add <item> --view <path>` prints the file the CLI
would write, transformed, without writing anything.

WHAT IS NORMALISED, AND THE ONE UPSTREAM DEFECT BEHIND IT

Both sides are reduced before comparing, and only in these three ways:

  · A leading `use client` directive is dropped from both. It has no meaning in
    a Vite SPA — but it cannot be compared either, because the CLI's own strip
    is ORDER-DEPENDENT. `shadcn/dist/chunk-BKSE3RKO.js` holds
    `var $c = /^["']use client["']$/g` and calls `$c.test(...)` once per file:
    `test` on a `/g` regex advances `lastIndex`, so the directive survives in
    every other file the same process touches. Demonstrated:
    `shadcn add tooltip --view …` strips it, `shadcn add collapsible tooltip
    --view …` keeps it, same file, same registry, same CLI.
  · A `// DRIFT(base-ui)` comment is dropped from the local side. It is a
    marker, not code.
  · Whitespace OUTSIDE string literals is removed from both. The CLI writes its
    rewritten JSX on one line and prettier writes it over five; neither is a
    product decision. String contents are compared verbatim, so a class list
    that loses a space is still caught.

Anything left after that is a real difference, and it fails — including a
difference upstream introduced, which is the signal to re-vendor rather than to
edit. The DRIFT table below is the only escape hatch. It is content-addressed
rather than a count, so an entry whose upstream half stops matching fails as
loudly as an unexplained diff: a stale allowance is how a baseline stops meaning
anything.

Needs the network. A registry that cannot be reached fails the check — there is
no result to report otherwise, and "could not ask" must never read as "nothing
changed". Not wired into `make test-web` for that reason; it is `make
check-upstream`, run when re-vendoring and when upstream moves.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND = REPO_ROOT / "frontend"
UI_DIR = FRONTEND / "src/components/ui"
AI_DIR = FRONTEND / "src/components/ai-elements"
SHADCN = FRONTEND / "node_modules/.bin/shadcn"
NODE_TOOLCHAIN = REPO_ROOT / "scripts/node-toolchain.py"

# `shadcn add --view` frames the file it would write. The frame is two box
# characters and two spaces; everything after them is the file, verbatim.
VIEW_PREFIX = "│ │ "

USE_CLIENT = re.compile(r"""^\s*["']use client["'];?[ \t]*\r?\n""")
DRIFT_MARKER = re.compile(r"//\s*DRIFT\(base-ui\)[^\n]*")


@dataclass(frozen=True)
class Vendored:
    """One file this check owns: where it lives, and how the CLI names it."""

    path: Path
    item: str

    @property
    def rel(self) -> str:
        return str(self.path.relative_to(FRONTEND))

    @property
    def label(self) -> str:
        return str(self.path.relative_to(REPO_ROOT))


# The one allowed drift class, spelled out per site rather than per file.
#
# The CLI's base-ui codemod rewrites `asChild` only where the child is a JSX
# ELEMENT it can lift into `render=`. Where the child is an expression, the
# codemod drops `asChild` and leaves the child inside — and a Base UI trigger
# then renders its own <button> around the composed one, so the control nests
# inside a control and a screen reader announces both. The `render=` the codemod
# could not write is hand-written at those sites and marked. Each entry is
# (upstream, local, why); both halves are matched after normalisation, so an
# upstream change to the site fails rather than silently re-allowing something
# else.
#
# Two shapes here are not codemod gaps, and say so. `ui/calendar.tsx` attaches a
# ref the registry file declares and never uses — an upstream defect. The six
# `ai-elements/prompt-input.tsx` entries after the first port Radix-shaped API
# surfaces the registry still ships: handler signatures typed against a native
# Event, and a hover card delay that Base UI takes on the trigger rather than on
# the root. Both are declared here for the same reason the codemod gaps are —
# the marker comment is normalised away, the code it marks is not — and the
# whole class goes when upstream ships Base UI sources for these elements.
ALLOWED_DRIFT: dict[str, list[tuple[str, str, str]]] = {
    "ui/combobox.tsx": [
        (
            "  showTrigger = true,\n  showClear = false,\n  ...props\n}: "
            "ComboboxPrimitive.Input.Props & {\n    showTrigger?: boolean\n"
            "  showClear?: boolean\n}) {",
            "  showTrigger = true,\n  showClear = false,\n  triggerLabel,\n"
            "  ...props\n}: ComboboxPrimitive.Input.Props & {\n"
            "  showTrigger?: boolean\n  showClear?: boolean\n"
            "  triggerLabel?: string\n}) {",
            "the chevron ComboboxInput renders is a real focusable button with "
            "no text; Base UI names it from its own Field context, which this "
            "codebase does not use, and the registry routes {...props} to the "
            "input rather than the trigger, so no call site or wrapper can "
            "reach it (axe button-name, critical, measured on a record page)",
        ),
        (
            '            render={<ComboboxTrigger />}\n'
            '            data-slot="input-group-button"',
            '            render={<ComboboxTrigger />}\n'
            '            aria-label={triggerLabel}\n'
            '            data-slot="input-group-button"',
            "the other half of the same edit: the accepted label has to land "
            "on the trigger button itself",
        ),
    ],
    "ui/calendar.tsx": [
        (
            '<Button variant="ghost" size="icon" '
            "data-day={day.date.toLocaleDateString(locale?.code)}",
            '<Button ref={ref} variant="ghost" size="icon" '
            "data-day={day.date.toLocaleDateString(locale?.code)}",
            "CalendarDayButton declares a ref and focuses it whenever "
            "modifiers.focused turns on, but the registry file attaches it to "
            "nothing and react-day-picker passes DayButton no ref of its own, "
            "so the effect reads null and keyboard focus never reaches the "
            "focused day",
        ),
    ],
    "ai-elements/message.tsx": [
        (
            "<TooltipTrigger>{button}</TooltipTrigger>",
            "<TooltipTrigger render={button} />",
            "MessageAction composes its own Button and hands it to the trigger as "
            "an expression child, which the codemod cannot lift into render=, so "
            "the Base UI trigger would render a second button around it",
        ),
    ],
    "ai-elements/prompt-input.tsx": [
        (
            '<InputGroup className="overflow-hidden">',
            '<InputGroup className="h-auto overflow-hidden">',
            "InputGroup's own escape from its fixed h-8 is has-[>textarea], which "
            "sees a DIRECT textarea child only; this composer nests its textarea "
            "one level down, so the primitive's height stays declared and inert. "
            "The override is a prop passed inside a vendored file, which no token, "
            "stylesheet rule or wrapper can reach",
        ),
        (
            "<TooltipTrigger>{button}</TooltipTrigger>",
            "<TooltipTrigger render={button} />",
            "the codemod cannot lift an expression child into render=, and the "
            "trigger would otherwise wrap the composed button in its own",
        ),
        (
            'import { cn } from "@/lib/utils";',
            'import { cn } from "@/lib/utils";'
            'import type { BaseUIEvent } from "@base-ui/react/types";',
            "the four handler signatures below name the event type Base UI calls "
            "them with, and nothing else in the file imports it",
        ),
        (
            "<DropdownMenuItem {...props} onSelect={handleSelect}> "
            '<ImageIcon className="mr-2 size-4" /> {label}',
            "<DropdownMenuItem {...props} onClick={handleSelect}> "
            '<ImageIcon className="mr-2 size-4" /> {label}',
            "Base UI menu items have no onSelect; what typechecked was React's "
            "DOM select handler, which fires on text selection and never on "
            "activation, so the add-attachments item opened nothing",
        ),
        (
            "<DropdownMenuItem {...props} onSelect={handleSelect}> "
            '<Monitor className="mr-2 size-4" />',
            "<DropdownMenuItem {...props} onClick={handleSelect}> "
            '<Monitor className="mr-2 size-4" />',
            "same port for the screenshot item; the public onSelect prop keeps "
            "upstream's name and is still called from the handler",
        ),
        (
            "(e: Event) => { e.preventDefault(); attachments.openFileDialog();",
            "(e: BaseUIEvent<React.SyntheticEvent<HTMLDivElement>>) => { "
            "e.preventDefault(); attachments.openFileDialog();",
            "Radix hands onSelect a native Event; the Base UI menu item is a div "
            "whose onSelect is React's, wrapped in BaseUIEvent",
        ),
        (
            "async (event: Event) => { onSelect?.(event);",
            "async (event: BaseUIEvent<React.SyntheticEvent<HTMLDivElement>>) => "
            "{ onSelect?.(event);",
            "the same event, and it is passed straight back to the onSelect this "
            "component takes as a prop, so the two types have to agree",
        ),
        (
            "(e: React.MouseEvent<HTMLButtonElement>) => { if (isGenerating && onStop) {",
            "(e: BaseUIEvent<React.MouseEvent<HTMLButtonElement>>) => { "
            "if (isGenerating && onStop) {",
            "the submit control is a Base UI button, so the click it forwards to "
            "the caller's onClick carries preventBaseUIHandler",
        ),
        (
            "export const PromptInputHoverCard = ({ openDelay = 0, closeDelay = 0, "
            "...props }: PromptInputHoverCardProps) => ( <HoverCard "
            "closeDelay={closeDelay} openDelay={openDelay} {...props} /> ); "
            "export type PromptInputHoverCardTriggerProps = ComponentProps< "
            "typeof HoverCardTrigger >; export const PromptInputHoverCardTrigger "
            "= ( props: PromptInputHoverCardTriggerProps ) => <HoverCardTrigger "
            "{...props} />;",
            "export const PromptInputHoverCard = (props: PromptInputHoverCardProps) "
            "=> ( <HoverCard {...props} /> ); "
            "export type PromptInputHoverCardTriggerProps = ComponentProps< "
            "typeof HoverCardTrigger >; export const PromptInputHoverCardTrigger "
            "= ({ closeDelay = 0, delay = 0, ...props }: "
            "PromptInputHoverCardTriggerProps) => ( <HoverCardTrigger "
            "closeDelay={closeDelay} delay={delay} {...props} /> );",
            "Radix takes openDelay/closeDelay on the hover card root, Base UI "
            "takes delay/closeDelay on the trigger; upstream's decision is that "
            "both are 0, and it is kept at the part that accepts it rather than "
            "dropped on the floor",
        ),
    ],
    "ai-elements/plan.tsx": [
        (
            '<CollapsibleTrigger render={<Button className={cn("size-8", className)} '
            'data-slot="plan-trigger" size="icon" variant="ghost" {...props} />}>',
            '<CollapsibleTrigger {...props} render={<Button className={cn("size-8", className)} '
            'data-slot="plan-trigger" size="icon" variant="ghost" />}>',
            "props belong to the trigger, which merges them into whatever it "
            "renders; leaving them on the render element drops the ones Base UI "
            "sets itself",
        ),
        (
            "<CollapsibleContent render={<CardContent "
            'data-slot="plan-content" {...props} />}></CollapsibleContent>',
            "<CollapsibleContent render={<CardContent "
            'data-slot="plan-content" {...props} />} />',
            "the codemod leaves an empty element pair where it lifted the child",
        ),
    ],
}


def targets() -> list[Vendored]:
    found = [Vendored(p, p.stem) for p in sorted(UI_DIR.glob("*.tsx"))]
    found += [Vendored(p, f"@ai-elements/{p.stem}") for p in sorted(AI_DIR.glob("*.tsx"))]
    return found


def node_env() -> dict[str, str]:
    """PATH with the repository's pinned Node in front of it.

    Never the interactive shell's node: `.nvmrc` pins one release and every
    other gate here enters it the same way.
    """
    proc = subprocess.run(
        [sys.executable, str(NODE_TOOLCHAIN), "--print-bin"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"could not select the pinned Node toolchain:\n{proc.stderr.strip()}")
    bin_dir = proc.stdout.strip()
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["NO_COLOR"] = "1"
    return env


def registry_view(target: Vendored, env: dict[str, str]) -> str:
    """The file the CLI would write for this item, transformed and not written."""
    proc = subprocess.run(
        [str(SHADCN), "add", target.item, "--view", target.rel],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    output = proc.stdout + proc.stderr
    body = "\n".join(
        line[len(VIEW_PREFIX) :] for line in output.splitlines() if line.startswith(VIEW_PREFIX)
    )
    # Loud, not silent: an unreachable registry, a renamed item and a CLI that
    # changed its output format all land here, and none of them is a pass.
    if proc.returncode != 0:
        raise RuntimeError(f"{target.item}: `shadcn add --view` exited {proc.returncode}\n{output}")
    if not body.strip():
        raise RuntimeError(f"{target.item}: the registry returned no file for {target.rel}\n{output}")
    return body


def strip_outside_strings(text: str) -> str:
    """Drop every whitespace character that is not inside a string literal.

    `"` and a backtick open a literal; `'` does not, because in these files it
    only ever appears inside one (`[&_svg:not([class*='size-'])]`). Keeping
    literal contents verbatim is what makes a class list that lost a space read
    as a difference rather than as formatting.
    """
    out: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in text:
        if quote is not None:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in '"`':
            quote = ch
            out.append(ch)
            continue
        if ch.isspace():
            continue
        out.append(ch)
    return "".join(out)


def normalise(text: str) -> str:
    return strip_outside_strings(DRIFT_MARKER.sub("", USE_CLIENT.sub("", text, count=1)))


def apply_allowed_drift(label: str, upstream: str) -> tuple[str, list[str]]:
    """Rewrite the upstream side at each declared site; report any that is stale."""
    stale: list[str] = []
    for want, ours, why in ALLOWED_DRIFT.get(label, []):
        a, b = strip_outside_strings(want), strip_outside_strings(ours)
        if a not in upstream:
            stale.append(f"DRIFT no longer applies — upstream has moved off {want!r} ({why})")
            continue
        upstream = upstream.replace(a, b, 1)
    return upstream, stale


def first_difference(a: str, b: str, width: int = 90) -> str:
    i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    lo = max(0, i - width // 3)
    return f"      upstream …{a[lo : lo + width]}…\n      ours     …{b[lo : lo + width]}…"


def self_check() -> str | None:
    """Prove the comparison fails on a planted edit, so a clean run means something."""
    upstream = 'const a = cn(\n  "flex items-center gap-2",\n  className\n)\n'
    reformatted = 'const a = cn("flex items-center gap-2", className)\n'
    if normalise(upstream) != normalise(reformatted):
        return "reformatting alone reads as a difference; the check would never be green"
    for planted, what in (
        ('const a = cn("flex items-center gap-3", className)\n', "a changed class"),
        ('const a = cn("flex items-centergap-2", className)\n', "a class list that lost a space"),
        ('const a = cn("flex items-center gap-2", other)\n', "a changed identifier"),
    ):
        if normalise(upstream) == normalise(planted):
            return f"{what} is not detected"
    if normalise('"use client"\nconst a = 1\n') != normalise("const a = 1\n"):
        return "the use-client directive is not normalised away"
    if normalise("<X render={y} // DRIFT(base-ui): why\n>\n") != normalise("<X render={y}>\n"):
        return "the DRIFT marker is not normalised away"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="check one file by stem, e.g. --only button")
    args = parser.parse_args()

    broken = self_check()
    if broken:
        print(f"check_upstream.py is not working: {broken}", file=sys.stderr)
        return 2

    checked = [t for t in targets() if not args.only or t.path.stem == args.only]
    if not checked:
        print(f"no vendored file matches --only {args.only!r}", file=sys.stderr)
        return 2

    try:
        env = node_env()
        # One CLI process per item, eight at a time: each spends its three
        # seconds waiting on a registry, and serially that is three minutes
        # nobody runs.
        with ThreadPoolExecutor(max_workers=8) as pool:
            views = list(pool.map(lambda t: registry_view(t, env), checked))
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        # A registry this check could not reach is not a registry that agrees
        # with the tree. Exit 2 rather than 1: nothing was compared.
        print(f"could not read the registries, so nothing was checked:\n{exc}", file=sys.stderr)
        return 2

    findings: list[str] = []
    for target, view in zip(checked, views):
        upstream = normalise(view)
        ours = normalise(target.path.read_text(encoding="utf-8"))
        label = str(target.path.relative_to(FRONTEND / "src/components"))
        upstream, stale = apply_allowed_drift(label, upstream)
        for note in stale:
            findings.append(f"  - {target.label}: {note}")
        if upstream != ours:
            findings.append(
                f"  - {target.label}: differs from `{target.item}` in the registry\n"
                f"{first_difference(upstream, ours)}"
            )

    if findings:
        print(
            "Vendored files have drifted from their registries. The fix is to re-vendor the\n"
            "file and move the product decision into a token, `styles.css` or a wrapper —\n"
            "never to edit the vendored file:",
            file=sys.stderr,
        )
        print("\n".join(findings), file=sys.stderr)
        return 1

    allowed = sum(len(v) for v in ALLOWED_DRIFT.values())
    print(
        f"upstream OK — {len(checked)} vendored files match their registry"
        f" ({allowed} declared DRIFT(base-ui) sites)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
