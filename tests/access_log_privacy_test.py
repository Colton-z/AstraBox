"""Access-log token mask — the sbxcap capability token must not reach logs.

The transcript capability token rides in the URL path (the sandbox side has no
header hook), and uvicorn's access log prints the request line verbatim —
which would persist a live per-session credential into stdout collection.
:mod:`astrabox.api.access_log_privacy` attaches a filter to ``uvicorn.access``
that masks the token segment; these tests pin the mask itself, the
record-rewrite shape uvicorn actually emits, and installer idempotency.
"""

from __future__ import annotations

import logging

from astrabox.api.access_log_privacy import (
    install_access_log_token_mask,
    mask_capability_tokens,
)


def test_mask_replaces_the_token_segment_only() -> None:
    line = (
        '"POST /api/v1/sbxcap/0a1b2c3d4e5f60718293a4b5c6d7e8f9/transcript/append '
        'HTTP/1.1" 200'
    )
    masked = mask_capability_tokens(line)
    assert "0a1b2c3d4e5f60718293a4b5c6d7e8f9" not in masked
    assert "/api/v1/sbxcap/***/transcript/append" in masked, (
        "the subpath after the token is routing, not secret — it must survive"
    )


def test_mask_handles_bare_prefix_and_foreign_paths() -> None:
    # Token as the LAST segment (no trailing subpath): still masked.
    assert mask_capability_tokens("GET /api/v1/sbxcap/deadbeef HTTP/1.1").endswith(
        "/api/v1/sbxcap/*** HTTP/1.1"
    )
    # Unrelated paths pass through byte-identical.
    other = 'GET /api/v1/sessions/sess-1/messages HTTP/1.1'
    assert mask_capability_tokens(other) == other


def test_filter_rewrites_uvicorn_access_record_args() -> None:
    install_access_log_token_mask()
    access_logger = logging.getLogger("uvicorn.access")
    record = access_logger.makeRecord(
        name="uvicorn.access",
        level=logging.INFO,
        fn="", lno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        # The default uvicorn access-log arg shape.
        args=("127.0.0.1:5", "POST", "/api/v1/sbxcap/feedc0ffee/transcript/load", "1.1", 200),
        exc_info=None,
    )
    for f in access_logger.filters:
        f.filter(record)
    rendered = record.getMessage()
    assert "feedc0ffee" not in rendered
    assert "/api/v1/sbxcap/***/transcript/load" in rendered


def test_install_is_idempotent() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    before = list(access_logger.filters)
    install_access_log_token_mask()
    install_access_log_token_mask()
    added = [f for f in access_logger.filters if f not in before]
    marked = [
        f for f in access_logger.filters
        if getattr(f, "_astrabox_sbxcap_mask", False)
    ]
    assert len(marked) == 1, f"expected exactly one mask filter, got {len(marked)}"
    assert len(added) <= 1
