from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_ZH_DOCS = (
    _REPO_ROOT
    / "website"
    / "i18n"
    / "zh-Hans"
    / "docusaurus-plugin-content-docs"
    / "current"
)

_EXCLUDED_PUBLIC_DOCS = {
    "RUN_E2E.md",
    "channel-spine.md",
    "comment-style.md",
    "configuration.md",
    "development.md",
    "domain-model.md",
    "frontend-design.md",
    "migrations.md",
}


def _registered_engines() -> set[str]:
    project = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(project["project"]["entry-points"]["astrabox.providers.engine"])


def _assert_contains(path: Path, values: set[str]) -> None:
    source = re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))
    missing = sorted(
        value
        for value in values
        if re.sub(r"\s+", " ", value) not in source
    )
    assert not missing, f"{path.relative_to(_REPO_ROOT)} is missing {missing}"


def _json_examples(path: Path) -> list[dict[str, object]]:
    blocks = re.findall(
        r"^```json\s*\n(.*?)^```\s*$",
        path.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    assert blocks, f"{path.relative_to(_REPO_ROOT)} has no JSON examples"
    examples = []
    for block in blocks:
        value = json.loads(block)
        assert isinstance(value, dict), str(path)
        examples.append(value)
    return examples


def _section(path: Path, heading: str) -> str:
    source = path.read_text(encoding="utf-8")
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## |\Z)",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"{path.relative_to(_REPO_ROOT)} is missing section {heading!r}"
    return match.group("body")


def _published_doc_ids() -> set[str]:
    doc_ids: set[str] = set()
    for path in (_REPO_ROOT / "docs").rglob("*.md"):
        relative = path.relative_to(_REPO_ROOT / "docs")
        if relative.parts[0] == "maintainers":
            continue
        if (
            len(relative.parts) == 1
            and path.name in _EXCLUDED_PUBLIC_DOCS
        ) or path.name.startswith(("design-", "architecture-recomb-")):
            continue
        doc_ids.add(relative.with_suffix("").as_posix())
    return doc_ids


def _published_doc_paths() -> list[Path]:
    return [
        _REPO_ROOT / "docs" / f"{doc_id}.md"
        for doc_id in sorted(_published_doc_ids())
    ]


def _api_path_pattern(path: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^}]+\})", path)
    pattern = "".join(
        r"[^/]+" if part.startswith("{") else re.escape(part)
        for part in parts
    )
    return re.compile(rf"^{pattern}$")


def test_repository_release_images_match_registered_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.engine.registry import (
        registered_engine_adapters,
    )
    from astrabox.providers import (
        load_entry_point_providers,
        register_builtin_providers,
    )
    from astrabox.config.release_images import IMAGE_PREFIX_ENV, IMAGE_TAG_ENV
    from astrabox.providers.sandbox_image import DEFAULT_AGENT_IMAGE_ENV

    # Repository documentation describes shipped defaults, not a test host's
    # deployment-specific image override.
    for override in (DEFAULT_AGENT_IMAGE_ENV, IMAGE_PREFIX_ENV, IMAGE_TAG_ENV):
        monkeypatch.delenv(override, raising=False)
    # The registry as a server composes it: built-ins, then installed plugins.
    register_builtin_providers()
    load_entry_point_providers()
    registered = _registered_engines()
    engines = {}
    for kind, adapter in registered_engine_adapters().items():
        capabilities = adapter.capabilities
        assert capabilities.engine_kind == kind, f"registry key {kind!r} names another engine"
        if kind in registered:
            engines[kind] = {
                "default_runtime_image": capabilities.default_runtime_image,
                "supported_session_kinds": sorted(capabilities.supported_session_kinds),
            }
    assert set(engines) == registered

    def image_repository(image: str) -> str:
        assert image, "a bundled engine needs a documented default image"
        return re.sub(r":[^/:]+$", "", image.split("@", 1)[0])

    image_kinds = {
        image_repository(item["default_runtime_image"]): kind
        for kind, item in engines.items()
    }
    assert len(image_kinds) == len(engines), "bundled engines share an ambiguous image"
    localized_labels: list[dict[str, str]] = []
    for introduction, heading in (
        (_REPO_ROOT / "README.md", "Included Agent programs"),
        (_REPO_ROOT / "README.zh-CN.md", "内置 Agent 程序"),
    ):
        rows = re.findall(
            r"^\|\s*([^|]+?)\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|$",
            _section(introduction, heading),
            re.MULTILINE,
        )
        assert sorted(image for _, image, _ in rows) == sorted(image_kinds), introduction
        labels = {name.casefold(): image_kinds[image] for name, image, _ in rows}
        assert len(labels) == len(engines), f"{introduction.name}: duplicate program labels"
        localized_labels.append(labels)
        for _, image, product in rows:
            kinds = engines[image_kinds[image]]["supported_session_kinds"]
            assert ("Agent" in product) == ("agent_chat" in kinds)
            assert ("Assistant" in product) == ("assistant_chat" in kinds)

    assert localized_labels[0] == localized_labels[1]


