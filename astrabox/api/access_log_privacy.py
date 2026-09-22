"""Mask capability tokens out of the uvicorn access log.

The sandbox→backend transcript capability token travels in the URL path
(``/api/v1/sbxcap/{token}/…`` — the sandbox side has no header-injection hook,
so the path is the only channel; see
:mod:`astrabox.core.service.orchestrator.transcript_capability`). Uvicorn's
default access log prints the request line verbatim, which would persist a
live credential into whatever collects stdout. Astrabox owns its uvicorn
entrypoints, so instead of asking every operator to configure log exclusions,
:func:`install_access_log_token_mask` attaches a :class:`logging.Filter` to
the ``uvicorn.access`` logger that rewrites the token segment to ``***``
before the record is emitted.

This covers the uvicorn access log in this process only. A TLS terminator or
reverse proxy in front keeps its own access log — the operator note in
``transcript_capability`` still applies there.
"""

from __future__ import annotations

import logging
import re
from typing import Any

__all__ = ["install_access_log_token_mask", "mask_capability_tokens"]

_FILTER_MARKER_ATTR = "_astrabox_sbxcap_mask"

#: The token is the single path segment after the sbxcap prefix. Everything up
#: to the next ``/`` (or whitespace / quote, for a bare-prefix request line) is
#: credential material; the trailing subpath is routing, not secret.
_SBXCAP_TOKEN_RE = re.compile(r"(/api/v1/sbxcap/)[^/\s\"']+")


def mask_capability_tokens(text: str) -> str:
    """Return ``text`` with every sbxcap token segment replaced by ``***``."""
    return _SBXCAP_TOKEN_RE.sub(r"\1***", text)


class _SbxcapTokenMaskFilter(logging.Filter):
    """Rewrite sbxcap tokens in access-log records; never drop a record.

    Uvicorn's access records carry the request line pieces in ``record.args``
    (``client_addr, method, full_path, http_version, status``), but this
    rewrites every string field — arg tuples and ``msg`` itself — so a custom
    ``log_config`` with a different format string stays covered.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str) and "/api/v1/sbxcap/" in record.msg:
                record.msg = mask_capability_tokens(record.msg)
            args = record.args
            if isinstance(args, tuple):
                record.args = tuple(self._mask_value(value) for value in args)
            elif isinstance(args, dict):
                record.args = {
                    key: self._mask_value(value) for key, value in args.items()
                }
        except Exception:  # noqa: BLE001 - a masking bug must not kill logging
            pass
        return True

    @staticmethod
    def _mask_value(value: Any) -> Any:
        if isinstance(value, str) and "/api/v1/sbxcap/" in value:
            return mask_capability_tokens(value)
        return value


def install_access_log_token_mask() -> None:
    """Attach the mask filter to ``uvicorn.access`` (idempotent).

    Called from ``create_app`` so every launch path — the ``astrabox`` CLI,
    ``python -m astrabox``, or an operator's own ``uvicorn …:APP_FACTORY
    --factory`` — is covered without touching uvicorn config. Installing
    before uvicorn configures that logger is fine: filters live on the logger
    object, which ``logging.getLogger`` creates on first reference, and
    uvicorn's dictConfig does not replace attached filters.
    """
    access_logger = logging.getLogger("uvicorn.access")
    for existing in access_logger.filters:
        if getattr(existing, _FILTER_MARKER_ATTR, False):
            return
    mask_filter = _SbxcapTokenMaskFilter()
    setattr(mask_filter, _FILTER_MARKER_ATTR, True)
    access_logger.addFilter(mask_filter)
