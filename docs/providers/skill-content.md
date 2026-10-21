# Skill content providers

Agents and Assistants use the same `skills` array. An entry is a Git string
(`repository@ref#directory`) or a source object. The console edits this array
as JSON. Catalog providers may return either shape in
`ExtensionRuntimeSelection.skill_descriptors`.

```json
{
  "provider": "git",
  "source": "git@code.example.com:team/skills.git",
  "version": "0123456789abcdef0123456789abcdef01234567",
  "path": "skills/example",
  "name": "example",
  "config": {
    "deploy_key_secret_name": "skills-read-key",
    "known_hosts": "code.example.com ssh-ed25519 <verified-public-host-key>"
  }
}
```

`provider`, `source`, `version` and `name` are required on an object. `path`
defaults to the source root. A selected directory must contain `SKILL.md`.
Installed names are unique within a declaration. Paths cannot escape the
source; symlinks must resolve inside the selected directory. Publication copies
their content, preserves executable files, and strips special permission bits.

## Git and SSH

Public Git and private HTTPS Git use the same built-in `git` provider. Private
HTTPS credentials remain in the Agent's HTTP Basic Vault; URLs cannot contain
credentials. Git resolves a ref to a full commit before computing the content
cache identity. Prefer a full commit ID for reproducibility. An ambiguous
branch/tag name is refused; use its full `refs/heads/…` or `refs/tags/…` name.
Short commit names require a checkout to resolve, even before a cache lookup.

SSH requires both `config.deploy_key_secret_name` and `config.known_hosts`.
Add the key's logical name to `ASTRABOX_DEPLOY_KEY_SECRET_NAMES`, and supply its
private-key value through the existing named environment secret mechanism.
The allowlist is checked before the secret is read. Obtain the public host key
from the repository operator through a trusted channel; `ssh-keyscan` alone
does not authenticate it. For a nonstandard SSH port, use the OpenSSH
`[hostname]:port` known-hosts form and an `ssh://` source URL.

Acquisition runs as the sandbox's platform account (root). A unique root-only
directory outside the workspace holds the key and known-hosts file, written
through the sandbox file API with mode `0600`. SSH uses that identity only,
disables the authentication agent and ambient SSH configuration, and requires
host-key verification. The core removes temporary material after success,
failure or cancellation. A sandbox that only supports HTTPS Git refuses SSH;
there is no protocol or identity fallback.

## Installing another provider

Implement `astrabox.seams.skills.SkillContentProvider` in a deployment-owned
package and advertise its class under the `astrabox.providers.skills` entry
point. Install the package in the server image before starting it. Any tools
the provider executes in a sandbox must already be in that sandbox image.
Agent configuration cannot name a Python import or an installation command.

The provider owns four operations:

- `validate(source)` checks its configuration and approved credential names,
  without reading secrets or accessing the network. Reject unknown options.
- `egress_hosts(source)` declares the sandbox hosts acquisition needs. Core
  applies the existing author/RuntimeProfile network boundary to these hosts.
- `resolve(source, sandbox, private_dir)` authorizes access and returns
  `ResolvedSkillSource(version, content_id)`. Both fields are public immutable
  identifiers. Include the tenant or namespace in source/config/content identity
  when a supplier's identifiers are only unique within that scope.
- `fetch(source, resolved, sandbox, destination, private_dir)` prepares exactly
  that resolved version in the initially absent destination. A provider can use
  its supplier's SDK or an in-image tool. It does not install into the cache or
  the conversation.

Resolution runs on every preparation, including cache hits. It must authorize
the requested source using the current credential. A cache hit skips fetching;
it does not bypass authorization. Temporary credentials belong only in the
provided private directory. Core removes that directory; providers close their
connections and other external resources. Use the base class's
`write_credential` to settle an in-flight file write before propagating
cancellation, so cleanup cannot race a late credential write. Do not place credentials in command
text, command environment, return values, logs, exceptions or source content.

Core keys content by provider, source declaration, resolved version and content
identity. Separate staging directories isolate concurrent acquisitions; misses
may fetch concurrently. A filesystem lock serializes publication. A complete
tree is atomically renamed into its immutable cache location, then its
declaration link is atomically replaced. Conversation preparation uses that
declaration's directory. Failed acquisition cannot publish a completion marker.
Cancellation before publication prevents publication; after the atomic commit
starts, a lost response cannot establish whether it committed. Visible trees
are complete in either case. Unreachable sandboxes cannot acknowledge credential
cleanup, and that failure is surfaced rather than reported as successful cleanup.

Unknown providers fail explicitly. Cache trees and temporary files live outside
the user's workspace. Replacing a sandbox fetches content again using the same
declaration and authorization path.

## Design references

The source boundary was checked against AstraBox at `3910d23b` (the existing
ExtensionProvider, engine installation scripts, Git credential resolver and
package-cache filesystem lock) and Vercel Skills at
[`13e4063a`](https://github.com/vercel-labs/skills/tree/13e4063a1cf913f5606d57d42ab83a86f5001e04/src/providers).
Vercel's MIT-licensed, actively maintained provider registry separates remote
content retrieval from installation, but its single-SKILL.md host interface
does not supply this runtime's sandbox credential lifecycle, directory tree or
immutable-version contract. Using that TypeScript CLI as a server dependency
would add another runtime and still require those adapters. No code is copied.
AstraBox retains its existing filesystem locking and named-secret mechanisms
and adds the content boundary they did not provide.

Git ref resolution follows the official
[ls-remote contract](https://git-scm.com/docs/git-ls-remote), including peeled
annotated tags. SSH identity and host verification follow
[OpenSSH configuration](https://man.openbsd.org/ssh_config).
