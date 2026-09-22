"""Backend-neutral collection schema vocabulary.

Modules here describe *what the stored documents look like* (field names,
per-writer field ownership, watermark fields) — never how any backend stores
them. Both the DAL repositories (enforcement at the write boundary) and the
service layer (writers) import from here, so the dependency arrow always
points downward: core → common, never common → core.
"""
