"""Write the bundle's own patch layer with its package names resolved to files.

The harness resolves a bare plugin name from the profile directory under DSH_HOME,
which exists only once a box has booted; the image instead names the bundle's
entry points by file, as its own overlay does for AstraBox's plugins.
"""
import json
import re
import sys
root, out = sys.argv[1], sys.argv[2]
pkg = json.load(open(f"{root}/package.json"))
exports = pkg["exports"]
def target(spec):
    sub = "." + spec[len(pkg["name"]):]
    entry = exports[sub]
    path = entry["default"] if isinstance(entry, dict) else entry
    return f"file://{root}/{path[2:]}"
text = open(f"{root}/cordis.patch.yml").read()
pattern = re.compile(r"^(\s*name:\s*)(" + re.escape(pkg["name"]) + r"(?:/[\w-]+)?)\s*$", re.M)
resolved = pattern.sub(lambda m: m.group(1) + target(m.group(2)), text)
if pattern.search(resolved):
    sys.exit("unresolved bundle name left in the layer")
open(out, "w").write(resolved)
