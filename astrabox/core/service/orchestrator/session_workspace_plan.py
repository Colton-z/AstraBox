"""Explicit workspace plans for session and agent runtimes.

This module decides cwd, opaque engine-resume identity, and default_repo
materialization. RuntimeManager executes the plan; it does not infer whether a
repo should be cloned from a default boolean.
"""

from dataclasses import dataclass
import re
from typing import Any, Literal

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    identity_path_from_source,
    identity_workspace_dir,
    plan_assistant_profile_identity,
)
from astrabox.seams.storage import WorkspaceRef


def _engine_allows_session_kind(engine_kind: str, session_kind: str) -> bool:
    """Lazy lookup because importing the engine registry here would cycle."""

    from astrabox.core.service.orchestrator.engine.capabilities import (
        engine_allowed_for_session_kind,
    )

    return engine_allowed_for_session_kind(engine_kind, session_kind)


RuntimeWorkspaceSubjectKind = Literal[
    "deployment_runtime", "assistant_runtime", "deployment_conversation"
]
RuntimeOperation = Literal["runtime_start", "runtime_attach"]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _template_has_default_repo(template: Any) -> bool:
    if isinstance(template, dict):
        return bool(template.get("default_repo"))
    return bool(getattr(template, "default_repo", None))


def _conversation_cwd(base_cwd: str, session_id: str, session_kind: str) -> str:
    """Return the workspace path shared by AstraBox's two conversation products."""

    if session_kind not in {"agent_chat", "assistant_chat"}:
        raise APIError(
            code="INVALID_SESSION_KIND",
            message=f"unsupported session_kind {session_kind!r}",
            status_code=400,
        )
    return f"{base_cwd}/conversations/{session_id}"


def _safe_path_segment(value: Any, *, label: str) -> str:
    text = _clean(value)
    if not text or not re.fullmatch(r"[A-Za-z0-9._@+:-]+", text):
        raise APIError(
            code="WORKSPACE_PLAN_INVALID",
            message=f"unsafe assistant workspace {label}: {text!r}",
            status_code=500,
        )
    return text


@dataclass(frozen=True)
class RuntimeWorkspacePlan:
    subject_kind: RuntimeWorkspaceSubjectKind
    session_kind: Literal["agent_chat", "assistant_chat"]
    operation: RuntimeOperation
    runtime_key: str
    conversation_session_id: str | None
    cwd: str
    resume_engine_session_key: str | None
    sandbox_id: str | None
    materialize_default_repo: bool
    default_repo_target_cwd: str | None
    engine_kind: str
    user_id: str | None = None
    assistant_id: str | None = None
    agent_id: str | None = None
    #: The identity this conversation was given by an earlier start, when it
    #: has had one. Its derived fields are re-planned either way; what has to
    #: survive is the allocated part — a POSIX owner is minted once and its
    #: account outlives any single placement.
    recorded_runtime_identity: dict[str, Any] | None = None

    def workspace_ref(self) -> WorkspaceRef:
        """The workspace whose files this runtime works on.

        Not every runtime has one. An Assistant's workspace is the Assistant's
        and an Agent conversation's is a subpath under its Agent; a
        `deployment_runtime` has none and is refused rather than
        given an invented root — a wrong root writes a user's files somewhere
        nobody will look for them, which reads exactly like an empty workspace.
        """
        if self.subject_kind == "assistant_runtime":
            return WorkspaceRef(
                subject_kind="assistant", subject_id=_clean(self.assistant_id)
            )
        if self.subject_kind == "deployment_conversation":
            return WorkspaceRef(
                subject_kind="agent",
                subject_id=_clean(self.agent_id),
                conversation_session_id=_clean(self.conversation_session_id),
            )
        raise APIError(
            code="WORKSPACE_PLAN_INVALID",
            message=(
                f"a {self.subject_kind!r} runtime has no workspace: its files are "
                "the box's and end with it"
            ),
            status_code=500,
        )

    def __post_init__(self) -> None:
        if self.operation == "runtime_attach" and self.materialize_default_repo:
            raise ValueError("runtime attach cannot materialize default_repo")
        if self.subject_kind == "deployment_runtime" and self.materialize_default_repo:
            raise ValueError("agent runtime cannot materialize default_repo")
        if self.subject_kind == "assistant_runtime" and self.materialize_default_repo:
            raise ValueError("assistant runtime cannot materialize default_repo")
        if self.resume_engine_session_key and self.materialize_default_repo:
            raise ValueError("resume startup cannot materialize default_repo")
        if self.materialize_default_repo:
            if not _clean(self.default_repo_target_cwd):
                raise ValueError("default_repo_target_cwd is required when materializing")
            if _clean(self.default_repo_target_cwd) != _clean(self.cwd):
                raise ValueError("default_repo_target_cwd must equal cwd")
            if not _clean(self.conversation_session_id):
                raise ValueError("conversation_session_id is required when materializing")
        elif self.default_repo_target_cwd is not None:
            raise ValueError("default_repo_target_cwd must be None when not materializing")
        if self.subject_kind == "assistant_runtime":
            if not _clean(self.user_id):
                raise ValueError("assistant runtime requires user_id")
            if not _clean(self.assistant_id):
                raise ValueError("assistant runtime requires assistant_id")
        if not _engine_allows_session_kind(self.engine_kind, self.session_kind):
            raise ValueError(
                f"engine_kind {self.engine_kind!r} does not support "
                f"session_kind {self.session_kind!r}"
            )
        if (self.subject_kind == "assistant_runtime") != (
            self.session_kind == "assistant_chat"
        ):
            raise ValueError(
                "assistant_runtime and assistant_chat must be selected together"
            )


