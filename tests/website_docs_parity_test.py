from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS_ROOT = _REPO_ROOT / "docs"
_ZH_ROOT = (
    _REPO_ROOT / "website" / "i18n" / "zh-Hans" / "docusaurus-plugin-content-docs" / "current"
)

_EXCLUDED_FILENAMES = {
    "RUN_E2E.md",
    "channel-spine.md",
    "comment-style.md",
    "development.md",
    "domain-model.md",
    "frontend-design.md",
    "migrations.md",
}
_CONFIG_TOKEN = re.compile(
    r"\b(?:ASTRABOX|ANTHROPIC|DEEPSEEK|LITELLM|LANGFUSE|OPENAI|AWS)_[A-Z0-9_]+\b"
)
_HEADING = re.compile(r"^(#{1,6})\s+")
_FENCE = re.compile(r"^(`{3,}|~{3,})(.*)$")
_EXPLICIT_ANCHOR = re.compile(r"\{#([a-z0-9-]+)\}")
_MARKDOWN_LINK = re.compile(r"!?\[[^]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")

_EXPECTED_SIDEBAR = (
    ("Quick start", ("overview", "capabilities", "quickstart")),
    (
        "Build Agent",
        (
            "authoring-agents",
            "adding-tools",
            "agent-skills",
            "permission-modes",
            "models",
        ),
    ),
    (
        "Configure Agent environment",
        ("environments", "container-reference", "networking"),
    ),
    (
        "Delegate tasks",
        (
            "sessions",
            "events-stream",
            "working-with-repos",
            "credentials",
            "multi-agents",
        ),
    ),
    (
        "Integrate Agent",
        ("schedules", "channels", "webhooks", "deployments", "agent-mcp"),
    ),
    ("Manage Agent context", ("files", "assistants")),
    (
        "Best practices",
        ("example-investment-research", "compare-claude-code-self-hosted"),
    ),
    ("CLI", ("cli/overview", "cli/commands", "cli/configuration")),
    (
        "API conventions",
        (
            "api",
            "api-authentication",
            "api-pagination",
            "api-errors",
            "api-data-structures",
        ),
    ),
    (
        "Self-host AstraBox",
        (
            "deploy",
            "deploy-distributed",
            "team-login",
            "egress-credential-injection",
            "providers/opensandbox",
            "providers/aws-efs",
            "architecture",
        ),
    ),
    (
        "Extend AstraBox",
        ("embedding", "writing-an-engine-adapter", "writing-a-channel-provider"),
    ),
)


def _published_english_paths() -> set[Path]:
    paths: set[Path] = set()
    for path in _DOCS_ROOT.rglob("*.md"):
        relative = path.relative_to(_DOCS_ROOT)
        if relative.parts[0] == "maintainers":
            continue
        if relative == Path("configuration.md") or path.name in _EXCLUDED_FILENAMES or path.name.startswith(
            ("design-", "architecture-recomb-")
        ):
            continue
        paths.add(relative)
    return paths


def _translated_paths() -> set[Path]:
    return {path.relative_to(_ZH_ROOT) for path in _ZH_ROOT.rglob("*.md")}


def _markdown_structure(source: str) -> tuple[list[int], list[str], bool]:
    headings: list[int] = []
    fences: list[str] = []
    closing_marker: str | None = None
    for line in source.splitlines():
        fence = _FENCE.match(line)
        if fence:
            marker, info = fence.groups()
            if closing_marker is None:
                closing_marker = marker[0]
                fences.append(info.strip())
            elif marker[0] == closing_marker:
                closing_marker = None
            continue
        if closing_marker is None and (heading := _HEADING.match(line)):
            headings.append(len(heading.group(1)))
    return headings, fences, closing_marker is None


def test_every_published_english_page_has_one_chinese_source() -> None:
    assert _translated_paths() == _published_english_paths()


def test_bilingual_pages_are_well_formed_and_preserve_shared_contract_tokens() -> None:
    for relative in sorted(_translated_paths()):
        english = (_DOCS_ROOT / relative).read_text(encoding="utf-8")
        chinese = (_ZH_ROOT / relative).read_text(encoding="utf-8")

        for locale, source in (("English", english), ("Chinese", chinese)):
            headings, _, fences_are_balanced = _markdown_structure(source)
            assert fences_are_balanced, f"{relative}: {locale} has an open code fence"
            assert headings and headings[0] == 1 and headings.count(1) == 1, (
                f"{relative}: {locale} must contain one leading page title"
            )
        assert set(_CONFIG_TOKEN.findall(chinese)) == set(_CONFIG_TOKEN.findall(english)), relative
        assert set(_EXPLICIT_ANCHOR.findall(english)) <= set(_EXPLICIT_ANCHOR.findall(chinese)), (
            relative
        )


def test_sidebar_keeps_the_reviewed_order_and_astrabox_additions() -> None:
    source = (_REPO_ROOT / "website" / "sidebars.ts").read_text(encoding="utf-8")
    categories = []
    for match in re.finditer(
        r"\{\s*type: 'category',\s*label: '([^']+)'.*?items: \[(.*?)\],\s*\}",
        source,
        re.DOTALL,
    ):
        label, items_source = match.groups()
        categories.append((label, tuple(re.findall(r"'([^']+)'", items_source))))
    assert tuple(categories) == _EXPECTED_SIDEBAR


def test_public_markdown_links_and_explicit_fragments_resolve() -> None:
    paths = [
        _REPO_ROOT / "README.md",
        _REPO_ROOT / "README.zh-CN.md",
        *(_DOCS_ROOT / relative for relative in sorted(_published_english_paths())),
        *(_ZH_ROOT / relative for relative in sorted(_translated_paths())),
    ]
    failures: list[str] = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(source.splitlines(), start=1):
            for raw_target in _MARKDOWN_LINK.findall(line):
                target = raw_target.strip("<>")
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                raw_path, _, fragment = target.partition("#")
                resolved = (path.parent / raw_path).resolve()
                if not resolved.exists():
                    failures.append(
                        f"{path.relative_to(_REPO_ROOT)}:{line_number}: {target}"
                    )
                    continue
                if fragment and resolved.suffix in {".md", ".mdx"}:
                    anchors = set(
                        _EXPLICIT_ANCHOR.findall(resolved.read_text(encoding="utf-8"))
                    )
                    if fragment not in anchors:
                        failures.append(
                            f"{path.relative_to(_REPO_ROOT)}:{line_number}: "
                            f"missing explicit anchor {fragment!r} in "
                            f"{resolved.relative_to(_REPO_ROOT)}"
                        )
    assert not failures, "\n".join(failures)


def test_navigation_routes_resolve_to_published_documents() -> None:
    source = (_REPO_ROOT / "website" / "docusaurus.config.ts").read_text(
        encoding="utf-8"
    )
    published = {path.with_suffix("").as_posix() for path in _published_english_paths()}
    routes = set(re.findall(r"to: '/docs/([^']+)'", source))
    assert routes <= published, sorted(routes - published)
