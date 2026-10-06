from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def load_helper():
    path = Path(__file__).resolve().parents[1] / "containers/litellm/deepseek_beta_headers.py"
    specification = importlib.util.spec_from_file_location("deepseek_beta_headers", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.mark.parametrize("source", ["unreviewed supplier source\n", ""])
def test_unreviewed_supplier_source_is_refused_even_with_our_override(source: str) -> None:
    helper = load_helper()
    with pytest.raises(ValueError, match="not the reviewed version"):
        helper.preserve_native_headers(source + helper.NATIVE_HEADER_OVERRIDE)


def test_an_unreviewed_unpatched_provider_is_refused() -> None:
    helper = load_helper()
    with pytest.raises(ValueError, match="not the reviewed version"):
        helper.preserve_native_headers("unreviewed supplier source\n")
