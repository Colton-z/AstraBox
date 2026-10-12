"""Git skill acquisition with invocation-scoped SSH credentials."""

from __future__ import annotations

import posixpath
import re
import shlex
from urllib.parse import urlsplit

from astrabox.common.utils.errors import APIError
from astrabox.seams.sandbox import SandboxHandle, sandbox_for_sandbox
from astrabox.seams.skills import (
    ResolvedSkillSource, SkillContentProvider, SkillSource,
    register_skill_content_provider,
)

def _parse_skill_descriptor(descriptor: str) -> tuple[str, str, str, str]:
    """Parse a git skill descriptor ``<repo>@<ref>#<path>`` into
    ``(repo, ref, path, name)``.

    ``<repo>`` is required; ``@<ref>`` (branch/tag/commit) and ``#<path>`` (skill
    dir inside the repo) are optional. The ref split is authority-aware so the
    user in an SSH URL (``git@host:org/repo``) is not mistaken for a ref
    delimiter. An HTTP(S) URL carrying credentials is refused: the descriptor is
    stored in the Agent and its cache manifest and echoed in clone errors, so a
    private HTTPS source authenticates through an ``http_basic`` Vault
    credential that the egress sidecar adds outside the sandbox. Fails loud on a
    malformed descriptor so a bad Agent entry never reaches a sandbox exec.
    """
    raw = descriptor.strip()
    if not raw:
        raise APIError(
            code="AGENT_RUNTIME_SKILL_CACHE_FAILED",
            message="skill descriptor must not be empty",
            status_code=500,
        )
    repo_ref, _, path = raw.partition("#")
    path = path.strip().strip("/")
    scheme_match = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", repo_ref)
    if scheme_match:
        authority_start = scheme_match.end()
        authority_end = repo_ref.find("/", authority_start)
        if authority_end == -1:
            authority_end = len(repo_ref)
        userinfo_at = repo_ref.rfind("@", authority_start, authority_end)
        scheme = scheme_match.group(0)[:-3].lower()
        if userinfo_at != -1 and scheme in {"http", "https"}:
            host = repo_ref[userinfo_at + 1:authority_end]
            raise APIError(
                code="AGENT_RUNTIME_SKILL_CACHE_FAILED",
                message=(
                    f"skill source {scheme}://{host}/… embeds credentials in its URL; "
                    "keep the URL clean and assign an HTTP Basic (Git HTTPS) Vault "
                    "credential for the repository to the Agent instead"
                ),
                status_code=500,
            )
        search_from = userinfo_at + 1 if userinfo_at != -1 else authority_start
    else:
        colon = repo_ref.find(":")
        first_at = repo_ref.find("@")
        if first_at != -1 and (colon == -1 or first_at < colon):
            search_from = first_at + 1
        else:
            search_from = 0
    ref_at = repo_ref.find("@", search_from)
    if ref_at != -1:
        repo = repo_ref[:ref_at].strip()
        ref = repo_ref[ref_at + 1:].strip()
    else:
        repo = repo_ref.strip()
        ref = ""
    if not repo:
        raise APIError(
            code="AGENT_RUNTIME_SKILL_CACHE_FAILED",
            message=f"skill descriptor is missing a repo: {descriptor!r}",
            status_code=500,
        )
    if "@" in ref or "#" in ref:
        raise APIError(
            code="AGENT_RUNTIME_SKILL_CACHE_FAILED",
            message=f"skill descriptor has a malformed ref: {descriptor!r}",
            status_code=500,
        )
    if path:
        name = posixpath.basename(path)
    else:
        repo_trimmed = repo[:-4] if repo.endswith(".git") else repo
        name = re.split(r"[/:]", repo_trimmed)[-1]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise APIError(
            code="AGENT_RUNTIME_SKILL_CACHE_FAILED",
            message=f"skill descriptor derived an invalid name {name!r} from {descriptor!r}",
            status_code=500,
        )
    return repo, ref, path, name


def _ssh_source(repo: str) -> bool:
    if "://" in repo:
        return repo.startswith("ssh://")
    return bool(re.fullmatch(r"(?:[^@\s/:]+@)?[^/:\s]+:.+", repo))


