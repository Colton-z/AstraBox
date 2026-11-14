# syntax=docker/dockerfile:1
#
# AstraBox — the OpenSandbox egress sidecar, decrypting only credential-bound hosts.
#
# The supplier's image unchanged, plus one mitmproxy addon loaded through its
# documented hook, OPENSANDBOX_EGRESS_MITMPROXY_SCRIPT ("User-defined addons
# can be loaded alongside this script", /var/egress/mitmscripts/system.py; the
# launcher passes each path to mitmdump as `-s` after the system addon). The
# lifecycle server never sets that variable on a sidecar, so the value below
# reaches every sidecar this image runs as. decrypt_bound_hosts.py says what
# the addon changes and why.
#
# Build (repo-root context):
#   docker build -t astrabox/egress:latest -f containers/egress/Dockerfile .
ARG UPSTREAM_EGRESS_IMAGE=opensandbox/egress:v1.1.7@sha256:db7345d567b0970f384b8e3fa7a93a71b7f43d4b16bb2009de34096e9a87b3b5
FROM ${UPSTREAM_EGRESS_IMAGE}

COPY --chmod=u=rwX,go=rX containers/egress/decrypt_bound_hosts.py /opt/astrabox/egress/decrypt_bound_hosts.py
ENV OPENSANDBOX_EGRESS_MITMPROXY_SCRIPT=/opt/astrabox/egress/decrypt_bound_hosts.py

# The hook and the hook names the addon relies on must be the ones this
# supplier image runs: a renamed variable or hook would load nothing and leave
# every connection decrypted, which looks healthy.
RUN set -eux; \
    grep -q 'OPENSANDBOX_EGRESS_MITMPROXY_SCRIPT' /var/egress/mitmscripts/system.py; \
    grep -q '^def tls_clienthello(data: ClientHelloData)' /var/egress/mitmscripts/system.py; \
    grep -q '^ACTIVE_VAULT_PATH = "/credential-vault/_active"$' /var/egress/mitmscripts/system.py; \
    mitmdump --version | grep -q '^Mitmproxy: 11\.'; \
    su -s /bin/sh mitmproxy -c 'python3 -c "import importlib.util as u; s = u.spec_from_file_location(\"addon\", \"/opt/astrabox/egress/decrypt_bound_hosts.py\"); m = u.module_from_spec(s); s.loader.exec_module(m); assert callable(m.tls_clienthello)"'
