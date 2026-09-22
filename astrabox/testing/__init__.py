"""Test-support code that ships with the package but is never product logic.

Modules here back the live E2E harness (fault injection against a real
server). Nothing under ``astrabox.testing`` may be imported by production
modules — the app wires these in at startup behind explicit env flags, and
the import direction stays testing → (common, core), never the reverse.
"""
