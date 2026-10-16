"""A deployment that runs no extension catalog (``ASTRABOX_EXTENSION_PROVIDER=none``).

A console grant minted while LiteLLM was the selected catalog must not keep its
management pages open once it is not.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from astrabox.common.utils.errors import APIError
from astrabox.providers import litellm_extensions


async def test_litellm_management_pages_close_unless_it_is_the_selected_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_EXTENSION_PROVIDER", "none")
    monkeypatch.setattr(litellm_extensions, "_registered_on", None)
    app = FastAPI()
    litellm_extensions.register_litellm_extension_routes(app)

    async with httpx.AsyncClient(
        base_url="http://astrabox.test", transport=httpx.ASGITransport(app=app)
    ) as client:
        # A grant minted while LiteLLM was selected must not keep its pages
        # open, so the refusal comes before any cookie is read.
        with pytest.raises(APIError) as refused:
            await client.get("/extension-console/open")

    assert refused.value.code == "EXTENSION_CATALOG_DISABLED"
