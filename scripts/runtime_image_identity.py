#!/usr/bin/env python3
"""Prove that a running container's image is an exact registry image's content.

A Pod or container reports its image as a content digest (Kubernetes
``imageID``, Docker ``.Image``), and depending on the runtime's image store
that digest names the pushed index, the platform manifest, or the image
config. This resolves the expected image's registry digest to the manifest and
config for one platform through the OCI distribution API, resolves the observed
digest the same way, and passes only when both name the same content. Every
manifest body is hashed and must match the digest it was requested by.

``scripts/e2e_smoke.sh`` runs it when a smoke targets an exact Agent, so the
turn it proves is known to have run on the image under test. It needs ``curl``
and a registry reachable at ``--registry-api``.

Usage::

    runtime_image_identity.py --registry-api http://registry:5000 \
        --expected-image registry:5000/astrabox/sandbox-codex:<tag> \
        --expected-digest sha256:<pushed digest> \
        --observed-image-id <Pod imageID> \
        --operating-system linux --architecture amd64 [--json-out FILE]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any


class RuntimeImageError(RuntimeError):
    """The observed image is not proven to be the expected image's content."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeImageError(message)


OCI_INDEX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
    }
)
OCI_MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    }
)
OCI_ACCEPT = ", ".join(sorted(OCI_INDEX_MEDIA_TYPES | OCI_MANIFEST_MEDIA_TYPES))


def sha256_digest(value: Any, label: str) -> str:
    require(
        isinstance(value, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None,
        f"{label} is not a sha256 digest",
    )
    return value


def registry_manifest(
    *, registry_api: str, repository: str, digest: str
) -> dict[str, Any]:
    digest = sha256_digest(digest, "registry manifest digest")
    encoded_repository = "/".join(
        urllib.parse.quote(part, safe="") for part in repository.split("/")
    )
    url = f"{registry_api.rstrip('/')}/v2/{encoded_repository}/manifests/{digest}"
    completed = subprocess.run(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "10",
            "-H",
            f"Accept: {OCI_ACCEPT}",
            url,
        ],
        capture_output=True,
        check=False,
    )
    require(
        completed.returncode == 0,
        f"registry did not return manifest {repository}@{digest}",
    )
    actual_digest = "sha256:" + hashlib.sha256(completed.stdout).hexdigest()
    require(
        actual_digest == digest,
        f"registry manifest content does not match {repository}@{digest}",
    )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeImageError(
            f"registry manifest {repository}@{digest} is not JSON"
        ) from exc
    require(
        isinstance(value, dict),
        f"registry manifest {repository}@{digest} is not an object",
    )
    return value


def runtime_manifest_identity(
    *,
    registry_api: str,
    repository: str,
    root_digest: str,
    operating_system: str,
    architecture: str,
) -> tuple[str, str]:
    value = registry_manifest(
        registry_api=registry_api,
        repository=repository,
        digest=root_digest,
    )
    media_type = value.get("mediaType")
    if media_type in OCI_INDEX_MEDIA_TYPES:
        descriptors = value.get("manifests")
        require(
            isinstance(descriptors, list),
            f"registry index {repository}@{root_digest} has no manifests",
        )
        matches = [
            descriptor
            for descriptor in descriptors
            if isinstance(descriptor, dict)
            and isinstance(descriptor.get("platform"), dict)
            and descriptor["platform"].get("os") == operating_system
            and descriptor["platform"].get("architecture") == architecture
        ]
        require(
            len(matches) == 1,
            f"registry index {repository}@{root_digest} has {len(matches)} "
            f"manifests for {operating_system}/{architecture}",
        )
        manifest_digest = sha256_digest(
            matches[0].get("digest"), "runtime manifest digest"
        )
        value = registry_manifest(
            registry_api=registry_api,
            repository=repository,
            digest=manifest_digest,
        )
        media_type = value.get("mediaType")
    else:
        manifest_digest = root_digest
    require(
        media_type in OCI_MANIFEST_MEDIA_TYPES,
        f"runtime descriptor {repository}@{manifest_digest} is not an image manifest",
    )
    config = value.get("config")
    require(
        isinstance(config, dict),
        f"runtime manifest {repository}@{manifest_digest} has no config descriptor",
    )
    config_digest = sha256_digest(
        config.get("digest"), "runtime config digest"
    )
    return manifest_digest, config_digest