@dataclass(frozen=True)
class ConversationWorkspacePlan:
    subject_kind: Literal["deployment_conversation"]
    session_kind: Literal["agent_chat"]
    conversation_session_id: str
    sandbox_id: str
    cwd: str
    materialize_default_repo: bool
    default_repo_target_cwd: str | None
    runtime_identity: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not _clean(self.sandbox_id):
            raise ValueError("agent conversation workspace requires sandbox_id")
        if self.runtime_identity is None:
            expected_suffix = f"/conversations/{self.conversation_session_id}"
            if not _clean(self.cwd).endswith(expected_suffix):
                raise ValueError("agent conversation workspace cwd must be conversation-scoped")
        else:
            expected_workspace = identity_workspace_dir(self.runtime_identity)
            if _clean(self.cwd) != _clean(expected_workspace):
                raise ValueError("agent conversation workspace cwd must match runtime identity")
        if self.materialize_default_repo:
            if _clean(self.default_repo_target_cwd) != _clean(self.cwd):
                raise ValueError("default_repo_target_cwd must equal conversation cwd")
        elif self.default_repo_target_cwd is not None:
            raise ValueError("default_repo_target_cwd must be None when not materializing")


class SessionWorkspacePlanner:
    def __init__(self, base_cwd: str) -> None:
        self._base_cwd = _clean(base_cwd)
        if not self._base_cwd:
            raise APIError(
                code="WORKSPACE_PLAN_INVALID",
                message="remote cwd is not configured",
                status_code=500,
            )

    @property
    def base_cwd(self) -> str:
        return self._base_cwd

    def resolve_terminal_cwd(
        self,
        *,
        session_id: str,
        session_kind: str,
        engine_session_key: str | None = None,
        existing_terminal_cwd: str | None = None,
    ) -> str:
        existing = _clean(existing_terminal_cwd)
        if existing:
            return existing
        return _conversation_cwd(self._base_cwd, session_id, session_kind)

    def plan_agent_chat_runtime_start(
        self,
        *,
        session_id: str,
        agent_id: str,
        template: Any,
        resume_engine_session_key: str | None,
        existing_terminal_cwd: str | None,
        runtime_identity: dict[str, Any] | None = None,
    ) -> RuntimeWorkspacePlan:
        """Per-session agent_chat startup.

        Uses the ``agent_chat`` runtime strategy for cwd (conversation subtree)
        and the ``deployment_conversation`` subject_kind so storage mounts the
        deployment-scoped workspace root.
        """
        resume_id = _clean(resume_engine_session_key) or None
        existing_cwd = _clean(existing_terminal_cwd) or None
        cwd = self.resolve_terminal_cwd(
            session_id=session_id,
            session_kind="agent_chat",
            engine_session_key=resume_id,
            existing_terminal_cwd=existing_cwd,
        )
        materialize = bool(_template_has_default_repo(template) and not resume_id and not existing_cwd)
        engine_kind = _clean(getattr(template, "engine_kind", None))
        if not engine_kind:
            raise APIError(
                code="WORKSPACE_PLAN_INVALID",
                message="agent runtime template has no engine_kind",
                status_code=500,
            )
        return RuntimeWorkspacePlan(
            subject_kind="deployment_conversation",
            session_kind="agent_chat",
            operation="runtime_start",
            runtime_key=session_id,
            conversation_session_id=session_id,
            cwd=cwd,
            resume_engine_session_key=resume_id,
            sandbox_id=None,
            materialize_default_repo=materialize,
            default_repo_target_cwd=cwd if materialize else None,
            engine_kind=engine_kind,
            agent_id=_clean(agent_id) or None,
            recorded_runtime_identity=(
                dict(runtime_identity) if isinstance(runtime_identity, dict) else None
            ),
        )

    def plan_runtime_attach(
        self,
        *,
        agent_id: str,
        session_id: str,
        session_kind: str,
        sandbox_id: str,
        engine_session_key: str | None,
        existing_terminal_cwd: str | None,
        engine_kind: str,
        runtime_identity: dict[str, Any] | None = None,
    ) -> RuntimeWorkspacePlan:
        if session_kind != "agent_chat":
            raise APIError(
                code="WORKSPACE_PLAN_INVALID",
                message=(
                    "generic runtime attach is only valid for agent_chat; "
                    "assistant_chat requires its Assistant workspace identity"
                ),
                status_code=500,
            )
        effective_sandbox_id = _clean(sandbox_id)
        if not effective_sandbox_id:
            raise APIError(
                code="WORKSPACE_PLAN_INVALID",
                message="runtime attach requires sandbox_id",
                status_code=400,
            )
        identity_cwd = identity_workspace_dir(runtime_identity)
        if not identity_cwd:
            raise APIError(
                code="WORKSPACE_PLAN_INVALID",
                message="agent_chat runtime attach requires runtime_identity.workspace_dir",
                status_code=500,
            )
        cwd = identity_cwd
        return RuntimeWorkspacePlan(
            subject_kind="deployment_conversation",
            session_kind="agent_chat",
            operation="runtime_attach",
            agent_id=_clean(agent_id) or None,
            runtime_key=session_id,
            conversation_session_id=session_id,
            cwd=cwd,
            resume_engine_session_key=_clean(engine_session_key) or None,
            sandbox_id=effective_sandbox_id,
            materialize_default_repo=False,
            default_repo_target_cwd=None,
            engine_kind=_clean(engine_kind),
        )

    def plan_assistant_runtime_attach(
        self,
        *,
        user_id: str,
        assistant_id: str,
        runtime_key: str,
        sandbox_id: str,
        existing_terminal_cwd: str | None,
        engine_kind: str,
    ) -> RuntimeWorkspacePlan:
        effective_sandbox_id = _clean(sandbox_id)
        if not effective_sandbox_id:
            raise APIError(
                code="WORKSPACE_PLAN_INVALID",
                message="assistant runtime attach requires sandbox_id",
                status_code=400,
            )
        safe_assistant_id = _safe_path_segment(assistant_id, label="assistant_id")
        effective_user_id = _safe_path_segment(user_id, label="user_id")
        identity = plan_assistant_profile_identity(
            engine_kind=engine_kind,
            user_id=effective_user_id,
            assistant_id=safe_assistant_id,
            sandbox_id=effective_sandbox_id,
        )
        existing_cwd = _clean(existing_terminal_cwd)
        cwd = (
            identity_path_from_source(identity, existing_cwd)
            if existing_cwd
            else identity_workspace_dir(identity)
        )
        return RuntimeWorkspacePlan(
            subject_kind="assistant_runtime",
            session_kind="assistant_chat",
            operation="runtime_attach",
            runtime_key=runtime_key,
            conversation_session_id=None,
            cwd=cwd,
            resume_engine_session_key=None,
            sandbox_id=effective_sandbox_id,
            materialize_default_repo=False,
            default_repo_target_cwd=None,
            engine_kind=engine_kind,
            user_id=effective_user_id,
            assistant_id=safe_assistant_id,
        )

    def plan_agent_runtime_start(
        self,
        *,
        agent_id: str,
        runtime_key: str,
        template: Any,
    ) -> RuntimeWorkspacePlan:
        engine_kind = _clean(getattr(template, "engine_kind", None))
        if not engine_kind:
            raise APIError(
                code="WORKSPACE_PLAN_INVALID",
                message="agent runtime template has no engine_kind",
                status_code=500,
            )
        return RuntimeWorkspacePlan(
            subject_kind="deployment_runtime",
            session_kind="agent_chat",
            operation="runtime_start",
            runtime_key=runtime_key,
            conversation_session_id=None,
            cwd=self._base_cwd,
            resume_engine_session_key=None,
            sandbox_id=None,
            materialize_default_repo=False,
            default_repo_target_cwd=None,
            engine_kind=engine_kind,
            agent_id=_clean(agent_id) or None,
        )

    def plan_assistant_runtime_start(
        self,
        *,
        user_id: str,
        assistant_id: str,
        runtime_key: str,
        template: Any,
        engine_kind: str,
    ) -> RuntimeWorkspacePlan:
        _ = template
        safe_assistant_id = _safe_path_segment(assistant_id, label="assistant_id")
        effective_user_id = _safe_path_segment(user_id, label="user_id")
        identity = plan_assistant_profile_identity(
            engine_kind=engine_kind,
            user_id=effective_user_id,
            assistant_id=safe_assistant_id,
        )
        cwd = identity_workspace_dir(identity)
        return RuntimeWorkspacePlan(
            subject_kind="assistant_runtime",
            session_kind="assistant_chat",
            operation="runtime_start",
            runtime_key=runtime_key,
            conversation_session_id=None,
            cwd=cwd,
            resume_engine_session_key=None,
            sandbox_id=None,
            materialize_default_repo=False,
            default_repo_target_cwd=None,
            engine_kind=engine_kind,
            user_id=effective_user_id,
            assistant_id=safe_assistant_id,
        )

    def plan_agent_conversation_root(
        self,
        *,
        session_id: str,
        sandbox_id: str,
        template: Any,
        materialization_pending: bool,
        runtime_identity: dict[str, Any] | None = None,
    ) -> ConversationWorkspacePlan:
        cwd = identity_workspace_dir(runtime_identity)
        if not cwd:
            cwd = self.resolve_terminal_cwd(
                session_id=session_id,
                session_kind="agent_chat",
                engine_session_key=None,
                existing_terminal_cwd=None,
            )
        materialize = bool(materialization_pending and _template_has_default_repo(template))
        return ConversationWorkspacePlan(
            subject_kind="deployment_conversation",
            session_kind="agent_chat",
            conversation_session_id=session_id,
            sandbox_id=_clean(sandbox_id),
            cwd=cwd,
            runtime_identity=runtime_identity,
            materialize_default_repo=materialize,
            default_repo_target_cwd=cwd if materialize else None,
        )
