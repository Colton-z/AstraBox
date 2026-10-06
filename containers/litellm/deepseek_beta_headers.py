"""Preserve DeepSeek's native Messages beta headers in the pinned LiteLLM SDK.

LiteLLM exposes a provider hook for native Messages endpoints to retain their
opt-ins. Its Copilot and openai_like adapters use it, but the pinned DeepSeek
adapter does not. Apply that same override at image build time, or materialize
it for a read-only testbed mount. Other providers and their remote beta policy
remain untouched. Reject an unreviewed supplier source instead of patching it.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path


NATIVE_CONFIG_PATH = "llms/deepseek/messages/transformation.py"
# Identical source in LiteLLM 1.99.0 (bundled) and 1.100.0 (AWS gateway).
NATIVE_CONFIG_SHA256 = "b39a83832eaaea8469e997b2abe104cdf6dce49f6d2c40729d902988e1022d99"
NATIVE_HEADER_OVERRIDE = '''    def should_filter_anthropic_beta_headers(self) -> bool:
        """Forward opt-ins to the native DeepSeek Messages endpoint."""
        return False
'''


def preserve_native_headers(source: str) -> str:
    """Add the supported provider override to exactly the reviewed source."""
    if hashlib.sha256(source.encode()).hexdigest() == NATIVE_CONFIG_SHA256:
        return source + NATIVE_HEADER_OVERRIDE
    if source.endswith(NATIVE_HEADER_OVERRIDE):
        original = source[:-len(NATIVE_HEADER_OVERRIDE)]
        if hashlib.sha256(original.encode()).hexdigest() == NATIVE_CONFIG_SHA256:
            return source
    raise ValueError("the installed LiteLLM DeepSeek provider source is not the reviewed version")


def installed_config() -> Path:
    """Locate the pinned provider without importing the proxy process."""
    specification = importlib.util.find_spec("litellm")
    if specification is None or specification.origin is None:
        raise RuntimeError("LiteLLM is not installed in this interpreter")
    return Path(specification.origin).parent / NATIVE_CONFIG_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--install", action="store_true", help="apply the provider override while building its image")
    mode.add_argument("--describe", action="store_true", help="emit the destination and provider source for a read-only mount")
    mode.add_argument("--check", action="store_true", help="check the deployed source without modifying it")
    args = parser.parse_args()
    path = installed_config()
    source = path.read_text()
    desired = preserve_native_headers(source)
    if args.install:
        path.write_text(desired)
    elif args.describe:
        print(json.dumps({"destination": str(path), "source": desired}))
    elif source != desired:
        raise RuntimeError("the deployed LiteLLM DeepSeek provider lacks its native header override")


if __name__ == "__main__":
    main()