def test_repository_introductions_follow_the_public_agent_journey() -> None:
    for path, statements in (
        (
            _REPO_ROOT / "README.md",
            {
                "Turn the Agent programs you already use into cloud Agents",
                "## Core concepts",
                "One stateful Agent execution, including its messages, Events, and current state",
                "## Included Agent programs",
                "## Workflow",
                "MCP servers, Plugins, Skills, or a repository only when the Agent needs them",
                "curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh | bash",
                "make build-agent-image",
                'export ANTHROPIC_MODEL="your-model-name"',
                "scripts/compose.sh up --build -d",
                "docs/img/agent-create-console-en.png",
                "## When to use AstraBox",
                "Local Agent programs remain the best fit for interactive development",
            },
        ),
        (
            _REPO_ROOT / "README.zh-CN.md",
            {
                "把你已经在使用的 Agent 程序变成 7×24 小时在线的云端 Agent",
                "## 核心概念",
                "Agent 的一次有状态运行，包含消息、Event 和当前状态",
                "## 内置 Agent 程序",
                "## 工作流程",
                "只有 Agent 需要时，才添加",
                "curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh | bash",
                "make build-agent-image",
                'export ANTHROPIC_MODEL="your-model-name"',
                "scripts/compose.sh up --build -d",
                "img/agent-create-console-zh.png",
                "## 适用场景",
                "本地 Agent 程序仍然适合在一台电脑上交互开发",
            },
        ),
    ):
        source = re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))
        missing = sorted(statement for statement in statements if statement not in source)
        assert not missing, f"{path.relative_to(_REPO_ROOT)} is missing {missing}"
        for stale_or_wrong_frame in (
            "One continuing conversation or task",
            "A specific work session",
            "一次持续的对话或任务",
            "工作会话",
            "claude-opus-4-8",
            "docker build -t astrabox/server",
            "default Agent",
            "默认 Agent",
            "shared MCP",
            "共享 MCP",
        ):
            assert stale_or_wrong_frame not in source


def test_homepage_follows_the_product_journey() -> None:
    homepage = (_REPO_ROOT / "website/src/pages/index.tsx").read_text(
        encoding="utf-8"
    )
    diagram = (
        _REPO_ROOT / "website/src/components/ArchitectureDiagram/index.tsx"
    ).read_text(encoding="utf-8")
    translations = json.loads(
        (_REPO_ROOT / "website/i18n/zh-Hans/code.json").read_text(encoding="utf-8")
    )
    chinese = "\n".join(
        value["message"]
        for key, value in translations.items()
        if key.startswith("home.")
        and isinstance(value, dict)
        and isinstance(value.get("message"), str)
    )

    for statement in (
        "The open-source alternative to",
        "Claude Managed Agents.",
        "Conversations start and resume in seconds",
        "A cloud Agent powered by an installed Agent program.",
        "One stateful Agent execution, including its messages, Events, and current state.",
        "From deployment to a running Agent",
        "Add a system prompt, MCP servers,",
        "only when the Agent needs them.",
        "curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh | bash",
        "Let work continue after you disconnect",
        "AstraBox keeps the Agent available; OpenSandbox runs its sandbox",
        "Use the HTTP API with your applications",
    ):
        assert statement in homepage

    for statement in (
        "AstraBox sandbox service",
        "connects to the Agent program",
        "Session history",
    ):
        assert statement in diagram

    for statement in (
        "Claude Managed Agents 的",
        "开源替代。",
        "对话秒级拉起、秒级恢复",
        "Agent 的一次有状态运行，包含消息、Event 和当前状态。",
        "从部署到运行 Agent",
        "只有 Agent 需要时，才添加系统提示词、MCP 服务器、Plugin、Skill 或代码仓库。",
        "AstraBox 让 Agent 持续在线，OpenSandbox 负责沙箱",
        "沙箱内的 AstraBox 服务",
        "通过 HTTP API 接入应用",
    ):
        assert statement in chinese

    public_copy = "\n".join((homepage, diagram, chinese))
    for stale_or_wrong_frame in (
        "claude-opus-4-8",
        "docker build -t",
        "Agent adapter",
        "Agent 适配器",
        "Provider interfaces",
        "Provider 接口",
        "One continuing conversation or task",
        "A specific work session",
        "一次持续的对话或任务",
        "工作会话",
        "reusable configuration template",
        "可复用的配置模板",
        "default Agent",
        "默认 Agent",
        "shared MCP",
        "共享 MCP",
        "运行第一个 Agent",
    ):
        assert stale_or_wrong_frame not in public_copy


