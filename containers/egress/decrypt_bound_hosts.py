"""Decrypt only the HTTPS connections a credential binding can apply to.

A mitmproxy addon the AstraBox egress image loads beside OpenSandbox's system
addon (``/var/egress/mitmscripts/system.py``) through the vendor's
``OPENSANDBOX_EGRESS_MITMPROXY_SCRIPT`` hook. With the credential proxy on, the
sidecar redirects every outbound connection on ports 80 and 443 to mitmproxy,
which terminates TLS before the system addon knows whether a binding matches.
Every HTTPS client in the box then sees a certificate issued by the sidecar's
CA. The Agent's own processes trust that CA, because execd's bootstrap installs
it into the box's stores; a container the Agent starts inside the box does not,
so its HTTPS requests fail certificate verification.

This addon reads the same active vault the system addon injects from, over the
sidecar's private socket, and passes a TLS connection through untouched unless
its SNI matches a host of a binding that applies to HTTPS. Such a client gets
the destination's real certificate. A connection to a bound host is still
decrypted, so the system addon injects its credential as before. The egress
policy (nftables rules and the DNS proxy) applies to every connection either
way; the addon changes only which connections mitmproxy decrypts.

Every ClientHello asks the socket for the active vault the way the system
addon asks for every request: conditionally, with the snapshot tag it last
read, so an unchanged vault answers 304 without a body and a vault write is
visible to the next connection.

Matching follows the system addon's ``_host_matches``: an exact host, or
``*.suffix`` for any name below the suffix but not the suffix itself. The
binding's port, method and path are not known before decryption, so any
binding on the host keeps it decrypted.

When the vault cannot be read, the addon leaves the connection to the system
addon's default, which decrypts it: the system addon cannot read the vault
either and refuses every decrypted request with 503, so the connection fails
closed exactly as it does in the vendor's image. Passing it through instead
would let the request reach its host while the vault's state is unknown. The
failure is logged with the ``credential proxy:`` prefix, the one the sidecar's
launcher forwards from mitmproxy's output into its own log.
"""

from __future__ import annotations

import http.client
import json
import os
import socket

from mitmproxy import ctx
from mitmproxy.tls import ClientHelloData

#: The vendor's names for the socket the sidecar serves its active vault on.
SOCKET_ENV = "OPENSANDBOX_CREDENTIAL_PROXY_SOCKET"
DEFAULT_SOCKET = "/run/opensandbox/credential-proxy/active.sock"
ACTIVE_VAULT_PATH = "/credential-vault/_active"

#: The snapshot tag of the vault ``_bound_hosts`` holds, as the socket sent it.
_bound_hosts_tag: str | None = None
_bound_hosts: frozenset[str] = frozenset()


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("credential-proxy", timeout=0.25)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
        except Exception:
            sock.close()
            raise
        self.sock = sock


def _hosts_of(snapshot: dict) -> frozenset[str]:
    """Host patterns of a vault snapshot's bindings that apply to HTTPS."""
    hosts: set[str] = set()
    for binding in snapshot.get("bindings") or []:
        match = binding.get("match") or {}
        # The sidecar stores bindings normalized, with ``https`` as the
        # default scheme (credentialvault.normalizeMatch).
        if "https" not in (match.get("schemes") or ["https"]):
            continue
        hosts.update(str(host).rstrip(".").lower() for host in match.get("hosts") or [])
    return frozenset(hosts)


def _current_bound_hosts() -> frozenset[str]:
    """The bound hosts of the vault the sidecar holds now.

    A 404 means no vault has been written yet (a prepared box before its
    conversation claims it), so nothing is bound. Raises on any other answer
    that does not carry a vault.
    """
    global _bound_hosts_tag, _bound_hosts
    path = os.environ.get(SOCKET_ENV, "").strip() or DEFAULT_SOCKET
    connection = _UnixSocketHTTPConnection(path)
    headers = {"If-None-Match": _bound_hosts_tag} if _bound_hosts_tag else {}
    try:
        connection.request("GET", ACTIVE_VAULT_PATH, headers=headers)
        response = connection.getresponse()
        body = response.read()
        tag = response.getheader("ETag")
    finally:
        connection.close()
    if response.status == 304 and _bound_hosts_tag and tag == _bound_hosts_tag:
        return _bound_hosts
    if response.status == 404:
        _bound_hosts_tag, _bound_hosts = None, frozenset()
        return _bound_hosts
    if response.status != 200 or not tag:
        raise RuntimeError(f"active vault lookup answered HTTP {response.status}")
    hosts = _hosts_of(json.loads(body.decode("utf-8")))
    _bound_hosts_tag, _bound_hosts = tag, hosts
    return hosts


def _host_matches(host: str, pattern: str) -> bool:
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and host != pattern[2:]
    return host == pattern


def tls_clienthello(data: ClientHelloData) -> None:
    """Pass the connection through unless its SNI names a bound host.

    A connection without SNI is the system addon's to decide; it passes those
    through already.
    """
    global _bound_hosts_tag
    if data.ignore_connection:
        return
    sni = (data.client_hello.sni or "").rstrip(".").lower()
    if not sni:
        return
    try:
        hosts = _current_bound_hosts()
    except Exception as exc:  # noqa: BLE001 - an addon must not break traffic handling
        _bound_hosts_tag = None
        ctx.log.warn(f"credential proxy: bound-host lookup failed, decrypting: {exc}")
        return
    if not any(_host_matches(sni, pattern) for pattern in hosts):
        data.ignore_connection = True
