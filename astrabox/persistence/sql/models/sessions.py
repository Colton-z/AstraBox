"""Sessions, their runtime binding, input custody, runtime facts, output log and transcripts.

The runtime owns the conversation; these tables hold what the platform needs
around it:

* :class:`Session`: identity, ownership, lifecycle and the runtime binding.
* :class:`SessionInput`: custody of an input until the runtime durably admits it.
* :class:`SessionResponse`, :class:`SessionResponseInput`,
  :class:`SessionInteraction`: what the runtime reported about its responses
  and the interactions they waited on, recorded by the session's reader.
* :class:`SessionOutput`: the browser parts the reader relayed, in order; the
  page's history and every reconnect read it.
* :class:`TerminalCommand`, :class:`TerminalOutput`: the session web
  terminal's command and its output, which a reconnecting page reads back.
* :class:`TranscriptScope`, :class:`TranscriptEntry`: the runtime's own
  transcript (SessionStore) bytes, kept so a replacement runtime can resume.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    UniqueConstraint,
    column,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base
from astrabox.persistence.sql.schema import (
    MYSQL_TABLE_OPTIONS,
    EmptyListDefault,
    GeneratedOrderIndex,
    NullableUnique,
    NullCount,
    TextDefault,
    boolean_checks,
    string_list_check,
)
from astrabox.persistence.sql.types import KeyText, StringList, binary_payload, payload_json, payload_text

_SESSION_FK = "sessions.session_id"
_RUNNING_TERMINAL = column("status") == "running"


class Session(Base):
    """A conversation with one Agent or Assistant, and the box it runs in.

    ``state`` is the platform's lifecycle (CREATING, READY, TERMINATED,
    DELETED). ``runtime_status`` is a copy of what the runtime last reported
    (``idle``, ``working``, ``waiting``), kept for lists and sweeps that cannot
    ask a runtime; the runtime itself stays the authority.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_list", "user_id", "hidden", "deleted", text("updated_at DESC"), text("session_id DESC")),
        Index("ix_sessions_agent_list", "agent_id", "session_kind", "deleted", text("updated_at DESC")),
        Index(None, "assistant_id", "user_id"),
        Index(None, "owner_type", "owner_id"),
        Index(None, "sandbox_id"),
        Index(None, "state", "updated_at"),
        Index(None, "environment_name", "session_id"),
        string_list_check("undestroyed_sandbox_ids"),
        *boolean_checks("hidden", "deleted", "runtime_unavailable"),
        MYSQL_TABLE_OPTIONS,
    )

    session_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    #: The physical Environment that may attach, recover or release this runtime.
    environment_name: Mapped[str] = mapped_column(KeyText(128))
    user_id: Mapped[str] = mapped_column(KeyText(512))
    org_id: Mapped[str | None] = mapped_column(payload_text())
    session_kind: Mapped[str] = mapped_column(KeyText(32))
    engine_kind: Mapped[str | None] = mapped_column(payload_text())
    #: ``agent`` or ``assistant``: which subject the conversation runs as.
    workspace_kind: Mapped[str] = mapped_column(payload_text())
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.agent_id"))
    assistant_id: Mapped[str | None] = mapped_column(ForeignKey("assistants.assistant_id"))
    #: The Assistant workspace's own bootstrap sessions name the workspace here.
    owner_type: Mapped[str | None] = mapped_column(KeyText(32))
    owner_id: Mapped[str | None] = mapped_column(KeyText(512))
    source_type: Mapped[str | None] = mapped_column(payload_text())
    deployment_name: Mapped[str | None] = mapped_column(payload_text())
    hidden: Mapped[bool] = mapped_column(server_default=text("false"))

    state: Mapped[str] = mapped_column(KeyText(32))
    deleted: Mapped[bool] = mapped_column(server_default=text("false"))
    deleted_at: Mapped[datetime | None]
    deleted_by: Mapped[str | None] = mapped_column(payload_text())
    archived_at: Mapped[datetime | None]
    archived_by: Mapped[str | None] = mapped_column(payload_text())

    title: Mapped[str | None] = mapped_column(payload_text())
    #: ``user`` when a person set the title, ``generated`` when the title service did.
    title_source: Mapped[str | None] = mapped_column(payload_text())
    title_response_id: Mapped[str | None] = mapped_column(payload_text())
    title_user_message_id: Mapped[str | None] = mapped_column(payload_text())
    title_assistant_message_id: Mapped[str | None] = mapped_column(payload_text())
    title_generated_at: Mapped[datetime | None]
    title_model_name: Mapped[str | None] = mapped_column(payload_text())
    #: The title service's single generation claim: ``generating``,
    #: ``completed``, ``failed`` or ``skipped``, for one response.
    title_generation_status: Mapped[str | None] = mapped_column(payload_text())
    title_generation_response_id: Mapped[str | None] = mapped_column(payload_text())
    title_generation_response_index: Mapped[int | None]
    title_generation_started_at: Mapped[datetime | None]
    title_generation_completed_at: Mapped[datetime | None]
    title_generation_failed_at: Mapped[datetime | None]
    title_generation_skipped_at: Mapped[datetime | None]
    title_generation_error: Mapped[str | None] = mapped_column(payload_text())
    title_generation_skip_reason: Mapped[str | None] = mapped_column(payload_text())

    permission_mode: Mapped[str | None] = mapped_column(payload_text())
    model_name: Mapped[str | None] = mapped_column(payload_text())
    slash_commands: Mapped[list[Any] | None]
    slash_command_details: Mapped[list[Any] | None]
    engine_capabilities: Mapped[dict[str, Any] | None]
    #: Last connected runtime's observed component versions and instance coordinates.
    runtime_versions: Mapped[dict[str, Any] | None]

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    #: The session list's order: moves when someone sends or the runtime answers.
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    read_at: Mapped[datetime | None]
    last_output_at: Mapped[datetime | None]
    runtime_status: Mapped[str | None] = mapped_column(payload_text())
    runtime_status_at: Mapped[datetime | None]

    # The box this conversation runs in.
    sandbox_backend: Mapped[str | None] = mapped_column(payload_text())
    sandbox_id: Mapped[str | None] = mapped_column(KeyText(128))
    sandbox_generation: Mapped[str | None] = mapped_column(payload_text())
    sandbox_callback_token: Mapped[str | None] = mapped_column(payload_text())
    expires_at: Mapped[datetime | None]
    engine_session_key: Mapped[str | None] = mapped_column(payload_text())
    runtime_identity: Mapped[dict[str, Any] | None]
    runtime_binding: Mapped[dict[str, Any] | None]
    terminal_cwd: Mapped[str | None] = mapped_column(payload_text())
    workspace_id: Mapped[str | None] = mapped_column(payload_text())
    sandbox_parked_at: Mapped[datetime | None]
    #: A browser endpoint keeps this box out of conversation-idle parking.
    exposed_sandbox_id: Mapped[str | None] = mapped_column(payload_text())
    #: When the last start that left the conversation open failed; inputs
    #: accepted before it are failed rather than handed to another attempt.
    runtime_start_failed_at: Mapped[datetime | None]
    sandbox_resumed_at: Mapped[datetime | None]
    sandbox_liveness_suspect_at: Mapped[datetime | None]
    undestroyed_sandbox_ids: Mapped[list[str]] = mapped_column(
        StringList(), server_default=EmptyListDefault(text_array=True)
    )
    startup_allocation: Mapped[dict[str, Any] | None]
    retained_startup_allocations: Mapped[list[Any]] = mapped_column(server_default=EmptyListDefault())
    runtime_unavailable: Mapped[bool] = mapped_column(server_default=text("false"))
    recovery_reason: Mapped[str | None] = mapped_column(payload_text())
    last_error: Mapped[str | None] = mapped_column(payload_text())
    startup_progress: Mapped[str | None] = mapped_column(payload_text())
    startup_user_action: Mapped[dict[str, Any] | None]
    runtime_recovery_owner: Mapped[str | None] = mapped_column(payload_text())
    startup_provider_assignment: Mapped[str | None] = mapped_column(payload_text())

    #: Where the session's reader resumes the runtime's events (opaque).
    reader_cursor: Mapped[dict[str, Any] | None]

    artifact_grant: Mapped[dict[str, Any] | None]
    artifact_run: Mapped[dict[str, Any] | None]