class GitSkillContentProvider(SkillContentProvider):
    """Git resolves refs before caching and fetches exactly the resolved commit."""

    name = "git"

    def validate(self, source: SkillSource) -> None:
        from astrabox.core.service.orchestrator.author_boundary import require_allowed_deploy_key

        unknown = set(source.config) - {"deploy_key_secret_name", "known_hosts"}
        if unknown:
            raise ValueError(f"unsupported Git skill configuration: {', '.join(sorted(unknown))}")
        parsed = urlsplit(source.source) if "://" in source.source else None
        if parsed is not None and parsed.scheme not in {"https", "http", "ssh", "file"}:
            raise ValueError("Git skills require an HTTPS, SSH or file source")
        if parsed is not None and (
            parsed.password is not None
            or (parsed.scheme in {"http", "https"} and parsed.username is not None)
        ):
            raise ValueError("Git skill URLs must not contain credentials; use the HTTPS Vault")
        if source.source.startswith("-") or any(c in source.source for c in "\r\n\0"):
            raise ValueError("invalid Git skill source")
        key_name = source.config.get("deploy_key_secret_name", "")
        known_hosts = source.config.get("known_hosts", "")
        if _ssh_source(source.source):
            if not key_name or not known_hosts.strip():
                raise ValueError("SSH skills require deploy_key_secret_name and verified known_hosts")
            require_allowed_deploy_key(key_name, label=f"skill {source.name!r}")
        elif key_name or known_hosts:
            raise ValueError("deploy_key_secret_name and known_hosts apply only to SSH skills")

    def egress_hosts(self, source: SkillSource) -> tuple[str, ...]:
        if "://" in source.source:
            parsed = urlsplit(source.source)
            host = "" if parsed.scheme == "file" else parsed.hostname or ""
        else:
            match = re.fullmatch(r"(?:[^@\s/:]+@)?([^/:\s]+):.+", source.source)
            host = match.group(1) if match else ""
        return (host.lower(),) if host else ()

    def _env(self, source: SkillSource, private_dir: str) -> dict[str, str]:
        env = {"GIT_TERMINAL_PROMPT": "0"}
        if _ssh_source(source.source):
            env["GIT_SSH_COMMAND"] = shlex.join([
                "ssh", "-F", "/dev/null", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
                "-o", "IdentityAgent=none", "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", f"UserKnownHostsFile={private_dir}/known_hosts",
                "-i", f"{private_dir}/key",
            ])
        return env

    async def _run(
        self, source: SkillSource, sandbox: SandboxHandle, private_dir: str, command: str,
    ) -> str:
        result = await sandbox.commands.run(command, env=self._env(source, private_dir), timeout=300)
        if result.exit_code != 0:
            # Git errors contain source URLs but never need credential values.
            reason = "Git acquisition failed"
            if "Host key verification failed" in result.stderr:
                reason = "SSH host key verification failed"
            elif "Permission denied" in result.stderr or "Authentication failed" in result.stderr:
                reason = "Git authentication or repository access was refused"
            raise APIError(
                code="AGENT_RUNTIME_SKILL_CACHE_FAILED",
                message=f"{reason} for skill {source.name!r} (exit {result.exit_code})",
                status_code=502,
            )
        return result.stdout

    async def resolve(
        self, source: SkillSource, sandbox: SandboxHandle, private_dir: str,
    ) -> ResolvedSkillSource:
        self.validate(source)
        if _ssh_source(source.source):
            from astrabox.core.service.orchestrator.runtime.storage._git_clone import (
                _resolve_author_deploy_key,
            )

            backend = sandbox_for_sandbox(sandbox)
            if backend is None:
                raise ValueError("SSH skill acquisition requires a registered sandbox provider")
            if backend.requires_https_git:
                raise ValueError("this sandbox provider does not support SSH Git acquisition")
            key = _resolve_author_deploy_key(
                source.config["deploy_key_secret_name"], label=f"skill {source.name!r}",
                error_code="AGENT_RUNTIME_SKILL_CACHE_FAILED",
            )
            await self.write_credential(sandbox, private_dir, "key", key.encode())
            await self.write_credential(
                sandbox, private_dir, "known_hosts",
                (source.config["known_hosts"].rstrip() + "\n").encode(),
            )
        output = await self._run(
            source, sandbox, private_dir,
            shlex.join(["git", "ls-remote", "--", source.source]),
        )
        refs = {}
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", parts[0]):
                refs[parts[1]] = parts[0]
        version = source.version
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", version):
            commit = version.lower()
        elif re.fullmatch(r"[0-9a-fA-F]{7,39}", version):
            # A short object name can name an unadvertised historical commit.
            # Git itself resolves it, using the same checkout fetch reuses.
            checkout = f"{private_dir}/checkout"
            await self._run(source, sandbox, private_dir, shlex.join([
                "git", "clone", "--no-checkout", "--", source.source, checkout,
            ]))
            commit = (await self._run(source, sandbox, private_dir, shlex.join([
                "git", "-C", checkout, "rev-parse", "--verify", f"{version}^{{commit}}",
            ]))).strip()
        else:
            names = [version] if version.startswith("refs/") else (
                [f"refs/heads/{version}", f"refs/tags/{version}"] if version else ["HEAD"]
            )
            candidates = {refs.get(f"{name}^{{}}", refs[name]) for name in names if name in refs}
            if len(candidates) != 1:
                raise ValueError(f"Git skill {source.name!r} needs an unambiguous existing ref")
            commit = candidates.pop()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise ValueError("Git did not resolve a full commit identity")
        return ResolvedSkillSource(version=commit, content_id=commit)

    async def fetch(
        self, source: SkillSource, resolved: ResolvedSkillSource,
        sandbox: SandboxHandle, destination: str, private_dir: str,
    ) -> None:
        checkout = f"{private_dir}/checkout"
        script = f"""set -euo pipefail
if test -d {shlex.quote(checkout)}; then
  mv -- {shlex.quote(checkout)} {shlex.quote(destination)}
else
  git clone --no-checkout -- {shlex.quote(source.source)} {shlex.quote(destination)}
fi
git -C {shlex.quote(destination)} checkout --detach {shlex.quote(resolved.version)}
test "$(git -C {shlex.quote(destination)} rev-parse HEAD)" = {shlex.quote(resolved.version)}
rm -rf -- {shlex.quote(destination + '/.git')}
"""
        await self._run(source, sandbox, private_dir, f"bash -c {shlex.quote(script)}")


register_skill_content_provider(GitSkillContentProvider())