def test_deployment_and_credential_guides_track_runtime_enums() -> None:
    from astrabox.core.service.orchestrator.deployment_service import _VALID_SCENES
    from astrabox.core.service.orchestrator.vault_service import _ALL_AUTH_TYPES
    from astrabox.seams.channel import CHANNEL_SCENE_PREFIX

    dynamic_scene = f"{CHANNEL_SCENE_PREFIX}<provider>"
    for root in (_REPO_ROOT / "docs", _ZH_DOCS):
        deployment_path = root / "deployments.md"
        scenes = set(
            re.findall(
                r"^\|[^|\n]+\|\s*`([^`]+)`\s*\|",
                deployment_path.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        )
        assert dynamic_scene in scenes, str(deployment_path)
        assert scenes - {dynamic_scene} == set(_VALID_SCENES), str(deployment_path)

        credential_path = root / "credentials.md"
        auth_rows = re.findall(
            r"^\|\s*`auth\.type`\s*\|([^\n]+)\|\s*$",
            credential_path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert auth_rows, f"{credential_path}: missing auth.type reference"
        for row in auth_rows:
            auth_types = set(re.findall(r"`([^`]+)`", row))
            assert auth_types == set(_ALL_AUTH_TYPES), str(credential_path)


def test_api_authentication_uses_astrabox_identity_credentials_and_scopes() -> None:
    from astrabox.common.utils.user_context import (
        API_ADMIN_SCOPE,
        API_READ_SCOPE,
        API_WRITE_SCOPE,
    )

    scopes = {API_READ_SCOPE, API_WRITE_SCOPE, API_ADMIN_SCOPE}
    for path in (
        _REPO_ROOT / "docs" / "api-authentication.md",
        _ZH_DOCS / "api-authentication.md",
    ):
        _assert_contains(path, scopes)


def test_api_errors_document_the_registered_error_envelope() -> None:
    from astrabox.common.utils.errors import APIError

    payload = APIError(
        code="SESSION_BUSY",
        message="session is busy",
        status_code=409,
    ).to_response_payload()
    assert payload["error"] == {
        "code": "SESSION_BUSY",
        "status_code": 409,
        "category": "state",
        "retryable": True,
        "owner": "session",
        "user_message": "session is busy",
    }
    for path in (_REPO_ROOT / "docs" / "api-errors.md", _ZH_DOCS / "api-errors.md"):
        assert _json_examples(path)[0] == payload, str(path)


def test_generic_api_structures_match_shared_response_helpers() -> None:
    from astrabox.common.utils.api_response import success_response
    from astrabox.common.utils.errors import APIError

    assert success_response({"session_id": "session"}) == {
        "code": "OK",
        "message": "success",
        "data": {"session_id": "session"},
    }
    for path in (
        _REPO_ROOT / "docs" / "api-data-structures.md",
        _ZH_DOCS / "api-data-structures.md",
    ):
        examples = _json_examples(path)
        success_examples = [example for example in examples if "error" not in example]
        error_examples = [example for example in examples if "error" in example]
        assert success_examples, f"{path}: missing success example"
        assert error_examples, f"{path}: missing error example"
        for example in success_examples:
            assert example == success_response(example["data"]), str(path)
        for example in error_examples:
            assert example == APIError(
                code="INVALID_REQUEST",
                message="permission_mode is required",
                status_code=400,
                data=example["data"],
            ).to_response_payload(), str(path)


def test_team_login_guide_separates_login_from_api_authentication() -> None:
    for path, statements in (
        (
            _REPO_ROOT / "docs" / "team-login.md",
            {
                "The local AstraBox deployment does not require login",
                "Authentication establishes who the user is; authorization determines",
                "Trusted identity headers",
                "Verified JWT",
                "Management console → Integrated services",
                "scripts/compose.sh -f containers/compose.sso.yaml up -d",
                "/api/v1/auth/callback",
                "ASTRABOX_AUTH_SESSION_SECRET",
                "ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET",
                "ASTRABOX_JWT_ISSUER",
                "does not provide a browser redirect",
                "A successful login does not automatically make a user an administrator",
                "[Authenticate API requests](api-authentication.md)",
                "ASTRABOX_ALLOWED_HOSTS",
                "./img/team-login.svg#inline",
            },
        ),
        (
            _ZH_DOCS / "team-login.md",
            {
                "AstraBox 本地部署默认不需要登录",
                "认证确认用户是谁；鉴权决定",
                "可信身份请求头",
                "已验证 JWT",
                "管理台 → 集成服务",
                "scripts/compose.sh -f containers/compose.sso.yaml up -d",
                "/api/v1/auth/callback",
                "ASTRABOX_AUTH_SESSION_SECRET",
                "ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET",
                "ASTRABOX_JWT_ISSUER",
                "这种方式不会提供浏览器重定向",
                "成功登录不会自动让用户成为管理员",
                "[API 请求认证](api-authentication.md)",
                "ASTRABOX_ALLOWED_HOSTS",
                "./img/team-login.svg#inline",
            },
        ),
    ):
        source = re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))
        missing = sorted(statement for statement in statements if statement not in source)
        assert not missing, f"{path.relative_to(_REPO_ROOT)} is missing {missing}"
        for duplicated_api_or_wrong_term in (
            "curl ",
            "/api/v1/sessions",
            "## Option 1",
            "## Option 2",
            "## 方式一",
            "## 方式二",
            "long-lived client credentials",
            "长期客户端凭证",
            "Bearer header format",
            "Bearer 请求头格式",
            "Full request example",
            "完整请求示例",
            "browser Session",
            "浏览器 Session",
            "Session cookie",
            "Session Cookie",
            "access policy",
            "访问策略",
            "SA Key",
            "SAT（JWT）",
        ):
            assert duplicated_api_or_wrong_term not in source