class SessionVaultBinding(Base):
    """A vault the session's runtime may draw credentials from, in priority order."""

    __tablename__ = "session_vault_bindings"
    __table_args__ = (
        MYSQL_TABLE_OPTIONS,
    )

    session_id: Mapped[str] = mapped_column(ForeignKey(_SESSION_FK, ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(primary_key=True)
    #: A deleted vault leaves the conversations that drew on it; their binding goes with it.
    vault_id: Mapped[str] = mapped_column(ForeignKey("vaults.vault_id", ondelete="CASCADE"))


class SessionShare(Base):
    """A public read-only link to a session."""

    __tablename__ = "session_shares"
    __table_args__ = (
        *boolean_checks("enabled", "allow_download"),
        MYSQL_TABLE_OPTIONS,
    )

    token: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey(_SESSION_FK, ondelete="CASCADE"), unique=True
    )
    enabled: Mapped[bool]
    allow_download: Mapped[bool] = mapped_column(server_default=text("false"))
    expires_at: Mapped[datetime | None]
    created_by: Mapped[str | None] = mapped_column(payload_text())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SessionReader(Base):
    """The lease of the one process allowed to read a session's runtime and record its facts."""

    __tablename__ = "session_readers"
    __table_args__ = (
        MYSQL_TABLE_OPTIONS,
    )

    session_id: Mapped[str] = mapped_column(ForeignKey(_SESSION_FK, ondelete="CASCADE"), primary_key=True)
    owner: Mapped[str] = mapped_column(KeyText(128))
    generation: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime]


