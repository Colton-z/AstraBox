"""Shared wire schemas for request dictionaries parsed by existing services."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from astrabox.api.documentation import object_schema
from astrabox.seams.skills import SkillSource

TEXT = {"type": "string"}
JSON_VALUE: dict[str, Any] = {
    "description": "A JSON value defined by the named tool, program, or data document."
}
JSON_OBJECT = {"type": "object", "additionalProperties": True}
STRING_LIST = {"type": "array", "items": TEXT}

INPUT_CONTENT = {
    "oneOf": [
        TEXT,
        {
            "type": "array",
            "minItems": 1,
            "items": {
                "oneOf": [
                    object_schema(
                        {"type": {"const": "text"}, "text": TEXT}, required=("type", "text")
                    ),
                    object_schema(
                        {
                            "type": {"const": "image"},
                            "source": object_schema(
                                {
                                    "type": {"const": "base64"},
                                    "media_type": {
                                        "type": "string",
                                        "enum": [
                                            "image/png",
                                            "image/jpeg",
                                            "image/gif",
                                            "image/webp",
                                        ],
                                    },
                                    "data": {
                                        "type": "string",
                                        "minLength": 1,
                                        "contentEncoding": "base64",
                                    },
                                },
                                required=("type", "media_type", "data"),
                            ),
                        },
                        required=("type", "source"),
                    ),
                ]
            },
        },
    ],
    "description": "Text or a nonempty text/image block list. The selected program must support every supplied block type.",
}
SEND_INPUT = object_schema(
    {
        "content": INPUT_CONTENT,
        "client_message_id": {
            **TEXT,
            "description": "Optional caller identifier for input deduplication.",
        },
    },
    required=("content",),
)
ANSWER_INTERACTION = object_schema(
    {
        "answer": object_schema(
            {
                "decision": {
                    **TEXT,
                    "description": "Tool approval: approve or reject. A decision presentation requires an exact declared option ID.",
                },
                "comment": TEXT,
                "permission_mode": {
                    **TEXT,
                    "description": "A native mode from the selected decision option's permission_mode_choices.",
                },
                "decline": {
                    "type": "boolean",
                    "description": "Decline a form instead of submitting answers.",
                },
                "answers": {
                    "type": "array",
                    "minItems": 1,
                    "items": object_schema(
                        {
                            "question_id": TEXT,
                            "option_label": TEXT,
                            "option_labels": STRING_LIST,
                            "free_text": TEXT,
                        },
                        required=("question_id",),
                    ),
                    "description": "One answer per question, using its declared ID and selection/free-text rules.",
                },
            },
            description="Use the pending interaction's presentation and declared options. Forms accept answers or decline; decisions and tool approvals require decision.",
        ),
    },
    required=("answer",),
)
TERMINAL_COMMAND = object_schema(
    {
        "command": {"type": "string", "minLength": 1},
        "working_directory": {
            **TEXT,
            "description": "Working directory inside this conversation's workspace.",
        },
        "cwd": {
            **TEXT,
            "description": "Alternate working-directory field; working_directory takes precedence.",
        },
    },
    required=("command",),
)
JSON_RPC_REQUEST = object_schema(
    {
        "jsonrpc": {"const": "2.0"},
        "id": {"type": ["string", "integer", "null"], "description": "Absent for a notification."},
        "method": TEXT,
        "params": {
            **JSON_OBJECT,
            "description": "Parameters defined by the selected native protocol method.",
        },
    },
    required=("jsonrpc", "method"),
    description="A native JSON-RPC 2.0 request or notification.",
)
JSON_RPC_RESPONSE = object_schema(
    {
        "jsonrpc": {"const": "2.0"},
        "id": {"type": ["string", "integer", "null"]},
        "result": JSON_VALUE,
        "error": object_schema(
            {"code": {"type": "integer"}, "message": TEXT, "data": JSON_VALUE},
            required=("code", "message"),
        ),
    },
    required=("jsonrpc", "id"),
    description="The native protocol supplies result on success or error on failure, never an AstraBox success envelope.",
)
MCP_REQUEST = {"allOf": [JSON_RPC_REQUEST, {"$ref": "#/components/schemas/MCPClientCall"}]}
MCP_RESPONSE = {
    **JSON_RPC_RESPONSE,
    "properties": {
        **JSON_RPC_RESPONSE["properties"],
        "result": {"$ref": "#/components/schemas/MCPServerResult"},
    },
}
ASSISTANT_FIELDS = {
    "display_name": TEXT,
    "icon": {"type": ["string", "null"]},
    "description": {"type": ["string", "null"]},
    "permission_mode_default": {
        **TEXT,
        "description": "A permission mode declared by the Environment's program.",
    },
    "system": {"type": ["string", "null"]},
    "timezone": TEXT,
    "model_config_override": object_schema({"model_name": TEXT}, additional_properties=False),
    "mcp_servers": {**JSON_OBJECT, "description": "Name-keyed native MCP server declarations."},
    "packages": {
        "type": "array",
        "items": JSON_OBJECT,
        "description": "Package declarations accepted by the installed program.",
    },
    "skills": {
        "anyOf": [
            TEXT,
            {"type": "null"},
            {"type": "array", "items": {"anyOf": [TEXT, TypeAdapter(SkillSource).json_schema()]}},
        ],
        "description": "Skill Git URLs or provider source descriptors; a single URL is accepted as shorthand for one entry.",
    },
    "engine_options": {
        **JSON_OBJECT,
        "description": "Configuration owned and validated by the selected program.",
    },
}
ASSISTANT_CREATE = object_schema(
    {
        **ASSISTANT_FIELDS,
        "environment_name": TEXT,
        "engine_kind": {
            **TEXT,
            "description": "If supplied, must match the Environment's Assistant-capable program.",
        },
    },
    required=("display_name", "environment_name"),
)
ASSISTANT_UPDATE = object_schema(
    ASSISTANT_FIELDS,
    description="Partial update. Environment and program are fixed after workspace materialization; unsupported update keys are ignored by the existing service.",
)
TRANSCRIPT_KEY = object_schema(
    {"project_key": TEXT, "session_id": TEXT, "subpath": TEXT},
    required=("project_key", "session_id"),
)
TRANSCRIPT_LOAD = object_schema({"key": TRANSCRIPT_KEY}, required=("key",))
TRANSCRIPT_APPEND = object_schema(
    {
        "key": TRANSCRIPT_KEY,
        "entries": {
            "type": "array",
            "items": {
                **JSON_OBJECT,
                "description": "One native SessionStore entry, preserved verbatim.",
            },
        },
        "append_id": TEXT,
        "payload_sha256": {
            "type": "string",
            "pattern": "^[0-9a-fA-F]{64}$",
            "description": "Digest of the canonical key/entries batch.",
        },
    },
    required=("key", "entries", "append_id", "payload_sha256"),
)
TRANSCRIPT_LIST = object_schema({"project_key": TEXT}, required=("project_key",))
MCP_TOKEN = object_schema(
    {
        "name": TEXT,
        "scope": {**TEXT, "default": "converse", "enum": ["read", "converse"]},
        "expires_in_days": {
            "type": ["integer", "null"],
            "description": "Lifetime in days; null uses the token service's no-expiry behavior.",
        },
    }
)

DEPLOYMENT_FIELDS = {
    "scene": {
        **TEXT,
        "description": "Installed trigger scene: a webhook, schedule, or channel provider's advertised scene.",
    },
    "name": TEXT,
    "prompt_prefix": TEXT,
    "enabled": {"type": "boolean"},
    "secret": {
        "type": ["string", "null"],
        "description": "Trigger secret where the selected scene uses one.",
    },
    "attention_policy": {
        **TEXT,
        "description": "Attention policy supported by the installed channel provider.",
    },
    "response_instruction": TEXT,
    "channel_config": {
        **JSON_OBJECT,
        "description": "Fields from the selected channel provider's config_fields catalog.",
    },
    "credentials": {
        "type": "object",
        "additionalProperties": TEXT,
        "description": "Private values from that provider's credential_fields catalog; reads return masks.",
    },
    "schedule": object_schema(
        {"cron": TEXT, "timezone": TEXT},
        description="Recurring schedule: five cron fields and an IANA timezone.",
    ),
    "allowed_participants": STRING_LIST,
    "execution_mode": {
        **TEXT,
        "description": "One execution_modes value from the selected channel provider.",
    },
    "execution_user_id": {
        **TEXT,
        "description": "Account execution subject; configuring it requires platform administration.",
    },
}
DEPLOYMENT_CREATE = object_schema(DEPLOYMENT_FIELDS, required=("scene",))
DEPLOYMENT_UPDATE = object_schema(
    DEPLOYMENT_FIELDS,
    description="Partial update of a trigger binding; authorization and scene validation remain server-side.",
)