def test_cli_docs_publish_the_complete_source_based_module() -> None:
    english_root = _REPO_ROOT / "docs" / "cli"
    chinese_root = _ZH_DOCS / "cli"
    page_names = ("overview.md", "commands.md", "configuration.md")

    assert not (_REPO_ROOT / "docs" / "cli.md").exists()
    for root in (english_root, chinese_root):
        assert sorted(path.name for path in root.glob("*.md")) == sorted(page_names)

    sidebar = (_REPO_ROOT / "website" / "sidebars.ts").read_text(encoding="utf-8")
    cli_category = re.search(
        r"\{\s*type: 'category',\s*label: 'CLI',\s*"
        r"items: \['cli/overview', 'cli/commands', 'cli/configuration'\],\s*\}",
        sidebar,
    )
    assert cli_category


def test_cli_command_reference_tracks_the_parser_surface() -> None:
    import argparse

    from astrabox.cli import _build_parser
    from astrabox.cli.mcp_server import tool_definitions
    from astrabox.cli.output import (
        EXIT_AUTH,
        EXIT_CONFLICT,
        EXIT_FAILED,
        EXIT_OK,
        EXIT_UNREACHABLE,
        EXIT_USAGE,
    )
    from astrabox.cli.resources import GET_ROUTES, SCHEMA_ROUTES

    parser = _build_parser()
    root_action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    documented_commands = (set(root_action.choices) - {"mcp"}) | {"mcp serve"}

    def leaf_parsers(
        command_parser: argparse.ArgumentParser,
        prefix: str = "",
    ) -> list[tuple[str, argparse.ArgumentParser]]:
        child_action = next(
            (
                action
                for action in command_parser._actions
                if isinstance(action, argparse._SubParsersAction)
            ),
            None,
        )
        if child_action is None:
            return [(prefix, command_parser)]
        leaves: list[tuple[str, argparse.ArgumentParser]] = []
        for name, child in child_action.choices.items():
            leaves.extend(leaf_parsers(child, f"{prefix} {name}".strip()))
        return leaves

    for path in (
        _REPO_ROOT / "docs" / "cli" / "commands.md",
        _ZH_DOCS / "cli" / "commands.md",
    ):
        source = path.read_text(encoding="utf-8")
        command_headings = set(
            re.findall(r"^#{2,4} `astrabox ([^`]+)`$", source, re.MULTILINE)
        )
        assert command_headings == documented_commands

        for command_name, command_parser in leaf_parsers(parser):
            for action in command_parser._actions:
                for option in action.option_strings:
                    if option not in {"-h", "--help"}:
                        assert option in source, (
                            f"{path.relative_to(_REPO_ROOT)} omits {option} from "
                            f"astrabox {command_name}"
                        )

        for resource_kind in (*SCHEMA_ROUTES, *GET_ROUTES):
            assert f"`{resource_kind}`" in source
        for tool in tool_definitions():
            assert f"`{tool['name']}`" in source
        for environment_variable in (
            "ASTRABOX_ENDPOINT",
            "ASTRABOX_SERVER_HOST_PORT",
            "ASTRABOX_TOKEN",
            "ASTRABOX_CLIENT_ID",
            "ASTRABOX_CLIENT_SECRET",
            "ASTRABOX_TOKEN_URL",
            "ASTRABOX_SCOPE",
            "ASTRABOX_HOST",
            "ASTRABOX_PORT",
            "ASTRABOX_LOG_LEVEL",
            "ASTRABOX_WEB_IDENTITY",
            "ASTRABOX_ALLOW_UNAUTHENTICATED_BIND",
            "ASTRABOX_AGENT_IMAGE",
        ):
            assert environment_variable in source
        for exit_code in (
            EXIT_OK,
            EXIT_FAILED,
            EXIT_USAGE,
            EXIT_AUTH,
            EXIT_UNREACHABLE,
            EXIT_CONFLICT,
        ):
            assert re.search(
                rf"^\|\s*`{exit_code}`\s*\|", source, re.MULTILINE
            )


