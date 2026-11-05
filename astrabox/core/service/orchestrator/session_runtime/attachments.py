"""Files a message carries, from the endpoint that took them to the engine's workspace.

An endpoint stages each file (``InputAttachmentRepository.stage``) before it
sends the message that names it: the Satori server when its caller uploads,
a channel when it fetches what a platform message carried. Accepting the
input binds the staged files to it in the same transaction and puts what the
engine reads into the input itself. An image also becomes an image content
block, which the engine's declaration must accept; every file's workspace path
is named in the input's words. The reader writes the files into the workspace
before it hands the input to the engine, the first moment the box is certain
to exist (:func:`deliver_attachments`).
"""

from __future__ import annotations

import base64
import posixpath
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.session_runtime.reader import InputPreparationRefused
from astrabox.persistence.repository.input_attachment_repository import InputAttachmentRepository

logger = get_logger(__name__)

#: The largest single file a message may carry. It is held in the database
#: until its Session ends, and an image travels in the input to the runtime.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
#: The most files one message may carry.
MAX_ATTACHMENTS = 10
#: How long a staged file waits for the message that sends it.
STAGED_TTL = timedelta(minutes=15)
#: Workspace directory, relative to the workspace root, that holds each input's files.
UPLOADS_DIR = "uploads"

IMAGE = "image"
FILE = "file"


@dataclass(frozen=True)
class SentAttachment:
    """A staged file a message carries, and whether it sends the file as an image."""

    attachment_id: str
    kind: str = FILE

    def __post_init__(self) -> None:
        if self.kind not in (IMAGE, FILE):
            raise ValueError(f"attachment kind must be {IMAGE!r} or {FILE!r}")


_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def image_media_type(data: bytes) -> str | None:
    """The image type the bytes are, among those an image content block accepts."""
    for signature, media_type in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return media_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def require_attachment_size(name: str, size: int) -> None:
    if size > MAX_ATTACHMENT_BYTES:
        raise APIError(
            code="INPUT_ATTACHMENT_TOO_LARGE", status_code=413,
            message=f"{name} is {size} bytes, above the {MAX_ATTACHMENT_BYTES}-byte attachment limit",
        )


def attachment_name(raw: str | None, *, fallback: str) -> str:
    """A file name safe as one workspace path segment."""
    name = posixpath.basename(str(raw or "").replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    return name if name not in {"", ".", ".."} else fallback


def workspace_names(names: Sequence[str]) -> list[str]:
    """Each file's name in its input's directory; a repeated name gets a counter."""
    taken: set[str] = set()
    unique: list[str] = []
    for name in names:
        stem, dot, extension = name.rpartition(".")
        if not dot or not stem:
            stem, extension = name, ""
        candidate, counter = name, 1
        while candidate in taken:
            counter += 1
            candidate = f"{stem} ({counter}).{extension}" if extension else f"{stem} ({counter})"
        taken.add(candidate)
        unique.append(candidate)
    return unique


def attachment_directory(input_id: str) -> str:
    return f"{UPLOADS_DIR}/{input_id}"


def turn_input(
    text: str,
    blocks: list[dict[str, Any]] | None,
    attachments: Sequence[tuple[Mapping[str, Any], str]],
    *,
    input_id: str,
) -> tuple[str, list[dict[str, Any]] | None]:
    """The input's words and content blocks once it carries ``attachments``, ``(file, kind)``.

    Every file is named by its workspace path in a closing paragraph, which
    the engine reads as part of the message. A file sent as an image is also
    an image block, and must be an image an image block accepts.
    """
    if not attachments:
        return text, blocks
    rows = [row for row, _kind in attachments]
    names = workspace_names([str(row["name"]) for row in rows])
    images: list[str | None] = []
    for row, kind in attachments:
        media_type = image_media_type(row["data"]) if kind == IMAGE else None
        if kind == IMAGE and media_type is None:
            raise APIError(
                code="INPUT_ATTACHMENT_UNSUPPORTED", status_code=400,
                message=f"{row['name']} is sent as an image but is not a PNG, JPEG, GIF or WebP image",
            )
        images.append(media_type)
    lines = ["Attached files, saved in your workspace:"]
    for name, media_type, row in zip(names, images, rows, strict=True):
        detail = f"{row['media_type']}, {row['size']} bytes"
        if media_type is not None:
            detail += ", also attached as an image"
        lines.append(f"- {attachment_directory(input_id)}/{name} ({detail})")
    note = "\n".join(lines)
    content = f"{text.rstrip()}\n\n{note}" if text.strip() else note
    image_blocks = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                     "data": base64.b64encode(row["data"]).decode("ascii")}}
        for media_type, row in zip(images, rows, strict=True) if media_type is not None
    ]
    if not image_blocks and not blocks:
        return content, None
    leading = list(blocks) if blocks else ([{"type": "text", "text": text}] if text.strip() else [])
    return content, [*leading, *image_blocks, {"type": "text", "text": note}]


WorkspaceWriter = Callable[..., Awaitable[list[str]]]


async def deliver_attachments(
    session: dict[str, Any], input_id: str, *, repository: InputAttachmentRepository, write: WorkspaceWriter,
) -> None:
    """Write an input's files where its words say they are, before the engine reads it.

    A file that cannot be written refuses the input, which then fails with
    ``INPUT_ATTACHMENT_DELIVERY_FAILED``: the engine never reads a message
    naming a file that is not there.
    """
    rows = await repository.for_input(str(session["session_id"]), input_id)
    if not rows:
        return
    names = workspace_names([str(row["name"]) for row in rows])
    try:
        await write(session, directory=attachment_directory(input_id),
                    files=[(name, row["data"]) for name, row in zip(names, rows, strict=True)])
    except Exception:
        logger.exception("attachment delivery failed session=%s input=%s", session.get("session_id"), input_id)
        raise InputPreparationRefused({
            "code": "INPUT_ATTACHMENT_DELIVERY_FAILED",
            "message": "the message's attachments could not be written into the workspace",
        }) from None