class SessionInput(Base):
    """One input, held by the platform until the runtime durably admits it.

    ``state``: ``pending`` (awaiting the delivery receipt), ``delivered``
    (the runtime received it), ``admitted`` (the runtime durably holds it) or
    ``failed`` (refused; ``failure`` says why). A runtime replaced before it
    admitted an input gets the input again; one replaced after does not.

    A user message imported from another system's history (``source``
    ``import``) is ``imported``: it is shown in the conversation and never
    handed to a runtime. ``import_record`` keeps where it came from.
    """

    __tablename__ = "session_inputs"
    __table_args__ = (
        UniqueConstraint("session_id", "client_message_id"),
        GeneratedOrderIndex("seq", name="ix_session_inputs_generated_seq").ddl_if(dialect="mysql"),
        Index(None, "session_id", "created_at", "seq"),
        Index("ix_session_inputs_conversation_order", "session_id", "conversation_order", "seq"),
        Index(
            "ix_session_inputs_open",
            "session_id",
            "seq",
            postgresql_where=text("state IN ('pending', 'delivered')"),
        ).ddl_if(dialect="postgresql"),
        Index("ix_session_inputs_open_mysql", "session_id", "state", "seq").ddl_if(dialect="mysql"),
        MYSQL_TABLE_OPTIONS,
    )

    input_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey(_SESSION_FK, ondelete="CASCADE"))
    #: Acceptance order across the whole store; a session's inputs order by it.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    client_message_id: Mapped[str] = mapped_column(KeyText(1024))
    author_user_id: Mapped[str | None] = mapped_column(payload_text())
    source: Mapped[str | None] = mapped_column(payload_text())
    content: Mapped[str] = mapped_column(payload_text())
    content_blocks: Mapped[list[Any] | None]
    state: Mapped[str] = mapped_column(KeyText(32), server_default=text("'pending'"))
    response_id: Mapped[str | None] = mapped_column(payload_text())
    failure: Mapped[dict[str, Any] | None]
    #: An imported message's source identity and parent, and the digest of
    #: what was imported.
    import_record: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    delivered_at: Mapped[datetime | None]
    admitted_at: Mapped[datetime | None]
    #: Position of the native user message in the reader's ordered output log.
    #: None while an input has no observed native conversation message.
    conversation_order: Mapped[int | None] = mapped_column(BigInteger)