def test_cli_configuration_reference_tracks_resource_schemas() -> None:
    import yaml

    from astrabox.cli.document import parse_document
    from astrabox.core.service.orchestrator.agent_schema import AGENT_FIELD_SCHEMA
    from astrabox.core.service.orchestrator.environment_schema import (
        ENV_FIELD_SCHEMA,
    )

    def table_fields(source: str, heading: str) -> dict[str, bool]:
        match = re.search(
            rf"^### {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^### |\Z)",
            source,
            re.MULTILINE | re.DOTALL,
        )
        assert match, f"missing quick-reference table {heading!r}"
        return {
            key: required == "✅"
            for key, required in re.findall(
                r"^\| `([^`]+)` \| ([✅❌]) \|",
                match.group("body"),
                re.MULTILINE,
            )
        }

    expected_environments = {
        str(field["key"]): bool(field.get("required"))
        for field in ENV_FIELD_SCHEMA
    }
    expected_agents = {
        str(field["key"]): bool(field.get("required"))
        for field in AGENT_FIELD_SCHEMA
    }
    pages = (
        (
            _REPO_ROOT / "docs" / "cli" / "configuration.md",
            "Environment fields",
            "Agent fields",
        ),
        (
            _ZH_DOCS / "cli" / "configuration.md",
            "Environment 字段",
            "Agent 字段",
        ),
    )
    for path, environment_heading, agent_heading in pages:
        source = path.read_text(encoding="utf-8")
        assert table_fields(source, environment_heading) == expected_environments
        assert table_fields(source, agent_heading) == expected_agents

        complete_documents = 0
        for block in re.findall(r"```yaml\s*\n(.*?)```", source, re.DOTALL):
            raw = yaml.safe_load(block)
            if (
                isinstance(raw, dict)
                and raw.get("version") == 1
                and set(raw) <= {"version", "environments", "agents"}
            ):
                parse_document(raw)
                complete_documents += 1
        assert complete_documents >= 9


