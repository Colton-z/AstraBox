#!/usr/bin/env python3
"""Check that the committed API client is what the OpenAPI snapshot generates.

``frontend/src/api/schema.d.ts`` is generated from
``tests/data/openapi_snapshot.json`` and committed, so the console typechecks and
builds without running a code generator. A checked-in generated file is only
worth anything while it still matches its input: edited by hand, or left behind
by a route whose response model moved, it describes an API the server does not
serve, and nothing fails — a file that has drifted compiles exactly as well as
one that has not. Regenerating into a temporary directory and comparing the
bytes is what turns that into a failing build.

This is deliberately a comparison and not a rewrite-in-place. Silently
regenerating would make the check always pass and move the divergence into a
diff nobody asked for; the failure names both files and the one command that
fixes it.

The generator lives in its own npm package (``scripts/api-codegen``) rather than
in ``frontend``: openapi-typescript emits through the TypeScript compiler API,
which the console's typescript@7 — the Go port — does not ship at all, and npm
will not nest a conflicting peer dependency under the package that asks for it.
Turning peer resolution off across the console's whole tree to make one tool fit
would have silenced every future conflict too, so the tool gets the typescript@5
it asks for, next to nothing else.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CODEGEN_DIR = REPO_ROOT / "scripts" / "api-codegen"
SNAPSHOT = REPO_ROOT / "tests" / "data" / "openapi_snapshot.json"
COMMITTED_CLIENT = REPO_ROOT / "frontend" / "src" / "api" / "schema.d.ts"
GENERATOR_CLI = CODEGEN_DIR / "node_modules" / "openapi-typescript" / "bin" / "cli.js"
NODE_TOOLCHAIN = REPO_ROOT / "scripts" / "node-toolchain.py"

_REGENERATE = "python3 scripts/node-toolchain.py npm --prefix scripts/api-codegen run gen"


def _fail(message: str) -> int:
    print(f"check-api-client: {message}", file=sys.stderr)
    return 1


def main() -> int:
    for required in (SNAPSHOT, COMMITTED_CLIENT):
        if not required.is_file():
            return _fail(f"{required.relative_to(REPO_ROOT)} is missing")

    if not GENERATOR_CLI.is_file():
        return _fail(
            f"the generator is not installed — run `npm --prefix "
            f"{CODEGEN_DIR.relative_to(REPO_ROOT)} ci` (this is what `make install-web` does)"
        )

    with tempfile.TemporaryDirectory() as workdir:
        regenerated = Path(workdir) / "schema.d.ts"
        # Run the CLI directly rather than through `npm run`, so the comparison
        # cannot be answered by a script that writes somewhere else: the output
        # path is this check's, and the input is the committed snapshot.
        result = subprocess.run(
            [
                sys.executable,
                str(NODE_TOOLCHAIN),
                "node",
                str(GENERATOR_CLI),
                str(SNAPSHOT),
                "-o",
                str(regenerated),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "NO_COLOR": "1"},
        )
        if result.returncode != 0:
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return _fail("the generator failed; its output is above")
        if not regenerated.is_file():
            return _fail("the generator reported success but wrote no file")

        if not filecmp.cmp(regenerated, COMMITTED_CLIENT, shallow=False):
            fresh = regenerated.read_text(encoding="utf-8").splitlines()
            committed = COMMITTED_CLIENT.read_text(encoding="utf-8").splitlines()
            print(
                "check-api-client: "
                f"{COMMITTED_CLIENT.relative_to(REPO_ROOT)} is not what "
                f"{SNAPSHOT.relative_to(REPO_ROOT)} generates.\n"
                f"  committed:   {len(committed)} lines\n"
                f"  regenerated: {len(fresh)} lines\n"
                f"Run `{_REGENERATE}` and commit the result. If the snapshot itself "
                "moved, regenerate it first (tests/http_wire_contract_test.py "
                "documents how) so the client is generated from the current tree.",
                file=sys.stderr,
            )
            for index, (a, b) in enumerate(zip(committed, fresh), start=1):
                if a != b:
                    print(f"  first difference at line {index}:", file=sys.stderr)
                    print(f"    committed:   {a}", file=sys.stderr)
                    print(f"    regenerated: {b}", file=sys.stderr)
                    break
            return 1

    print(
        f"api client OK — {COMMITTED_CLIENT.relative_to(REPO_ROOT)} "
        f"({len(COMMITTED_CLIENT.read_text(encoding='utf-8').splitlines())} lines) "
        f"regenerates byte-identically from {SNAPSHOT.relative_to(REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    if shutil.which("node") is None and not NODE_TOOLCHAIN.is_file():
        raise SystemExit(_fail("no node toolchain available"))
    raise SystemExit(main())