class SessionControl(Base):
    """A person's request to steer the running runtime: interrupt it, or stop one of its sub-agents.

    Recorded only while a reader holds the session's lease, and applied only by
    that reader generation (``reader_generation``): a request is about what is
    running now, so a later reader does not replay it. ``state``: ``pending``,
    ``done``, ``failed`` (``error`` says why) or ``expired`` (its reader ended
    before applying it).
    """

    __tablename__ = "session_controls"
    __table_args__ = (
        Index(
            "ix_session_controls_pending",
            "session_id",
            "requested_at",
            postgresql_where=text("state = 'pending'"),
        ),
        MYSQL_TABLE_OPTIONS,
    )

    control_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey(_SESSION_FK, ondelete="CASCADE"))
    #: ``interrupt`` or ``stop_child`` (``target`` is the runtime's sub-agent handle).
    kind: Mapped[str] = mapped_column(payload_text())
    target: Mapped[str | None] = mapped_column(payload_text())
    reader_generation: Mapped[int] = mapped_column(BigInteger)
    requested_by: Mapped[str | None] = mapped_column(payload_text())
    state: Mapped[str] = mapped_column(payload_text(), server_default=TextDefault("pending"))
    error: Mapped[str | None] = mapped_column(payload_text())
    requested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    applied_at: Mapped[datetime | None]


class SessionResponse(Base):
    """A response the runtime produced, as its reader recorded it.

    A reply imported from another system's history has ``origin`` ``import``
    and is ``ended`` from the start; no runtime produced it, so its browser
    parts are kept in ``import_record`` rather than in the output log, which
    only the session's reader writes.
    """

    __tablename__ = "session_responses"
    __table_args__ = (
        Index(None, "session_id", "started_at", "seq"),
        Index("ix_session_responses_conversation_order", "session_id", "conversation_order", "seq"),
        Index(
            "ix_session_responses_open",
            "session_id",
            postgresql_where=text("status <> 'ended'"),
        ),
        GeneratedOrderIndex("seq", name="ix_session_responses_generated_seq").ddl_if(dialect="mysql"),
        MYSQL_TABLE_OPTIONS,
    )

    session_id: Mapped[str] = mapped_column(ForeignKey(_SESSION_FK, ondelete="CASCADE"), primary_key=True)
    response_id: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    #: ``input`` or ``engine`` (see ``engine/runtime.py``), or ``import``.
    origin: Mapped[str] = mapped_column(payload_text())
    #: ``running``, ``waiting`` or ``ended``.
    status: Mapped[str] = mapped_column(payload_text())
    #: ``completed``, ``failed`` or ``cancelled`` once ended. An imported reply
    #: whose source never recorded its ending has none.
    outcome: Mapped[str | None] = mapped_column(payload_text())
    native_reason: Mapped[str | None] = mapped_column(payload_text())
    usage: Mapped[dict[str, Any] | None]
    error: Mapped[dict[str, Any] | None]
    #: An imported reply's source identity, completion, parent, digest and
    #: browser parts.
    import_record: Mapped[dict[str, Any] | None]
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    ended_at: Mapped[datetime | None]
    #: First native content position, independent of response lifecycle timing.
    conversation_order: Mapped[int | None] = mapped_column(BigInteger)


class SessionResponseInput(Base):
    """An input a response answers."""

    __tablename__ = "session_response_inputs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "response_id"],
            ["session_responses.session_id", "session_responses.response_id"],
            ondelete="CASCADE",
        ),
        Index(None, "input_id"),
        MYSQL_TABLE_OPTIONS,
    )

    session_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    response_id: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    input_id: Mapped[str] = mapped_column(
        ForeignKey("session_inputs.input_id", ondelete="CASCADE"), primary_key=True
    )


class SessionInteraction(Base):
    """An approval or question a response waited on.

    ``status``: ``open``, ``answered``, ``withdrawn`` or ``expired``. The
    runtime holds the interaction itself. A person's answer is stored on the
    open row and handed to the runtime by the session's reader; the runtime's
    acceptance closes the row as ``answered``.
    """

    __tablename__ = "session_interactions"
    __table_args__ = (
        Index(
            "ix_session_interactions_open",
            "session_id",
            postgresql_where=text("status = 'open'"),
        ),
        MYSQL_TABLE_OPTIONS,
    )

    session_id: Mapped[str] = mapped_column(ForeignKey(_SESSION_FK, ondelete="CASCADE"), primary_key=True)
    interaction_id: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    response_id: Mapped[str] = mapped_column(payload_text())
    contract: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(payload_text())
    #: A person's answer, set once; the session's reader hands it to the runtime.
    answer: Mapped[dict[str, Any] | None]
    answered_by: Mapped[str | None] = mapped_column(payload_text())
    answered_at: Mapped[datetime | None]
    opened_at: Mapped[datetime] = mapped_column(server_default=func.now())
    closed_at: Mapped[datetime | None]