def test_every_public_doc_appears_once_in_the_sidebar() -> None:
    sidebar = (_REPO_ROOT / "website" / "sidebars.ts").read_text(encoding="utf-8")
    for doc_id in sorted(_published_doc_ids()):
        count = len(re.findall(rf"['\"]{re.escape(doc_id)}['\"]", sidebar))
        assert count == 1, f"public doc {doc_id!r} appears {count} times in sidebars.ts"


def test_documented_http_operations_exist_in_the_live_openapi_schema() -> None:
    from astrabox.api.app import create_app

    schema = create_app().openapi()
    operations = [
        (method.upper(), _api_path_pattern(path))
        for path, declaration in schema["paths"].items()
        for method in declaration
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    ]
    reference = re.compile(
        r"\b(GET|POST|PUT|PATCH|DELETE)\s+"
        r"(/api/v1/[A-Za-z0-9_./{}-]+)"
    )
    missing: list[str] = []
    for english_path in _published_doc_paths():
        relative = english_path.relative_to(_REPO_ROOT / "docs")
        for path in (english_path, _ZH_DOCS / relative):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                for method, raw_path in reference.findall(line):
                    documented_path = raw_path.split("?", 1)[0].rstrip(".,;:)")
                    if not any(
                        candidate_method == method
                        and pattern.fullmatch(documented_path)
                        for candidate_method, pattern in operations
                    ):
                        missing.append(
                            f"{path.relative_to(_REPO_ROOT)}:{line_number}: "
                            f"{method} {raw_path}"
                        )
    assert not missing, "\n".join(missing)


def test_public_astrabox_environment_variables_have_a_live_source() -> None:
    from astrabox.config.env_registry import ENV_REGISTRY

    token = re.compile(r"\bASTRABOX_[A-Z0-9_]+\b")
    documented = {
        name
        for english_path in _published_doc_paths()
        for path in (
            english_path,
            _ZH_DOCS / english_path.relative_to(_REPO_ROOT / "docs"),
        )
        for name in token.findall(path.read_text(encoding="utf-8"))
    }
    live = {row.name for row in ENV_REGISTRY}
    source_roots = (
        _REPO_ROOT / "astrabox",
        _REPO_ROOT / "containers",
        _REPO_ROOT / "e2e",
        _REPO_ROOT / "scripts",
        _REPO_ROOT / "tests",
    )
    for source_root in source_roots:
        for path in source_root.rglob("*"):
            if (
                not path.is_file()
                or "node_modules" in path.parts
                or "_frontend_dist" in path.parts
                or path.suffix
                not in {
                    ".json",
                    ".mjs",
                    ".py",
                    ".sh",
                    ".toml",
                    ".ts",
                    ".tsx",
                    ".yaml",
                    ".yml",
                }
            ):
                continue
            try:
                live.update(token.findall(path.read_text(encoding="utf-8")))
            except UnicodeDecodeError:
                continue
    live.update(
        token.findall((_REPO_ROOT / "Makefile").read_text(encoding="utf-8"))
    )
    assert not documented - live, sorted(documented - live)


@pytest.mark.filterwarnings(
    "ignore::pydantic.warnings.PydanticDeprecatedSince20"
)
def test_pinned_opensandbox_server_keeps_the_documented_runtime_boundary() -> None:
    from fastapi import HTTPException

    server_config = pytest.importorskip(
        "opensandbox_server.config",
        reason="requires the optional sandbox-server contract extra",
    )
    server_validators = pytest.importorskip(
        "opensandbox_server.services.validators",
        reason="requires the optional sandbox-server contract extra",
    )
    secure_runtime_config = server_config.SecureRuntimeConfig

    runtime_description = str(
        secure_runtime_config.model_fields["type"].description or ""
    )
    assert "Kubernetes only" in runtime_description

    with pytest.raises(HTTPException) as rejected:
        server_validators.ensure_egress_runtime_compatible(
            SimpleNamespace(default_action="deny"),
            secure_runtime_config(type="gvisor", docker_runtime="runsc"),
        )
    assert rejected.value.status_code == 400
