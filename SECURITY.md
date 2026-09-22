# Security Policy

Colton Qi takes the security of AstraBox seriously. Thank you for helping
keep AstraBox and its users safe.

## Supported versions

Security fixes are provided for the latest `0.1.x` release line.

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅        |
| < 0.1   | ❌        |

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues, pull
requests, or discussions.** Public disclosure before a fix is available puts users at
risk.

Instead, report it privately through GitHub: open the repository's
[**Security ▸ Report a vulnerability**](https://github.com/Colton-z/AstraBox/security/advisories/new)
page. This keeps the report visible only to you and the maintainers and lets us
work on a fix together in a private advisory.

Please include, where possible:

- A description of the vulnerability and its impact.
- Steps to reproduce, or a proof-of-concept.
- Affected version(s) and configuration (for example, the sandbox backend and
  whether AstraBox runs on one server or Kubernetes).
- Any suggested remediation.

## Deployment security model

The default local Compose deployment is **single-user with no API auth** and
binds to loopback. Its Docker runtime mounts the host Docker socket, which is
**root-equivalent on the host**:

- **Never expose the backend port to an untrusted network.** Bind it to
  loopback (`-p 127.0.0.1:8088:8000`) or configure OIDC, verified JWT, or
  trusted-header authentication before opening it up.
- Treat unauthenticated access to that deployment as privileged access to the
  Docker host.

Kubernetes deployments use the configured cluster identity instead of a Docker
socket. Restrict that identity and the OpenSandbox lifecycle endpoint to their
documented permissions and trusted callers. Persistent workspace routing uses
privileged host-side mount helpers; those privileges are not granted to user
sandboxes. Shared-sandbox conversations use isolated Linux accounts and working
directories but share a container and network namespace. Use separate sandboxes
when that shared-container boundary does not meet the workload's requirements.

Native SessionStore data and encrypted credentials are kept by the platform,
independently of optional persistent workspace files. Protect database access,
workspace storage, backups, and encryption/signing keys. Multiple API replicas
must share the intended keys and services; adding replicas does not make an
individual dependency highly available. See [deployment](docs/deploy.md) and
[OpenSandbox isolation](docs/providers/opensandbox.md).

## What to expect

- We aim to acknowledge a report within **3 business days**.
- We will share an initial assessment, material status changes, and a coordinated
  disclosure timeline as they become available.
- Credit for the discovery once a fix is released, if you would like it.

Please give us a reasonable window to address the issue before any public disclosure.
