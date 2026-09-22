"""Per-purpose keys derived from the deployment's own configured secret.

Some things need a key that is *the platform's*: a value every deployment
already has, belonging to this trust domain and no other. The bearer key the
resident assistant's in-box gateway authenticates with is one of them.

Two rules this module exists to hold:

* a purpose never uses the configured secret directly — it derives through
  :func:`derive_platform_key` with a domain string of its own, so two purposes
  can never produce the same value and learning one derived key tells you
  nothing about another;
* the derivation binds a subject too (a session, a profile), so a key is good
  for one purpose AND one subject.

**Trust-domain boundary.** The gateway key cannot derive from the sandbox
control-plane API key: that credential authenticates AstraBox to a different
system, rotates independently, and is absent from the bundled authless local
control plane. Coupling the two would invalidate live gateway tokens on a
control-plane rotation and make local deployments unable to start an assistant.

The gateway still needs *a* key: that requirement belongs to the third-party
``hermes-agent`` package, which authenticates its own API server.

**Relationship to the transcript capability.**
:mod:`astrabox.core.service.orchestrator.transcript_capability` reads the same
two environment variables and applies the same idea (never the raw vault key,
always a domain-separated derivation), but keeps its own copy of the resolution
rather than calling this one. Its vault-derived value would change under a
shared domain string, and the tokens it already minted are held by sandboxes
that are currently running, so folding the two together is a migration with
live credentials in it rather than a refactor.

**Local-store key fallback.**
``ASTRABOX_VAULT_MASTER_KEY`` is the AES-GCM master key of the local secret
vault. It is accepted as an HMAC root so a deployment configured with only that
key can still start and so sibling transcript capabilities resolve consistently.
The raw vault key is never used directly: a domain-separated subkey confines
the derived material to this purpose. An explicit signing key remains the
preferred source. A zero-config single-node install uses the same master key
persisted in ``<state_dir>/vault.key``; this is already the deployment's stable
secret and avoids inventing a second local key lifecycle. A non-local secret
store, including ``aws_kms``, exposes no raw master key and therefore requires
``ASTRABOX_TRANSCRIPT_SIGNING_KEY``.
"""

from __future__ import annotations

import hashlib
import hmac
import os

#: Resolution order for the configured secret. The explicit key wins; otherwise
#: the vault master key is used through a domain-separated subkey, so what this
#: module derives from is never the raw vault key.
_EXPLICIT_ENV = "ASTRABOX_TRANSCRIPT_SIGNING_KEY"
_VAULT_ENV = "ASTRABOX_VAULT_MASTER_KEY"
_ROOT_FROM_VAULT_DOMAIN = b"astrabox-platform-secret-root-v1"

__all__ = ["platform_secret_root", "derive_platform_key"]


def platform_secret_root() -> bytes:
    """Resolve the deployment's stable secret root.

    Explicit operator configuration wins. With no configured environment
    variable, the local secret store supplies its persisted deployment master
    key. Every source is still domain-separated before a consumer can use it.
    """
    explicit = str(os.getenv(_EXPLICIT_ENV, "") or "").strip()
    if explicit:
        return explicit.encode("utf-8")
    master = str(os.getenv(_VAULT_ENV, "") or "").strip()
    if master:
        return hmac.new(
            master.encode("utf-8"), _ROOT_FROM_VAULT_DOMAIN, hashlib.sha256
        ).digest()

    # Lazy import avoids pulling persistence registration into every module
    # that imports this small derivation helper. The secret store owns the
    # atomic create/read contract and the 0600 file permissions.
    from astrabox.providers.secret_store import load_master_key

    return hmac.new(
        load_master_key(), _ROOT_FROM_VAULT_DOMAIN, hashlib.sha256
    ).digest()


def derive_platform_key(root: bytes, *, domain: str, subject: str) -> bytes:
    """Derive one purpose's key for one subject from ``root``."""
    message = f"{domain}:{subject}".encode("utf-8")
    return hmac.new(root, message, hashlib.sha256).digest()