class SessionOutput(Base):
    """One browser part, in the order the session's reader relayed it.

    Only the holder of the session's reader lease writes here. It numbers the
    parts itself, continuing from the highest stored ``seq``, and commits them
    together with the runtime cursor they were read up to, inside a statement
    that checks its lease generation. A reader that takes over resumes from
    that cursor and numbers the same events the same way, so a follower that
    saw a part live and then reads it from here sees one part, not two.
    """

    __tablename__ = "session_output"
    __table_args__ = (
        Index(None, "session_id", "response_id", "seq"),
        # The child-run view reads only these parts, and the console polls it
        # while children run; without this it would scan the whole log.
        Index(
            "ix_session_output_child_runs",
            "session_id",
            "seq",
            postgresql_where=text("part->>'type' = 'data-subagent'"),
        ),
        MYSQL_TABLE_OPTIONS,
    )

    session_id: Mapped[str] = mapped_column(ForeignKey(_SESSION_FK, ondelete="CASCADE"), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    response_id: Mapped[str | None] = mapped_column(KeyText(512))
    part: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TranscriptScope(Base):
    """One transcript file a session's runtime keeps: its conversation or one sub-file.

    A scope is keyed the way the runtime names it, ``(project_key,
    native_session_id, subpath)``, inside one platform session, which is the
    tenant fence every read and write names. ``native_session_id`` is the
    runtime's own conversation id, not the platform session's; ``subpath`` is
    ``""`` for the main transcript, otherwise a sub-agent's transcript or a
    mirrored log's path. Entries are numbered ``1..last_sequence`` without
    gaps. ``updated_at`` is when the scope's last batch was stored.
    """

    __tablename__ = "transcript_scopes"
    __table_args__ = (
        UniqueConstraint("platform_session_id", "project_key", "native_session_id", "subpath"),
        MYSQL_TABLE_OPTIONS,
    )

    scope_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    platform_session_id: Mapped[str] = mapped_column(ForeignKey(_SESSION_FK, ondelete="CASCADE"))
    project_key: Mapped[str] = mapped_column(KeyText(1024))
    native_session_id: Mapped[str] = mapped_column(KeyText(512))
    subpath: Mapped[str] = mapped_column(KeyText(1024), server_default=text("''"))
    last_sequence: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TranscriptEntry(Base):
    """One transcript entry as the runtime wrote it, at its place in its scope.

    Entries arrive in batches the sender names with ``append_id``;
    ``batch_index`` is an entry's place in its batch. One statement stores a
    whole batch, so a re-sent batch finds every row it stored, and the unique
    key refuses a second copy of a batch two deliveries race to store.
    """

    __tablename__ = "transcript_entries"
    __table_args__ = (
        UniqueConstraint("scope_id", "append_id", "batch_index"),
        MYSQL_TABLE_OPTIONS,
    )

    scope_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("transcript_scopes.scope_id", ondelete="CASCADE"),
        primary_key=True,
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    append_id: Mapped[str] = mapped_column(KeyText(128))
    batch_index: Mapped[int]
    entry_json: Mapped[str] = mapped_column(payload_text())


class TranscriptChildRunIndex(Base):
    """Engine-owned child relationship metadata through one native scope position.

    Raw transcript entries remain authoritative. Listing children advances this
    derived projection in bounded pages instead of decoding their full history
    on every request.
    """

    __tablename__ = "transcript_child_run_indexes"
    __table_args__ = (MYSQL_TABLE_OPTIONS,)

    scope_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("transcript_scopes.scope_id", ondelete="CASCADE"), primary_key=True,
    )
    engine_kind: Mapped[str] = mapped_column(KeyText(64), primary_key=True)
    through_seq: Mapped[int] = mapped_column(BigInteger)
    entries: Mapped[list[dict[str, Any]]] = mapped_column(payload_json())


