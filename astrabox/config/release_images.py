"""Names of the container images an AstraBox release runs.

``.github/workflows/release.yml`` publishes every image under
``containers/<component>/Dockerfile`` as ``<prefix><component>:<version>``:
``ghcr.io/colton-z/astrabox-server:0.1.0``,
``ghcr.io/colton-z/astrabox-sandbox-hermes:0.1.0`` and so on. A deployment that
configures no image therefore runs the images of its own release, because the
tag defaults to this package's version.

Two settings move every default image at once:

* ``ASTRABOX_IMAGE_PREFIX`` replaces the repository prefix, for a registry
  mirror or a private registry that holds the same images;
* ``ASTRABOX_IMAGE_TAG`` replaces the tag. ``scripts/compose.sh`` sets the
  prefix to ``astrabox/`` and the tag to ``latest``, the names ``make build-*``
  gives images built from a checkout.

An image named explicitly still wins: ``ASTRABOX_AGENT_IMAGE``,
``ASTRABOX_WORKSPACE_MOUNTER_IMAGE`` and an Environment's image pin are read
before these defaults.
"""

from __future__ import annotations

import os

from astrabox import __version__

IMAGE_PREFIX_ENV = "ASTRABOX_IMAGE_PREFIX"
IMAGE_TAG_ENV = "ASTRABOX_IMAGE_TAG"

#: Repository prefix of the published images. The Compose file repeats it as
#: the server image's default; ``tests/release_images_test.py`` keeps the two
#: equal.
PUBLISHED_IMAGE_PREFIX = "ghcr.io/colton-z/astrabox-"


def release_image(component: str) -> str:
    """Return the image reference for ``component`` (``sandbox-codex``, ...).

    The prefix and tag are read on every call, so a changed deployment
    setting takes effect on the next resolution rather than at import.
    """
    prefix = os.environ.get(IMAGE_PREFIX_ENV) or PUBLISHED_IMAGE_PREFIX
    tag = os.environ.get(IMAGE_TAG_ENV) or __version__
    return f"{prefix}{component}:{tag}"


__all__ = [
    "IMAGE_PREFIX_ENV",
    "IMAGE_TAG_ENV",
    "PUBLISHED_IMAGE_PREFIX",
    "release_image",
]
