"""Read one turn input that is text, or text and images together.

A user message can carry more than prose: pasting a slide or a screenshot
puts an image on the clipboard next to its text, and the model is meant to
see both. The platform validates one small content-block vocabulary; each
engine adapter then declares which types its vendor input protocol consumes
and owns the conversion into that protocol.

Everything between the API and the engine that only needs to *identify* an
input — the durable command row, the FIFO consumption boundary, titles,
logs — keeps using a string. :func:`read_turn_input_content` returns both:
the blocks the engine gets, and the text projection those layers read. The
blocks are returned only when they say something a string cannot, so a
plain text turn stays exactly the row it has always been.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.errors import APIError

#: The image encodings the engine's own content block accepts. A source the
#: vendor cannot read is refused here, where the caller still has a request to
#: answer, rather than in the box where it would surface as a failed turn.
_SUPPORTED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

#: The closed vocabulary accepted by the turn-input API. A live engine
#: manifest may declare a subset, but never a type this boundary cannot parse.
TURN_INPUT_CONTENT_TYPES = frozenset({"text", "image"})


def _invalid(message: str) -> APIError:
    return APIError(code="INVALID_REQUEST", message=message, status_code=400)


def _read_text_block(block: dict[str, Any]) -> dict[str, Any]:
    text = block.get("text")
    if not isinstance(text, str):
        raise _invalid("a text content block needs a string text")
    return {"type": "text", "text": text}


def _read_image_block(block: dict[str, Any]) -> dict[str, Any]:
    source = block.get("source")
    if not isinstance(source, dict):
        raise _invalid("an image content block needs a source object")
    if str(source.get("type") or "") != "base64":
        raise _invalid("an image content block source must be base64")
    media_type = source.get("media_type")
    data = source.get("data")
    if not isinstance(media_type, str) or media_type not in _SUPPORTED_IMAGE_MEDIA_TYPES:
        supported = ", ".join(sorted(_SUPPORTED_IMAGE_MEDIA_TYPES))
        raise _invalid(f"unsupported image media_type; expected one of {supported}")
    if not isinstance(data, str) or not data:
        raise _invalid("an image content block needs base64 data")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def read_turn_input_content(
    content: Any,
) -> tuple[str, list[dict[str, Any]] | None]:
    """Return ``(text, blocks)`` for one turn input.

    ``blocks`` is ``None`` for input a string already describes completely.
    ``text`` is always the input's prose, so a caller that needs a name for
    the turn never has to know whether an image came with it.
    """

    if isinstance(content, str):
        return content, None
    if not isinstance(content, list) or not content:
        raise _invalid("content must be a string or a non-empty content block list")

    blocks: list[dict[str, Any]] = []
    texts: list[str] = []
    has_non_text = False
    for block in content:
        if not isinstance(block, dict):
            raise _invalid("each content block must be an object")
        block_type = str(block.get("type") or "").strip()
        if block_type == "text":
            read = _read_text_block(block)
            texts.append(read["text"])
        elif block_type == "image":
            read = _read_image_block(block)
            has_non_text = True
        else:
            raise _invalid(
                f"unsupported content block type {block_type!r}; expected text or image"
            )
        blocks.append(read)

    text = "\n".join(part for part in texts if part.strip())
    if not has_non_text:
        # Nothing here outlives the string projection, so do not carry a
        # second representation of the same words.
        return text, None
    return text, blocks


def read_engine_content_blocks(value: Any) -> list[dict[str, Any]] | None:
    """Re-read the blocks a durable command row carries, or ``None``.

    The API already refused anything malformed, so a row that fails here is
    a corrupt journal rather than a bad request — and delivering a partly
    readable input to the engine would lose exactly the part that cannot be
    read. Raise instead; the delivery path's other malformed-row checks do
    the same.
    """

    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise RuntimeError("delivery command content_blocks is malformed")
    try:
        _text, blocks = read_turn_input_content(value)
    except APIError as exc:
        raise RuntimeError(
            f"delivery command content_blocks is malformed: {exc.message}"
        ) from exc
    return blocks


def turn_input_is_empty(text: str, blocks: list[dict[str, Any]] | None) -> bool:
    """True when an input would reach the engine carrying nothing at all."""

    if blocks:
        return False
    return not text.strip()