def expected_image_repository(reference: str) -> str:
    authority, separator, repository_and_tag = reference.partition("/")
    require(
        bool(authority and separator and repository_and_tag),
        "expected runtime image has no registry authority",
    )
    final_component = repository_and_tag.rsplit("/", 1)[-1]
    require(
        ":" in final_component and "@" not in repository_and_tag,
        "expected runtime image must use an exact tag",
    )
    repository = repository_and_tag.rsplit(":", 1)[0]
    require(bool(repository), "expected runtime image has no repository")
    return repository


def observed_image_identity(value: str, expected_repository: str) -> str:
    observed = value.strip()
    for prefix in ("docker-pullable://", "containerd://"):
        if observed.startswith(prefix):
            observed = observed[len(prefix) :]
            break
    if re.fullmatch(r"sha256:[0-9a-f]{64}", observed):
        return observed
    repository_reference, separator, digest = observed.rpartition("@")
    require(bool(separator), "Pod imageID has no digest")
    digest = sha256_digest(digest, "Pod imageID digest")
    _, slash, repository = repository_reference.partition("/")
    require(
        bool(slash) and repository == expected_repository,
        "Pod imageID names a different repository",
    )
    return digest


def runtime_image_evidence(
    *,
    registry_api: str,
    expected_image: str,
    expected_digest: str,
    observed_image_id: str,
    operating_system: str,
    architecture: str,
) -> dict[str, Any]:
    parsed_registry = urllib.parse.urlsplit(registry_api)
    require(
        parsed_registry.scheme in {"http", "https"}
        and bool(parsed_registry.netloc)
        and parsed_registry.path in {"", "/"}
        and not parsed_registry.query
        and not parsed_registry.fragment,
        "registry API must be an HTTP(S) origin",
    )
    require(
        re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", operating_system)
        is not None,
        "runtime operating system is invalid",
    )
    require(
        re.fullmatch(r"[A-Za-z0-9_+-]+", architecture) is not None,
        "runtime architecture is invalid",
    )
    repository = expected_image_repository(expected_image)
    expected_digest = sha256_digest(expected_digest, "expected registry digest")
    observed_digest = observed_image_identity(observed_image_id, repository)
    expected_manifest, expected_config = runtime_manifest_identity(
        registry_api=registry_api,
        repository=repository,
        root_digest=expected_digest,
        operating_system=operating_system,
        architecture=architecture,
    )
    if observed_digest in {expected_digest, expected_manifest, expected_config}:
        observed_manifest = expected_manifest
        observed_config = expected_config
    else:
        observed_manifest, observed_config = runtime_manifest_identity(
            registry_api=registry_api,
            repository=repository,
            root_digest=observed_digest,
            operating_system=operating_system,
            architecture=architecture,
        )
    require(
        (observed_manifest, observed_config)
        == (expected_manifest, expected_config),
        "runtime image id resolves to different content than the release image",
    )
    return {
        "architecture": architecture,
        "image": expected_image,
        "observed_image_id_digest": observed_digest,
        "operating_system": operating_system,
        "registry_digest": expected_digest,
        "runtime_config_digest": expected_config,
        "runtime_manifest_digest": expected_manifest,
        "state": "PASS",
    }


def write_evidence(path: Path | None, evidence: dict[str, Any]) -> None:
    rendered = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(rendered)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(path)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    value.add_argument("--registry-api", required=True)
    value.add_argument("--expected-image", required=True)
    value.add_argument("--expected-digest", required=True)
    value.add_argument("--observed-image-id", required=True)
    value.add_argument("--operating-system", required=True)
    value.add_argument("--architecture", required=True)
    value.add_argument("--json-out", type=Path, help="write evidence atomically instead of stdout")
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        evidence = runtime_image_evidence(
            registry_api=args.registry_api,
            expected_image=args.expected_image,
            expected_digest=args.expected_digest,
            observed_image_id=args.observed_image_id,
            operating_system=args.operating_system,
            architecture=args.architecture,
        )
        write_evidence(args.json_out, evidence)
    except (RuntimeImageError, subprocess.SubprocessError, ValueError) as exc:
        print(f"RUNTIME IMAGE IDENTITY FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
