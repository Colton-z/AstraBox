"""Model base URLs for engines that speak the OpenAI wire.

Such an engine is configured with an API root and appends the endpoint path
itself (``chat/completions``). :func:`resolve_openai_base_url` turns a model
connection's base URL into that root, and refuses one that names an
Anthropic-compatible route, which an engine on this wire cannot speak.
"""

from __future__ import annotations

from astrabox.common.utils.errors import APIError
from astrabox.seams.model import ResolvedModelAccess


def resolve_openai_base_url(
    model_access: ResolvedModelAccess, *, engine_label: str, error_code: str
) -> str:
    """The model connection's base URL as an OpenAI-wire client's API root.

    Surrounding whitespace and trailing slashes are removed, and so is one
    endpoint path the client appends itself (``/chat/completions`` or
    ``/responses``, in any letter case). An unset base URL returns ``""``;
    whether that is an error is the caller's to say.

    Raises ``error_code`` (500), naming ``engine_label``, when the URL is an
    Anthropic-compatible route (``/api/anthropic``).
    """

    base_url = _normalize_openai_base_url(model_access.base_url or "")
    if _looks_anthropic_compatible_base_url(base_url):
        raise APIError(
            code=error_code,
            message=(
                f"{engine_label} requires an OpenAI-compatible model base URL; the "
                "RuntimeProfile's model connection names an Anthropic-compatible one"
            ),
            status_code=500,
        )
    return base_url


def _normalize_openai_base_url(raw_value: str) -> str:
    value = str(raw_value or "").strip().rstrip("/")
    if not value:
        return ""
    lowered = value.lower()
    for suffix in ("/chat/completions", "/responses"):
        if lowered.endswith(suffix):
            value = value[: -len(suffix)].rstrip("/")
            break
    return value


def _looks_anthropic_compatible_base_url(value: str) -> bool:
    lowered = str(value or "").strip().lower().rstrip("/")
    return lowered.endswith("/api/anthropic") or "/api/anthropic/" in lowered