class RuntimeStateHead(Base):
    """The published snapshot of one owner's engine state archive.

    ``archive`` is the kind of archive (the engine's native session store, or
    an Assistant profile's state); ``owner_key`` is the digest of the owner's
    four fields, kept beside it. ``snapshot_id`` is derived from the bytes and
    from the snapshot it replaced (``previous_snapshot_id``), so a writer that
    read an older head cannot publish over a newer one.
    """

    __tablename__ = "runtime_state_heads"
    __table_args__ = (
        CheckConstraint(
            "archive IN ('native_session_store', 'profile_state')", name="archive"
        ),
        CheckConstraint(
            "subject_kind IN ('assistant', 'agent', 'session')", name="subject_kind"
        ),
        MYSQL_TABLE_OPTIONS,
    )

    archive: Mapped[str] = mapped_column(KeyText(32), primary_key=True)
    owner_key: Mapped[str] = mapped_column(KeyText(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(payload_text())
    subject_kind: Mapped[str] = mapped_column(payload_text())
    subject_id: Mapped[str] = mapped_column(payload_text())
    engine_kind: Mapped[str] = mapped_column(payload_text())
    snapshot_id: Mapped[str] = mapped_column(payload_text())
    previous_snapshot_id: Mapped[str | None] = mapped_column(payload_text())
    sha256: Mapped[str] = mapped_column(payload_text())
    size: Mapped[int] = mapped_column(BigInteger)
    chunk_count: Mapped[int]


class RuntimeStateChunk(Base):
    """One chunk of a head's snapshot, as raw bytes.

    Only the published snapshot's chunks are kept: the publish that moves a
    head deletes the chunks of the snapshot it replaced in the same
    transaction.
    """

    __tablename__ = "runtime_state_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["archive", "owner_key"],
            ["runtime_state_heads.archive", "runtime_state_heads.owner_key"],
            ondelete="CASCADE",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    archive: Mapped[str] = mapped_column(KeyText(32), primary_key=True)
    owner_key: Mapped[str] = mapped_column(KeyText(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(KeyText(64), primary_key=True)
    chunk_index: Mapped[int] = mapped_column(primary_key=True)
    data: Mapped[bytes] = mapped_column(binary_payload())


class TerminalCommand(Base):
    """A command run in the session's web terminal.

    A session's terminal runs one command at a time:
    ``ix_terminal_commands_running`` admits one ``running`` row per session.
    The process running the command holds the row under a lease
    (``expires_at``) and extends it while the command runs, so a running row
    whose lease has passed belongs to a process that stopped, and whoever finds
    it may end it. ``interrupt_requested_at`` is a person's request to stop
    the command; the process running it carries the request out.

    A session keeps only its latest command. Starting one deletes the ended
    commands before it, with their output; the new row inherits the shell the
    terminal uses (``execution_id``) and starts where the previous command
    ended (``next_cwd``) unless the request names a directory.
    """

    __tablename__ = "terminal_commands"
    __table_args__ = (
        Index(None, "session_id", "started_at"),
        Index(
            "ix_terminal_commands_running",
            "session_id",
            unique=True,
            postgresql_where=_RUNNING_TERMINAL,
        ).ddl_if(dialect="postgresql"),
        NullableUnique(
            "session_id", predicate=_RUNNING_TERMINAL,
            marker="_running_slot", name="ix_terminal_commands_running",
        ).ddl_if(dialect="mysql"),
        MYSQL_TABLE_OPTIONS,
    )

    command_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey(_SESSION_FK, ondelete="CASCADE"))
    command: Mapped[str] = mapped_column(payload_text())
    #: Where the command runs.
    cwd: Mapped[str | None] = mapped_column(payload_text())
    #: Where the shell was when the command ended, and so where the next starts.
    next_cwd: Mapped[str | None] = mapped_column(payload_text())
    #: ``running``, ``exited``, ``interrupted`` or ``failed``.
    status: Mapped[str] = mapped_column(KeyText(32))
    exit_code: Mapped[int | None]
    #: The box process the terminal's shell runs as; ``None`` for a terminal in
    #: an isolation session, which ``sessions.runtime_identity`` names.
    execution_id: Mapped[str | None] = mapped_column(payload_text())
    expires_at: Mapped[datetime]
    interrupt_requested_at: Mapped[datetime | None]
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    ended_at: Mapped[datetime | None]


class TerminalOutput(Base):
    """One output event of a terminal command, numbered by the database."""

    __tablename__ = "terminal_output"
    __table_args__ = (
        Index(None, "command_id", "seq"),
        MYSQL_TABLE_OPTIONS,
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("terminal_commands.command_id", ondelete="CASCADE")
    )
    payload: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProcessSummary(Base):
    """A generated summary of the work inside one response; the row is also the generation claim."""

    __tablename__ = "process_summaries"
    __table_args__ = (
        Index(
            "ix_process_summaries_generating",
            "expires_at",
            postgresql_where=text("status = 'generating'"),
        ),
        *boolean_checks("response_completed"),
        MYSQL_TABLE_OPTIONS,
    )

    session_id: Mapped[str] = mapped_column(ForeignKey(_SESSION_FK, ondelete="CASCADE"), primary_key=True)
    response_id: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    status: Mapped[str] = mapped_column(payload_text())
    claim_token: Mapped[str | None] = mapped_column(payload_text())
    through_seq: Mapped[int | None] = mapped_column(BigInteger)
    response_completed: Mapped[bool] = mapped_column(server_default=text("false"))
    summary: Mapped[str | None] = mapped_column(payload_text())
    error: Mapped[str | None] = mapped_column(payload_text())
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ToolCard(Base):
    """A card a platform tool recorded for one session or one Assistant.

    A card belongs to the scope of the address its tool was called through:
    an Agent's address serves one session, while an Assistant's serves all of
    its owner's sessions without saying which, so its cards belong to the
    Assistant.
    """

    __tablename__ = "tool_cards"
    __table_args__ = (
        CheckConstraint(NullCount("session_id", "assistant_id", nonnull=True) == 1, name="one_owner"),
        MYSQL_TABLE_OPTIONS,
    )

    card_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey(_SESSION_FK, ondelete="CASCADE"))
    assistant_id: Mapped[str | None] = mapped_column(
        ForeignKey("assistants.assistant_id", ondelete="CASCADE")
    )
    server: Mapped[str] = mapped_column(payload_text())
    tool: Mapped[str] = mapped_column(payload_text())
    kind: Mapped[str] = mapped_column(payload_text())
    data: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PlatformMCPBinding(Base):
    """How the platform MCP server routes a request from one box to its subject.

    ``deployment_id`` is the routing address the box presents, not a
    :class:`~astrabox.persistence.sql.models.deployments.Deployment`. Two kinds
    of address have a row:

    * ``assistant_workspace`` (``source`` ``platform_runtime``): the address an
      Assistant's engine serves all of its owner's sessions through. It goes
      with the Assistant's workspace, which an Assistant's deletion removes.
    * ``session`` (``source`` ``prepared_slot``): the address a prepared unit's
      engine was started with before any session existed. ``session_id`` is
      empty until a session claims the unit; the row goes with the unit when
      it is discarded unclaimed, and with the session when it is deleted.

    A session started without a prepared unit is addressed by its own id and
    has no row.
    """

    __tablename__ = "platform_mcp_bindings"
    __table_args__ = (
        CheckConstraint(
            "(scope_kind = 'assistant_workspace' AND source = 'platform_runtime'"
            " AND assistant_id IS NOT NULL AND user_id IS NOT NULL AND session_id IS NULL)"
            " OR (scope_kind = 'session' AND source = 'prepared_slot' AND assistant_id IS NULL)",
            name="kind",
        ),
        Index(None, "session_id"),
        Index(None, "assistant_id"),
        Index(
            "ix_platform_mcp_bindings_unclaimed_slots",
            "created_at",
            postgresql_where=text("source = 'prepared_slot' AND session_id IS NULL"),
        ),
        MYSQL_TABLE_OPTIONS,
    )

    deployment_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    scope_kind: Mapped[str] = mapped_column(payload_text())
    source: Mapped[str] = mapped_column(payload_text())
    #: The Agent a prepared unit was prepared for, or the Assistant's harness.
    template_name: Mapped[str | None] = mapped_column(payload_text())
    user_id: Mapped[str | None] = mapped_column(payload_text())
    conversation_user_id: Mapped[str | None] = mapped_column(payload_text())
    assistant_id: Mapped[str | None] = mapped_column(
        ForeignKey("assistant_workspaces.assistant_id", ondelete="CASCADE")
    )
    session_id: Mapped[str | None] = mapped_column(ForeignKey(_SESSION_FK, ondelete="CASCADE"))
    sandbox_id: Mapped[str | None] = mapped_column(payload_text())
    sandbox_backend: Mapped[str | None] = mapped_column(payload_text())
    #: Where an Assistant workspace's files are read from for artifacts.
    root_path: Mapped[str | None] = mapped_column(payload_text())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
