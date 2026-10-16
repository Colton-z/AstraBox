"""The schema 0.1.9 ships: one typed table per entity.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('agent_categories',
    sa.Column('category_id', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('name_key', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('category_id', name=op.f('pk_agent_categories')),
    sa.UniqueConstraint('name_key', name=op.f('uq_agent_categories_name_key'))
    )
    op.create_table('artifact_budgets',
    sa.Column('budget_key', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('tokens', sa.Float(), nullable=True),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('budget_key', name=op.f('pk_artifact_budgets'))
    )
    op.create_index(op.f('ix_artifact_budgets_kind_updated_at'), 'artifact_budgets', ['kind', 'updated_at'], unique=False)
    op.create_table('artifacts',
    sa.Column('artifact_id', sa.Text(), nullable=False),
    sa.Column('owner_id', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('language', sa.Text(), nullable=True),
    sa.Column('current_version', sa.Integer(), nullable=False),
    sa.Column('manifest', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('origin_session_kind', sa.Text(), nullable=True),
    sa.Column('origin_session_id', sa.Text(), nullable=True),
    sa.Column('origin_agent_id', sa.Text(), nullable=True),
    sa.Column('origin_assistant_id', sa.Text(), nullable=True),
    sa.Column('revision', sa.BigInteger(), nullable=False),
    sa.Column('event_seq', sa.BigInteger(), nullable=False),
    sa.Column('document_count', sa.Integer(), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('artifact_id', name=op.f('pk_artifacts'))
    )
    op.create_index(op.f('ix_artifacts_origin_assistant_id_updated_at_artifact_id'), 'artifacts', ['origin_assistant_id', 'updated_at', 'artifact_id'], unique=False, postgresql_where=sa.text('origin_assistant_id IS NOT NULL'))
    op.create_index(op.f('ix_artifacts_origin_session_id_updated_at_artifact_id'), 'artifacts', ['origin_session_id', 'updated_at', 'artifact_id'], unique=False, postgresql_where=sa.text('origin_session_id IS NOT NULL'))
    op.create_index(op.f('ix_artifacts_owner_id_origin_agent_id_updated_at_artifact_id'), 'artifacts', ['owner_id', 'origin_agent_id', 'updated_at', 'artifact_id'], unique=False, postgresql_where=sa.text('origin_agent_id IS NOT NULL'))
    op.create_index(op.f('ix_artifacts_owner_id_updated_at_artifact_id'), 'artifacts', ['owner_id', 'updated_at', 'artifact_id'], unique=False)
    op.create_table('environments',
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('engine_kind', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('sandbox_backend', sa.Text(), nullable=True),
    sa.Column('runtime_template_name', sa.Text(), nullable=True),
    sa.Column('network_type', sa.Text(), nullable=False),
    sa.Column('network_allowed_hosts', postgresql.ARRAY(sa.Text()), nullable=True),
    sa.Column('network_allow_mcp_servers', sa.Boolean(), nullable=True),
    sa.Column('idle_action', sa.Text(), nullable=False),
    sa.Column('sandbox_tenancy', sa.Text(), nullable=True),
    sa.Column('sandbox_permission_level', sa.Text(), nullable=True),
    sa.Column('endpoint_provider', sa.Text(), nullable=True),
    sa.Column('provider_base_url', sa.Text(), nullable=True),
    sa.Column('provider_api_key_ref', sa.Text(), nullable=True),
    sa.Column('provider_api_key_secret_name', sa.Text(), nullable=True),
    sa.Column('tracing_enabled', sa.Boolean(), nullable=True),
    sa.Column('tracing_endpoint', sa.Text(), nullable=True),
    sa.Column('tracing_headers', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('tracing_auth_token_ref', sa.Text(), nullable=True),
    sa.Column('tracing_auth_token_secret_name', sa.Text(), nullable=True),
    sa.Column('tracing_environment', sa.Text(), nullable=True),
    sa.Column('tracing_signals', postgresql.ARRAY(sa.Text()), nullable=True),
    sa.Column('tracing_log_user_prompt', sa.Boolean(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_by', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('name', name=op.f('pk_environments'))
    )
    op.create_table('installation',
    sa.Column('singleton', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('installation_id', sa.Text(), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("installation_id <> ''", name=op.f('ck_installation_installation_id_present')),
    sa.CheckConstraint('singleton', name=op.f('ck_installation_singleton')),
    sa.PrimaryKeyConstraint('singleton', name=op.f('pk_installation'))
    )
    op.create_table('mcp_client_tokens',
    sa.Column('token_id', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('org_id', sa.Text(), nullable=True),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('secret_digest', sa.Text(), nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_used_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('token_id', name=op.f('pk_mcp_client_tokens')),
    sa.UniqueConstraint('secret_digest', name=op.f('uq_mcp_client_tokens_secret_digest'))
    )
    op.create_index(op.f('ix_mcp_client_tokens_user_id_created_at'), 'mcp_client_tokens', ['user_id', 'created_at'], unique=False)
    op.create_table('mcp_servers',
    sa.Column('mcp_server_id', sa.Text(), nullable=False),
    sa.Column('org_id', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('transport', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.Text(), nullable=True),
    sa.Column('updated_by', sa.Text(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('mcp_server_id', name=op.f('pk_mcp_servers')),
    sa.UniqueConstraint('org_id', 'name', name=op.f('uq_mcp_servers_org_id_name'))
    )
    op.create_index(op.f('ix_mcp_servers_org_id_updated_at'), 'mcp_servers', ['org_id', 'updated_at'], unique=False)
    op.create_table('runtime_errors',
    sa.Column('signature', sa.Text(), nullable=False),
    sa.Column('logger', sa.Text(), nullable=False),
    sa.Column('severity', sa.Text(), nullable=False),
    sa.Column('error_type', sa.Text(), nullable=True),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('location', sa.Text(), nullable=False),
    sa.Column('traceback', sa.Text(), nullable=True),
    sa.Column('count', sa.BigInteger(), nullable=False),
    sa.Column('first_seen', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('last_seen', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('signature', name=op.f('pk_runtime_errors'))
    )
    op.create_index(op.f('ix_runtime_errors_last_seen'), 'runtime_errors', ['last_seen'], unique=False)
    op.create_table('runtime_state_heads',
    sa.Column('archive', sa.Text(), nullable=False),
    sa.Column('owner_key', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('subject_kind', sa.Text(), nullable=False),
    sa.Column('subject_id', sa.Text(), nullable=False),
    sa.Column('engine_kind', sa.Text(), nullable=False),
    sa.Column('snapshot_id', sa.Text(), nullable=False),
    sa.Column('previous_snapshot_id', sa.Text(), nullable=True),
    sa.Column('sha256', sa.Text(), nullable=False),
    sa.Column('size', sa.BigInteger(), nullable=False),
    sa.Column('chunk_count', sa.Integer(), nullable=False),
    sa.CheckConstraint("archive IN ('native_session_store', 'profile_state')", name=op.f('ck_runtime_state_heads_archive')),
    sa.CheckConstraint("subject_kind IN ('assistant', 'agent', 'session')", name=op.f('ck_runtime_state_heads_subject_kind')),
    sa.PrimaryKeyConstraint('archive', 'owner_key', name=op.f('pk_runtime_state_heads'))
    )
    op.create_table('sandbox_capacity',
    sa.Column('backend', sa.Text(), nullable=False),
    sa.Column('inventory_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('backend', name=op.f('pk_sandbox_capacity'))
    )
    op.create_table('secrets',
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('sealed', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('scope', 'key', name=op.f('pk_secrets'))
    )
    op.create_table('user_profiles',
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('user_id', name=op.f('pk_user_profiles'))
    )
    op.create_table('vaults',
    sa.Column('vault_id', sa.Text(), nullable=False),
    sa.Column('org_id', sa.Text(), nullable=False),
    sa.Column('created_by', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('archived_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('vault_id', name=op.f('pk_vaults'))
    )
    op.create_index(op.f('ix_vaults_org_id_created_at'), 'vaults', ['org_id', 'created_at'], unique=False)
    op.create_table('agents',
    sa.Column('agent_id', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('name_key', sa.Text(collation='C'), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=True),
    sa.Column('icon', sa.Text(), nullable=True),
    sa.Column('tags', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('use_cases', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('system', sa.Text(), nullable=True),
    sa.Column('engine_options', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('artifacts_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('skills', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('mcp_servers', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('packages', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('default_repo', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('terminal_panel', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('diff_panel', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('exposure_mode', sa.Text(), nullable=True),
    sa.Column('idle_hibernate_seconds', sa.Integer(), nullable=True),
    sa.Column('prepared_capacity', sa.Integer(), server_default='0', nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('environment_name', sa.Text(), nullable=True),
    sa.Column('visibility', sa.Text(), server_default='private', nullable=False),
    sa.Column('admins', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('allowed_user_ids', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('catalog_skill_ids', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('catalog_skills', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('state', sa.Text(), server_default='ACTIVE', nullable=False),
    sa.Column('created_by', sa.Text(), nullable=False),
    sa.Column('org_id', sa.Text(), nullable=True),
    sa.Column('updated_by', sa.Text(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("state = 'DELETED' OR environment_name IS NOT NULL", name=op.f('ck_agents_live_environment')),
    sa.CheckConstraint("state IN ('ACTIVE', 'DELETING', 'DELETED')", name=op.f('ck_agents_state')),
    sa.CheckConstraint("visibility IN ('public', 'private', 'allowlist')", name=op.f('ck_agents_visibility')),
    sa.CheckConstraint('prepared_capacity BETWEEN 0 AND 1000', name=op.f('ck_agents_prepared_capacity')),
    sa.ForeignKeyConstraint(['environment_name'], ['environments.name'], name=op.f('fk_agents_environment_name_environments'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('agent_id', name=op.f('pk_agents'))
    )
    op.create_index(op.f('ix_agents_environment_name_agent_id'), 'agents', ['environment_name', 'agent_id'], unique=False)
    op.create_index(op.f('ix_agents_name_key_agent_id'), 'agents', ['name_key', 'agent_id'], unique=False, postgresql_where=sa.text("state <> 'DELETED'"))
    op.create_index('ix_agents_prepared_capacity_sweep', 'agents', ['agent_id'], unique=False, postgresql_where=sa.text("prepared_capacity > 0 AND state = 'ACTIVE'"))
    op.create_index(op.f('ix_agents_updated_at_agent_id'), 'agents', ['updated_at', 'agent_id'], unique=False, postgresql_where=sa.text("state <> 'DELETED'"))
    op.create_table('artifact_documents',
    sa.Column('artifact_id', sa.Text(), nullable=False),
    sa.Column('path', sa.Text(collation='C'), nullable=False),
    sa.Column('collection', sa.Text(collation='C'), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('updated_by', sa.Text(), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.artifact_id'], name=op.f('fk_artifact_documents_artifact_id_artifacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('artifact_id', 'path', name=op.f('pk_artifact_documents'))
    )
    op.create_index(op.f('ix_artifact_documents_artifact_id_collection_path'), 'artifact_documents', ['artifact_id', 'collection', 'path'], unique=False)
    op.create_table('artifact_events',
    sa.Column('artifact_id', sa.Text(), nullable=False),
    sa.Column('seq', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('path', sa.Text(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.artifact_id'], name=op.f('fk_artifact_events_artifact_id_artifacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('artifact_id', 'seq', name=op.f('pk_artifact_events'))
    )
    op.create_index(op.f('ix_artifact_events_artifact_id'), 'artifact_events', ['artifact_id'], unique=True, postgresql_where=sa.text("kind = 'version'"))
    op.create_index(op.f('ix_artifact_events_artifact_id_path'), 'artifact_events', ['artifact_id', 'path'], unique=True, postgresql_where=sa.text("kind = 'data'"))
    op.create_index(op.f('ix_artifact_events_artifact_id_seq'), 'artifact_events', ['artifact_id', 'seq'], unique=False, postgresql_where=sa.text("kind = 'presence'"))
    op.create_index(op.f('ix_artifact_events_kind_created_at'), 'artifact_events', ['kind', 'created_at'], unique=False, postgresql_where=sa.text("kind IN ('room', 'presence')"))
    op.create_table('artifact_run_reservations',
    sa.Column('reservation_id', sa.Text(), nullable=False),
    sa.Column('budget_key', sa.Text(), nullable=False),
    sa.Column('artifact_id', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['budget_key'], ['artifact_budgets.budget_key'], name=op.f('fk_artifact_run_reservations_budget_key_artifact_budgets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('reservation_id', name=op.f('pk_artifact_run_reservations'))
    )
    op.create_index(op.f('ix_artifact_run_reservations_budget_key_created_at'), 'artifact_run_reservations', ['budget_key', 'created_at'], unique=False)
    op.create_table('artifact_sample_reservations',
    sa.Column('reservation_id', sa.Text(), nullable=False),
    sa.Column('budget_key', sa.Text(), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('released_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['budget_key'], ['artifact_budgets.budget_key'], name=op.f('fk_artifact_sample_reservations_budget_key_artifact_budgets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('reservation_id', name=op.f('pk_artifact_sample_reservations'))
    )
    op.create_index(op.f('ix_artifact_sample_reservations_budget_key_created_at'), 'artifact_sample_reservations', ['budget_key', 'created_at'], unique=False)
    op.create_table('artifact_versions',
    sa.Column('artifact_id', sa.Text(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.Text(), nullable=False),
    sa.Column('byte_size', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('language', sa.Text(), nullable=True),
    sa.Column('manifest', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('author_user_id', sa.Text(), nullable=False),
    sa.Column('author_kind', sa.Text(), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.artifact_id'], name=op.f('fk_artifact_versions_artifact_id_artifacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('artifact_id', 'version', name=op.f('pk_artifact_versions'))
    )
    op.create_table('artifact_viewers',
    sa.Column('artifact_id', sa.Text(), nullable=False),
    sa.Column('viewer_id', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.artifact_id'], name=op.f('fk_artifact_viewers_artifact_id_artifacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('artifact_id', 'viewer_id', name=op.f('pk_artifact_viewers'))
    )
    op.create_table('assistant_defaults',
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('environment_name', sa.Text(), nullable=True),
    sa.Column('model_name', sa.Text(), nullable=True),
    sa.Column('instructions', sa.Text(), nullable=True),
    sa.Column('skills', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('mcp_servers', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('packages', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('engine_options', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('extension_catalog', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('updated_by', sa.Text(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['environment_name'], ['environments.name'], name=op.f('fk_assistant_defaults_environment_name_environments')),
    sa.PrimaryKeyConstraint('scope', name=op.f('pk_assistant_defaults'))
    )
    op.create_table('assistants',
    sa.Column('assistant_id', sa.Text(), nullable=False),
    sa.Column('owner_id', sa.Text(), nullable=False),
    sa.Column('org_id', sa.Text(), nullable=True),
    sa.Column('is_primary', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('icon', sa.Text(), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('engine_kind', sa.Text(), nullable=False),
    sa.Column('environment_name', sa.Text(), nullable=True),
    sa.Column('permission_mode_default', sa.Text(), nullable=True),
    sa.Column('system', sa.Text(), nullable=True),
    sa.Column('timezone', sa.Text(), nullable=True),
    sa.Column('model_name', sa.Text(), nullable=True),
    sa.Column('mcp_servers', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('skills', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('packages', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('engine_options', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('extension_catalog', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('credentials_updated_by', sa.Text(), nullable=True),
    sa.Column('credentials_updated_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('main_session_id', sa.Text(), nullable=True),
    sa.Column('workspace_id', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('deleted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('deleted OR environment_name IS NOT NULL', name=op.f('ck_assistants_live_environment')),
    sa.ForeignKeyConstraint(['environment_name'], ['environments.name'], name=op.f('fk_assistants_environment_name_environments'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('assistant_id', name=op.f('pk_assistants')),
    sa.UniqueConstraint('workspace_id', name=op.f('uq_assistants_workspace_id'))
    )
    op.create_index(op.f('ix_assistants_environment_name'), 'assistants', ['environment_name'], unique=False)
    op.create_index('ix_assistants_live_primary', 'assistants', ['owner_id'], unique=True, postgresql_where=sa.text('is_primary AND NOT deleted'))
    op.create_index(op.f('ix_assistants_owner_id_updated_at_assistant_id'), 'assistants', ['owner_id', 'updated_at', 'assistant_id'], unique=False)
    op.create_table('runtime_state_chunks',
    sa.Column('archive', sa.Text(), nullable=False),
    sa.Column('owner_key', sa.Text(), nullable=False),
    sa.Column('snapshot_id', sa.Text(), nullable=False),
    sa.Column('chunk_index', sa.Integer(), nullable=False),
    sa.Column('data', sa.LargeBinary(), nullable=False),
    sa.ForeignKeyConstraint(['archive', 'owner_key'], ['runtime_state_heads.archive', 'runtime_state_heads.owner_key'], name=op.f('fk_runtime_state_chunks_archive_owner_key_runtime_state_heads'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('archive', 'owner_key', 'snapshot_id', 'chunk_index', name=op.f('pk_runtime_state_chunks'))
    )
    op.create_table('sandbox_inventory',
    sa.Column('backend', sa.Text(), nullable=False),
    sa.Column('sandbox_id', sa.Text(), nullable=False),
    sa.Column('cpu_millicores', sa.BigInteger(), nullable=False),
    sa.Column('memory_bytes', sa.BigInteger(), nullable=False),
    sa.Column('released', sa.Boolean(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['backend'], ['sandbox_capacity.backend'], name=op.f('fk_sandbox_inventory_backend_sandbox_capacity'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('backend', 'sandbox_id', name=op.f('pk_sandbox_inventory'))
    )
    op.create_table('sandbox_reservations',
    sa.Column('backend', sa.Text(), nullable=False),
    sa.Column('reservation_key', sa.Text(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('cpu_millicores', sa.BigInteger(), nullable=False),
    sa.Column('memory_bytes', sa.BigInteger(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=True),
    sa.Column('assignment_id', sa.Text(), nullable=False),
    sa.Column('sandbox_id', sa.Text(), nullable=True),
    sa.Column('reserved_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('box_created_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.CheckConstraint("(state = 'creating' AND sandbox_id IS NULL AND box_created_at IS NULL) OR (state = 'created' AND sandbox_id IS NOT NULL AND box_created_at IS NOT NULL)", name=op.f('ck_sandbox_reservations_state')),
    sa.ForeignKeyConstraint(['backend'], ['sandbox_capacity.backend'], name=op.f('fk_sandbox_reservations_backend_sandbox_capacity'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('backend', 'reservation_key', name=op.f('pk_sandbox_reservations'))
    )
    op.create_table('vault_credentials',
    sa.Column('credential_id', sa.Text(), nullable=False),
    sa.Column('vault_id', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=True),
    sa.Column('auth_type', sa.Text(), nullable=False),
    sa.Column('mcp_server_url', sa.Text(), nullable=True),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('secret_name', sa.Text(), nullable=True),
    sa.Column('username', sa.Text(), nullable=True),
    sa.Column('header_name', sa.Text(), nullable=True),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('refresh_token_endpoint', sa.Text(), nullable=True),
    sa.Column('refresh_client_id', sa.Text(), nullable=True),
    sa.Column('refresh_auth_method', sa.Text(), nullable=True),
    sa.Column('refresh_scope', sa.Text(), nullable=True),
    sa.Column('refresh_claimed_until', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('network_access', sa.Text(), nullable=True),
    sa.Column('allowed_hosts', postgresql.ARRAY(sa.Text()), nullable=True),
    sa.Column('inject_header', sa.Boolean(), nullable=True),
    sa.Column('inject_body', sa.Boolean(), nullable=True),
    sa.Column('allowed_methods', postgresql.ARRAY(sa.Text()), nullable=True),
    sa.Column('allowed_paths', postgresql.ARRAY(sa.Text()), nullable=True),
    sa.Column('allow_insecure_http', sa.Boolean(), nullable=True),
    sa.Column('archived_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(auth_type IN ('mcp_oauth', 'static_bearer', 'mcp_static_header') AND mcp_server_url IS NOT NULL AND url IS NULL AND secret_name IS NULL) OR (auth_type = 'http_basic' AND url IS NOT NULL AND mcp_server_url IS NULL AND secret_name IS NULL) OR (auth_type = 'environment_variable' AND secret_name IS NOT NULL AND mcp_server_url IS NULL AND url IS NULL)", name=op.f('ck_vault_credentials_one_key_per_type')),
    sa.ForeignKeyConstraint(['vault_id'], ['vaults.vault_id'], name=op.f('fk_vault_credentials_vault_id_vaults'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('credential_id', name=op.f('pk_vault_credentials'))
    )
    op.create_index(op.f('ix_vault_credentials_vault_id_created_at'), 'vault_credentials', ['vault_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_vault_credentials_vault_id_mcp_server_url'), 'vault_credentials', ['vault_id', 'mcp_server_url'], unique=True, postgresql_where=sa.text('archived_at IS NULL AND mcp_server_url IS NOT NULL'))
    op.create_index(op.f('ix_vault_credentials_vault_id_secret_name'), 'vault_credentials', ['vault_id', 'secret_name'], unique=True, postgresql_where=sa.text('archived_at IS NULL AND secret_name IS NOT NULL'))
    op.create_index(op.f('ix_vault_credentials_vault_id_url'), 'vault_credentials', ['vault_id', 'url'], unique=True, postgresql_where=sa.text('archived_at IS NULL AND url IS NOT NULL'))
    op.create_table('agent_box_admissions',
    sa.Column('agent_id', sa.Text(), nullable=False),
    sa.Column('sandbox_id', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('admitted_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], name=op.f('fk_agent_box_admissions_agent_id_agents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('agent_id', 'sandbox_id', 'session_id', name=op.f('pk_agent_box_admissions'))
    )
    op.create_index(op.f('ix_agent_box_admissions_sandbox_id'), 'agent_box_admissions', ['sandbox_id'], unique=False)
    op.create_table('agent_category_links',
    sa.Column('agent_id', sa.Text(), nullable=False),
    sa.Column('category_id', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], name=op.f('fk_agent_category_links_agent_id_agents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['category_id'], ['agent_categories.category_id'], name=op.f('fk_agent_category_links_category_id_agent_categories'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('agent_id', 'category_id', name=op.f('pk_agent_category_links'))
    )
    op.create_index(op.f('ix_agent_category_links_category_id'), 'agent_category_links', ['category_id'], unique=False)
    op.create_table('agent_connector_servers',
    sa.Column('agent_id', sa.Text(), nullable=False),
    sa.Column('connector_id', sa.Text(), nullable=False),
    sa.Column('hub_server_id', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], name=op.f('fk_agent_connector_servers_agent_id_agents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('agent_id', 'connector_id', name=op.f('pk_agent_connector_servers'))
    )
    op.create_table('agent_favorites',
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('agent_id', sa.Text(), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], name=op.f('fk_agent_favorites_agent_id_agents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'agent_id', name=op.f('pk_agent_favorites'))
    )
    op.create_index(op.f('ix_agent_favorites_agent_id'), 'agent_favorites', ['agent_id'], unique=False)
    op.create_table('agent_mcp_assignments',
    sa.Column('agent_id', sa.Text(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('item_id', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], name=op.f('fk_agent_mcp_assignments_agent_id_agents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('agent_id', 'provider', 'item_id', name=op.f('pk_agent_mcp_assignments'))
    )
    op.create_table('agent_prepared_slots',
    sa.Column('slot_id', sa.Text(), nullable=False),
    sa.Column('agent_id', sa.Text(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('placement', sa.Text(), nullable=False),
    sa.Column('sandbox_id', sa.Text(), nullable=False),
    sa.Column('sandbox_backend', sa.Text(), nullable=False),
    sa.Column('runtime_generation', sa.Text(), nullable=True),
    sa.Column('prepared_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('claimed_session_id', sa.Text(), nullable=True),
    sa.Column('claimed_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('retire_reason', sa.Text(), nullable=True),
    sa.Column('retiring_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('manifest', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.CheckConstraint("state IN ('prepared', 'claimed', 'retiring')", name=op.f('ck_agent_prepared_slots_state')),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], name=op.f('fk_agent_prepared_slots_agent_id_agents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('slot_id', name=op.f('pk_agent_prepared_slots'))
    )
    op.create_index(op.f('ix_agent_prepared_slots_agent_id_prepared_at'), 'agent_prepared_slots', ['agent_id', 'prepared_at'], unique=False)
    op.create_index(op.f('ix_agent_prepared_slots_sandbox_id'), 'agent_prepared_slots', ['sandbox_id'], unique=False)
    op.create_table('agent_retiring_client_pools',
    sa.Column('agent_id', sa.Text(), nullable=False),
    sa.Column('pool_name', sa.Text(), nullable=False),
    sa.Column('backend', sa.Text(), nullable=False),
    sa.Column('recorded_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], name=op.f('fk_agent_retiring_client_pools_agent_id_agents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('agent_id', 'pool_name', name=op.f('pk_agent_retiring_client_pools'))
    )
    op.create_table('agent_runtimes',
    sa.Column('agent_id', sa.Text(), nullable=False),
    sa.Column('sandbox_id', sa.Text(), nullable=True),
    sa.Column('sandbox_backend', sa.Text(), nullable=True),
    sa.Column('resident_generation', sa.Text(), nullable=True),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('runtime_generation', sa.Text(), nullable=True),
    sa.Column('sandbox_generation', sa.Text(), nullable=True),
    sa.Column('client_pool_epoch', sa.Text(), nullable=True),
    sa.Column('client_pool_name', sa.Text(), nullable=True),
    sa.Column('client_pool_backend', sa.Text(), nullable=True),
    sa.Column('prewarm_revision', sa.Text(), nullable=True),
    sa.Column('prepared_runtime_generation', sa.Text(), nullable=True),
    sa.Column('prepared_runtime_error', sa.Text(), nullable=True),
    sa.Column('resources_operation_provider', sa.Text(), nullable=True),
    sa.Column('resources_operation_id', sa.Text(), nullable=True),
    sa.Column('resources_operation_deadline', sa.Double(), nullable=True),
    sa.Column('slots_revision', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('admissions_revision', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('last_conversation_uid', sa.Integer(), nullable=True),
    sa.Column('workspace_id', sa.Text(), nullable=True),
    sa.Column('seats_per_box', sa.Integer(), server_default='0', nullable=False),
    sa.Column('capacity_last_miss_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.CheckConstraint('(client_pool_name IS NULL) = (client_pool_backend IS NULL)', name=op.f('ck_agent_runtimes_client_pool_backend')),
    sa.CheckConstraint('(sandbox_id IS NULL) = (sandbox_backend IS NULL)', name=op.f('ck_agent_runtimes_resident_backend')),
    sa.CheckConstraint('num_nulls(resources_operation_provider, resources_operation_id, resources_operation_deadline) IN (0, 3)', name=op.f('ck_agent_runtimes_resources_operation')),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], name=op.f('fk_agent_runtimes_agent_id_agents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('agent_id', name=op.f('pk_agent_runtimes')),
    sa.UniqueConstraint('workspace_id', name=op.f('uq_agent_runtimes_workspace_id'))
    )
    op.create_index('ix_agent_runtimes_resident_sweep', 'agent_runtimes', ['agent_id'], unique=False, postgresql_where=sa.text('sandbox_id IS NOT NULL'))
    op.create_index(op.f('ix_agent_runtimes_sandbox_id'), 'agent_runtimes', ['sandbox_id'], unique=False, postgresql_where=sa.text('sandbox_id IS NOT NULL'))
    op.create_table('agent_slot_gateway_entries',
    sa.Column('agent_id', sa.Text(), nullable=False),
    sa.Column('slot_id', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('endpoint_provider', sa.Text(), nullable=True),
    sa.Column('recorded_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], name=op.f('fk_agent_slot_gateway_entries_agent_id_agents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('agent_id', 'slot_id', name=op.f('pk_agent_slot_gateway_entries'))
    )
    op.create_table('agent_undestroyed_sandboxes',
    sa.Column('agent_id', sa.Text(), nullable=False),
    sa.Column('sandbox_id', sa.Text(), nullable=False),
    sa.Column('recorded_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], name=op.f('fk_agent_undestroyed_sandboxes_agent_id_agents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('agent_id', 'sandbox_id', name=op.f('pk_agent_undestroyed_sandboxes'))
    )
    op.create_table('agent_vault_bindings',
    sa.Column('agent_id', sa.Text(), nullable=False),
    sa.Column('vault_id', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], name=op.f('fk_agent_vault_bindings_agent_id_agents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vault_id'], ['vaults.vault_id'], name=op.f('fk_agent_vault_bindings_vault_id_vaults')),
    sa.PrimaryKeyConstraint('agent_id', 'vault_id', name=op.f('pk_agent_vault_bindings'))
    )
    op.create_index(op.f('ix_agent_vault_bindings_vault_id'), 'agent_vault_bindings', ['vault_id'], unique=False)
    op.create_table('artifact_shares',
    sa.Column('artifact_id', sa.Text(), nullable=False),
    sa.Column('token', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('access', sa.Text(), nullable=False),
    sa.Column('audience', sa.Text(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['artifact_id', 'version'], ['artifact_versions.artifact_id', 'artifact_versions.version'], name=op.f('fk_artifact_shares_artifact_id_version_artifact_versions'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.artifact_id'], name=op.f('fk_artifact_shares_artifact_id_artifacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('artifact_id', name=op.f('pk_artifact_shares')),
    sa.UniqueConstraint('token', name=op.f('uq_artifact_shares_token'))
    )
    op.create_table('artifact_version_contents',
    sa.Column('artifact_id', sa.Text(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['artifact_id', 'version'], ['artifact_versions.artifact_id', 'artifact_versions.version'], name=op.f('fk_artifact_version_contents_artifact_id_version_artifact_versions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('artifact_id', 'version', name=op.f('pk_artifact_version_contents'))
    )
    op.create_table('assistant_connectors',
    sa.Column('assistant_id', sa.Text(), nullable=False),
    sa.Column('connector_id', sa.Text(), nullable=False),
    sa.Column('hub_server_id', sa.Text(), nullable=True),
    sa.Column('turned_off', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.CheckConstraint('hub_server_id IS NOT NULL OR turned_off', name=op.f('ck_assistant_connectors_records_a_choice')),
    sa.ForeignKeyConstraint(['assistant_id'], ['assistants.assistant_id'], name=op.f('fk_assistant_connectors_assistant_id_assistants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('assistant_id', 'connector_id', name=op.f('pk_assistant_connectors'))
    )
    op.create_table('assistant_default_mcp_assignments',
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('item_id', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['scope'], ['assistant_defaults.scope'], name=op.f('fk_assistant_default_mcp_assignments_scope_assistant_defaults'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('scope', 'position', name=op.f('pk_assistant_default_mcp_assignments')),
    sa.UniqueConstraint('scope', 'provider', 'item_id', name=op.f('uq_assistant_default_mcp_assignments_scope_provider_item_id'))
    )
    op.create_table('assistant_default_vault_bindings',
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('vault_id', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['scope'], ['assistant_defaults.scope'], name=op.f('fk_assistant_default_vault_bindings_scope_assistant_defaults'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vault_id'], ['vaults.vault_id'], name=op.f('fk_assistant_default_vault_bindings_vault_id_vaults')),
    sa.PrimaryKeyConstraint('scope', 'position', name=op.f('pk_assistant_default_vault_bindings')),
    sa.UniqueConstraint('scope', 'vault_id', name=op.f('uq_assistant_default_vault_bindings_scope_vault_id'))
    )
    op.create_index(op.f('ix_assistant_default_vault_bindings_vault_id'), 'assistant_default_vault_bindings', ['vault_id'], unique=False)
    op.create_table('assistant_mcp_assignments',
    sa.Column('assistant_id', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('item_id', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['assistant_id'], ['assistants.assistant_id'], name=op.f('fk_assistant_mcp_assignments_assistant_id_assistants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('assistant_id', 'position', name=op.f('pk_assistant_mcp_assignments')),
    sa.UniqueConstraint('assistant_id', 'provider', 'item_id', name=op.f('uq_assistant_mcp_assignments_assistant_id_provider_item_id'))
    )
    op.create_table('assistant_vault_bindings',
    sa.Column('assistant_id', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('vault_id', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['assistant_id'], ['assistants.assistant_id'], name=op.f('fk_assistant_vault_bindings_assistant_id_assistants'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vault_id'], ['vaults.vault_id'], name=op.f('fk_assistant_vault_bindings_vault_id_vaults')),
    sa.PrimaryKeyConstraint('assistant_id', 'position', name=op.f('pk_assistant_vault_bindings')),
    sa.UniqueConstraint('assistant_id', 'vault_id', name=op.f('uq_assistant_vault_bindings_assistant_id_vault_id'))
    )
    op.create_index(op.f('ix_assistant_vault_bindings_vault_id'), 'assistant_vault_bindings', ['vault_id'], unique=False)
    op.create_table('assistant_workspaces',
    sa.Column('assistant_id', sa.Text(), nullable=False),
    sa.Column('created_by_user_id', sa.Text(), nullable=False),
    sa.Column('engine_kind', sa.Text(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('current_sandbox_id', sa.Text(), nullable=True),
    sa.Column('current_sandbox_expires_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('runtime_identity', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('provisioning_session_id', sa.Text(), nullable=True),
    sa.Column('provisioning_sandbox_generation', sa.Text(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('hibernated_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('post_commit_cleanup_pending', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('profile_refresh_revision', sa.Text(), nullable=True),
    sa.Column('profile_refresh_session_id', sa.Text(), nullable=True),
    sa.Column('profile_refresh_started_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('prepared_operation_provider', sa.Text(), nullable=True),
    sa.Column('prepared_operation_id', sa.Text(), nullable=True),
    sa.Column('prepared_operation_deadline', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("state IN ('MATERIALIZING', 'READY', 'HIBERNATING', 'RECOVERY_REQUIRED')", name=op.f('ck_assistant_workspaces_state')),
    sa.ForeignKeyConstraint(['assistant_id'], ['assistants.assistant_id'], name=op.f('fk_assistant_workspaces_assistant_id_assistants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('assistant_id', name=op.f('pk_assistant_workspaces'))
    )
    op.create_index('ix_assistant_workspaces_bound', 'assistant_workspaces', ['assistant_id'], unique=False, postgresql_where=sa.text('current_sandbox_id IS NOT NULL'))
    op.create_index(op.f('ix_assistant_workspaces_current_sandbox_id'), 'assistant_workspaces', ['current_sandbox_id'], unique=False)
    op.create_table('deployments',
    sa.Column('deployment_id', sa.Text(), nullable=False),
    sa.Column('agent_id', sa.Text(), nullable=True),
    sa.Column('assistant_id', sa.Text(), nullable=True),
    sa.Column('created_by', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('scene', sa.Text(), nullable=False),
    sa.Column('prompt_prefix', sa.Text(), server_default='', nullable=False),
    sa.Column('scope', sa.Text(), nullable=True),
    sa.Column('owner_user_id', sa.Text(), nullable=True),
    sa.Column('owner_org_id', sa.Text(), nullable=True),
    sa.Column('source_assistant_id', sa.Text(), nullable=True),
    sa.Column('name', sa.Text(), nullable=True),
    sa.Column('schedule_cron', sa.Text(), nullable=True),
    sa.Column('schedule_timezone', sa.Text(), nullable=True),
    sa.Column('secret', sa.Text(), nullable=True),
    sa.Column('attention_policy', sa.Text(), nullable=True),
    sa.Column('response_instruction', sa.Text(), nullable=True),
    sa.Column('execution_mode', sa.Text(), nullable=True),
    sa.Column('execution_user_id', sa.Text(), nullable=True),
    sa.Column('allowed_participants', postgresql.ARRAY(sa.Text()), nullable=True),
    sa.Column('channel_config', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('credentials_configured', sa.Boolean(), nullable=True),
    sa.Column('callback_base_url', sa.Text(), nullable=True),
    sa.Column('source_cursor', sa.BigInteger(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("scope IS NULL OR (scope = 'personal' AND agent_id IS NOT NULL AND owner_user_id IS NOT NULL AND owner_org_id IS NOT NULL AND source_assistant_id IS NOT NULL)", name=op.f('ck_deployments_personal_owner')),
    sa.CheckConstraint('(agent_id IS NULL) <> (assistant_id IS NULL)', name=op.f('ck_deployments_one_target')),
    sa.CheckConstraint('(schedule_cron IS NULL) = (schedule_timezone IS NULL)', name=op.f('ck_deployments_schedule_pair')),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], name=op.f('fk_deployments_agent_id_agents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['assistant_id'], ['assistants.assistant_id'], name=op.f('fk_deployments_assistant_id_assistants'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_assistant_id'], ['assistants.assistant_id'], name=op.f('fk_deployments_source_assistant_id_assistants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('deployment_id', name=op.f('pk_deployments'))
    )
    op.create_index(op.f('ix_deployments_agent_id_created_at'), 'deployments', ['agent_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_deployments_assistant_id_created_at'), 'deployments', ['assistant_id', 'created_at'], unique=False)
    op.create_index('ix_deployments_channel_bindings', 'deployments', ['deployment_id'], unique=False, postgresql_where=sa.text("enabled AND scene LIKE 'channel:%'"))
    op.create_index('ix_deployments_personal_owner', 'deployments', ['owner_user_id', 'owner_org_id', 'created_at'], unique=False, postgresql_where=sa.text("scope = 'personal'"))
    op.create_index('ix_deployments_schedules', 'deployments', ['deployment_id'], unique=False, postgresql_where=sa.text("scene = 'schedule'"))
    op.create_index(op.f('ix_deployments_source_assistant_id'), 'deployments', ['source_assistant_id'], unique=False, postgresql_where=sa.text('source_assistant_id IS NOT NULL'))
    op.create_table('sessions',
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('org_id', sa.Text(), nullable=True),
    sa.Column('session_kind', sa.Text(), nullable=False),
    sa.Column('engine_kind', sa.Text(), nullable=True),
    sa.Column('workspace_kind', sa.Text(), nullable=False),
    sa.Column('agent_id', sa.Text(), nullable=True),
    sa.Column('assistant_id', sa.Text(), nullable=True),
    sa.Column('owner_type', sa.Text(), nullable=True),
    sa.Column('owner_id', sa.Text(), nullable=True),
    sa.Column('source_type', sa.Text(), nullable=True),
    sa.Column('deployment_name', sa.Text(), nullable=True),
    sa.Column('hidden', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('deleted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('deleted_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('deleted_by', sa.Text(), nullable=True),
    sa.Column('archived_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('archived_by', sa.Text(), nullable=True),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('title_source', sa.Text(), nullable=True),
    sa.Column('title_response_id', sa.Text(), nullable=True),
    sa.Column('title_user_message_id', sa.Text(), nullable=True),
    sa.Column('title_assistant_message_id', sa.Text(), nullable=True),
    sa.Column('title_generated_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('title_model_name', sa.Text(), nullable=True),
    sa.Column('title_generation_status', sa.Text(), nullable=True),
    sa.Column('title_generation_response_id', sa.Text(), nullable=True),
    sa.Column('title_generation_response_index', sa.Integer(), nullable=True),
    sa.Column('title_generation_started_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('title_generation_completed_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('title_generation_failed_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('title_generation_skipped_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('title_generation_error', sa.Text(), nullable=True),
    sa.Column('title_generation_skip_reason', sa.Text(), nullable=True),
    sa.Column('permission_mode', sa.Text(), nullable=True),
    sa.Column('model_name', sa.Text(), nullable=True),
    sa.Column('slash_commands', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('slash_command_details', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('engine_capabilities', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('read_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('last_output_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('runtime_status', sa.Text(), nullable=True),
    sa.Column('runtime_status_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('sandbox_backend', sa.Text(), nullable=True),
    sa.Column('sandbox_id', sa.Text(), nullable=True),
    sa.Column('sandbox_generation', sa.Text(), nullable=True),
    sa.Column('sandbox_callback_token', sa.Text(), nullable=True),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('engine_session_key', sa.Text(), nullable=True),
    sa.Column('runtime_identity', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('runtime_binding', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('terminal_cwd', sa.Text(), nullable=True),
    sa.Column('workspace_id', sa.Text(), nullable=True),
    sa.Column('sandbox_parked_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('runtime_start_failed_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('sandbox_resumed_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('sandbox_liveness_suspect_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('undestroyed_sandbox_ids', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('startup_allocation', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('retained_startup_allocations', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('runtime_unavailable', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('recovery_reason', sa.Text(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('startup_progress', sa.Text(), nullable=True),
    sa.Column('startup_user_action', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('runtime_recovery_owner', sa.Text(), nullable=True),
    sa.Column('startup_provider_assignment', sa.Text(), nullable=True),
    sa.Column('reader_cursor', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('artifact_grant', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('artifact_run', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], name=op.f('fk_sessions_agent_id_agents')),
    sa.ForeignKeyConstraint(['assistant_id'], ['assistants.assistant_id'], name=op.f('fk_sessions_assistant_id_assistants')),
    sa.PrimaryKeyConstraint('session_id', name=op.f('pk_sessions'))
    )
    op.create_index('ix_sessions_agent_list', 'sessions', ['agent_id', 'session_kind', 'deleted', sa.literal_column('updated_at DESC')], unique=False)
    op.create_index(op.f('ix_sessions_assistant_id_user_id'), 'sessions', ['assistant_id', 'user_id'], unique=False)
    op.create_index(op.f('ix_sessions_owner_type_owner_id'), 'sessions', ['owner_type', 'owner_id'], unique=False)
    op.create_index(op.f('ix_sessions_sandbox_id'), 'sessions', ['sandbox_id'], unique=False)
    op.create_index(op.f('ix_sessions_state_updated_at'), 'sessions', ['state', 'updated_at'], unique=False)
    op.create_index('ix_sessions_user_list', 'sessions', ['user_id', 'hidden', 'deleted', sa.literal_column('updated_at DESC'), sa.literal_column('session_id DESC')], unique=False)
    op.create_table('agent_task_references',
    sa.Column('reference_id', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('agent_id', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('org_id', sa.Text(), nullable=False),
    sa.Column('context_id', sa.Text(), nullable=False),
    sa.Column('message_id', sa.Text(), nullable=True),
    sa.Column('task_id', sa.Text(), nullable=True),
    sa.Column('fingerprint', sa.Text(), nullable=True),
    sa.Column('session_id', sa.Text(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('task', 'answer', 'context')", name=op.f('ck_agent_task_references_kind')),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], name=op.f('fk_agent_task_references_agent_id_agents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_agent_task_references_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('reference_id', name=op.f('pk_agent_task_references'))
    )
    op.create_index(op.f('ix_agent_task_references_agent_id_user_id_org_id_created_at_reference_id'), 'agent_task_references', ['agent_id', 'user_id', 'org_id', 'created_at', 'reference_id'], unique=False)
    op.create_index(op.f('ix_agent_task_references_session_id'), 'agent_task_references', ['session_id'], unique=False)
    op.create_table('assistant_profiles',
    sa.Column('assistant_id', sa.Text(), nullable=False),
    sa.Column('marker_key', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('sandbox_id', sa.Text(), nullable=False),
    sa.Column('configuration_revision', sa.Text(), nullable=False),
    sa.Column('ready_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['assistant_id'], ['assistant_workspaces.assistant_id'], name=op.f('fk_assistant_profiles_assistant_id_assistant_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('assistant_id', 'marker_key', name=op.f('pk_assistant_profiles'))
    )
    op.create_table('channel_account_links',
    sa.Column('deployment_id', sa.Text(), nullable=False),
    sa.Column('participant', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('linked_by', sa.Text(), nullable=False),
    sa.Column('linked_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.deployment_id'], name=op.f('fk_channel_account_links_deployment_id_deployments'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('deployment_id', 'participant', name=op.f('pk_channel_account_links'))
    )
    op.create_table('channel_aliases',
    sa.Column('deployment_id', sa.Text(), nullable=False),
    sa.Column('alias', sa.Text(), nullable=False),
    sa.Column('conversation_key', sa.Text(), nullable=True),
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('interaction_ref', sa.Text(), nullable=True),
    sa.Column('question_index', sa.Integer(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.deployment_id'], name=op.f('fk_channel_aliases_deployment_id_deployments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_channel_aliases_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('deployment_id', 'alias', name=op.f('pk_channel_aliases'))
    )
    op.create_index(op.f('ix_channel_aliases_session_id'), 'channel_aliases', ['session_id'], unique=False)
    op.create_table('channel_conversation_locks',
    sa.Column('deployment_id', sa.Text(), nullable=False),
    sa.Column('execution_user_id', sa.Text(), nullable=False),
    sa.Column('conversation_key', sa.Text(), nullable=False),
    sa.Column('owner', sa.Text(), nullable=True),
    sa.Column('generation', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.deployment_id'], name=op.f('fk_channel_conversation_locks_deployment_id_deployments'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('deployment_id', 'execution_user_id', 'conversation_key', name=op.f('pk_channel_conversation_locks'))
    )
    op.create_table('channel_conversations',
    sa.Column('deployment_id', sa.Text(), nullable=False),
    sa.Column('execution_user_id', sa.Text(), nullable=False),
    sa.Column('conversation_key', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.deployment_id'], name=op.f('fk_channel_conversations_deployment_id_deployments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_channel_conversations_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('deployment_id', 'execution_user_id', 'conversation_key', name=op.f('pk_channel_conversations'))
    )
    op.create_index(op.f('ix_channel_conversations_session_id'), 'channel_conversations', ['session_id'], unique=False)
    op.create_table('channel_inbound',
    sa.Column('item_id', sa.Text(), nullable=False),
    sa.Column('deployment_id', sa.Text(), nullable=False),
    sa.Column('dedup_key', sa.Text(), nullable=True),
    sa.Column('channel_name', sa.Text(), nullable=False),
    sa.Column('agent_id', sa.Text(), nullable=True),
    sa.Column('assistant_id', sa.Text(), nullable=True),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('owner_token', sa.Text(), nullable=True),
    sa.Column('generation', sa.BigInteger(), server_default=sa.text('1'), nullable=False),
    sa.Column('lease_expires_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('authorization_expires_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('ignored_reason', sa.Text(), nullable=True),
    sa.Column('session_id', sa.Text(), nullable=True),
    sa.Column('resolved_conversation_key', sa.Text(), nullable=True),
    sa.Column('content', sa.Text(), server_default='', nullable=False),
    sa.Column('reply_text', sa.Text(), nullable=True),
    sa.Column('action', sa.Text(), nullable=True),
    sa.Column('conversation_key', sa.Text(), nullable=True),
    sa.Column('conversation', sa.Text(), nullable=True),
    sa.Column('reply_context', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('reference', sa.Text(), nullable=True),
    sa.Column('participant', sa.Text(), nullable=True),
    sa.Column('provider_ignore_reason', sa.Text(), nullable=True),
    sa.Column('creator_user_id', sa.Text(), server_default='', nullable=False),
    sa.Column('execution_user_id', sa.Text(), server_default='', nullable=False),
    sa.Column('execution_mode', sa.Text(), nullable=True),
    sa.Column('retain_context', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('message_timestamp', sa.Text(), nullable=True),
    sa.Column('binding_revision', sa.Text(), nullable=True),
    sa.Column('prompt_prefix', sa.Text(), server_default='', nullable=False),
    sa.Column('selection', sa.Text(), nullable=True),
    sa.Column('selection_reason', sa.Text(), nullable=True),
    sa.Column('participated', sa.Boolean(), nullable=True),
    sa.Column('response_policy_mode', sa.Text(), nullable=True),
    sa.Column('response_instruction', sa.Text(), nullable=True),
    sa.Column('judgment_respond', sa.Boolean(), nullable=True),
    sa.Column('judgment_reason', sa.Text(), nullable=True),
    sa.Column('judgment_judge', sa.Text(), nullable=True),
    sa.Column('judged_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('explicit_attention', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('answer_ref', sa.Text(), nullable=True),
    sa.Column('answer_question_index', sa.Integer(), nullable=True),
    sa.Column('recall_event_id', sa.Text(), nullable=True),
    sa.Column('recall_timestamp', sa.Text(), nullable=True),
    sa.Column('frozen_content', sa.Text(), nullable=True),
    sa.Column('frozen_context_ids', postgresql.ARRAY(sa.Text()), nullable=True),
    sa.Column('context_excluded', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('context_submitted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("state IN ('RECEIVED', 'DISPATCHING', 'SETTLED', 'IGNORED', 'DEAD', 'OBSERVED', 'AUTHORIZED')", name=op.f('ck_channel_inbound_state')),
    sa.CheckConstraint('(agent_id IS NULL) <> (assistant_id IS NULL)', name=op.f('ck_channel_inbound_one_target')),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.deployment_id'], name=op.f('fk_channel_inbound_deployment_id_deployments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_channel_inbound_session_id_sessions'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('item_id', name=op.f('pk_channel_inbound')),
    sa.UniqueConstraint('deployment_id', 'dedup_key', name=op.f('uq_channel_inbound_deployment_id_dedup_key'))
    )
    op.create_index('ix_channel_inbound_authorizations', 'channel_inbound', ['authorization_expires_at'], unique=False, postgresql_where=sa.text("state = 'AUTHORIZED'"))
    op.create_index('ix_channel_inbound_context', 'channel_inbound', ['deployment_id', 'conversation', 'binding_revision', 'message_timestamp', 'dedup_key'], unique=False, postgresql_where=sa.text('retain_context AND NOT context_submitted AND NOT context_excluded AND provider_ignore_reason IS NULL'))
    op.create_index('ix_channel_inbound_participation', 'channel_inbound', ['deployment_id', 'conversation', 'binding_revision'], unique=False, postgresql_where=sa.text("state = 'SETTLED'"))
    op.create_index('ix_channel_inbound_recoverable', 'channel_inbound', ['lease_expires_at'], unique=False, postgresql_where=sa.text("state IN ('RECEIVED', 'DISPATCHING')"))
    op.create_index(op.f('ix_channel_inbound_session_id'), 'channel_inbound', ['session_id'], unique=False, postgresql_where=sa.text('session_id IS NOT NULL'))
    op.create_table('channel_interactions',
    sa.Column('deployment_id', sa.Text(), nullable=False),
    sa.Column('ref', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('interaction_id', sa.Text(), nullable=False),
    sa.Column('conversation_key', sa.Text(), nullable=True),
    sa.Column('channel_name', sa.Text(), nullable=False),
    sa.Column('reply_context', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('view', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('state', sa.Text(), server_default='OPEN', nullable=False),
    sa.Column('question_index', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('answering_item', sa.Text(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("state IN ('OPEN', 'ANSWERED', 'CLOSED')", name=op.f('ck_channel_interactions_state')),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.deployment_id'], name=op.f('fk_channel_interactions_deployment_id_deployments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_channel_interactions_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('deployment_id', 'ref', name=op.f('pk_channel_interactions'))
    )
    op.create_index(op.f('ix_channel_interactions_session_id'), 'channel_interactions', ['session_id'], unique=False)
    op.create_table('channel_outbox',
    sa.Column('outbox_id', sa.Text(), nullable=False),
    sa.Column('deployment_id', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('response_id', sa.Text(), nullable=False),
    sa.Column('response_seq', sa.BigInteger(), nullable=False),
    sa.Column('channel_name', sa.Text(), nullable=False),
    sa.Column('conversation_key', sa.Text(), nullable=True),
    sa.Column('reply_context', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('streaming', sa.Boolean(), nullable=False),
    sa.Column('stream_started', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('state', sa.Text(), server_default='PENDING', nullable=False),
    sa.Column('lease_owner', sa.Text(), nullable=True),
    sa.Column('lease_generation', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('lease_expires_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('next_attempt_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('delivery_aliases', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("state IN ('PENDING', 'SENDING', 'DELIVERED', 'DEAD')", name=op.f('ck_channel_outbox_state')),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.deployment_id'], name=op.f('fk_channel_outbox_deployment_id_deployments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_channel_outbox_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('outbox_id', name=op.f('pk_channel_outbox')),
    sa.UniqueConstraint('deployment_id', 'session_id', 'response_id', name=op.f('uq_channel_outbox_deployment_id_session_id_response_id'))
    )
    op.create_index(op.f('ix_channel_outbox_session_id'), 'channel_outbox', ['session_id'], unique=False)
    op.create_index('ix_channel_outbox_undelivered', 'channel_outbox', ['deployment_id', 'session_id', 'response_seq'], unique=False, postgresql_where=sa.text("state IN ('PENDING', 'SENDING')"))
    op.create_table('channel_output_subscriptions',
    sa.Column('deployment_id', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('channel_name', sa.Text(), nullable=False),
    sa.Column('reply_context', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('conversation_key', sa.Text(), nullable=True),
    sa.Column('streaming', sa.Boolean(), nullable=False),
    sa.Column('after_seq', sa.BigInteger(), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.deployment_id'], name=op.f('fk_channel_output_subscriptions_deployment_id_deployments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_channel_output_subscriptions_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('deployment_id', 'session_id', name=op.f('pk_channel_output_subscriptions'))
    )
    op.create_index(op.f('ix_channel_output_subscriptions_session_id'), 'channel_output_subscriptions', ['session_id'], unique=False)
    op.create_table('hermes_cron_jobs',
    sa.Column('profile_key', sa.Text(), nullable=False),
    sa.Column('job_id', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('delivered_run_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_hermes_cron_jobs_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('profile_key', 'job_id', name=op.f('pk_hermes_cron_jobs'))
    )
    op.create_index(op.f('ix_hermes_cron_jobs_session_id_profile_key'), 'hermes_cron_jobs', ['session_id', 'profile_key'], unique=False)
    op.create_table('platform_mcp_bindings',
    sa.Column('deployment_id', sa.Text(), nullable=False),
    sa.Column('scope_kind', sa.Text(), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('template_name', sa.Text(), nullable=True),
    sa.Column('user_id', sa.Text(), nullable=True),
    sa.Column('conversation_user_id', sa.Text(), nullable=True),
    sa.Column('assistant_id', sa.Text(), nullable=True),
    sa.Column('session_id', sa.Text(), nullable=True),
    sa.Column('sandbox_id', sa.Text(), nullable=True),
    sa.Column('sandbox_backend', sa.Text(), nullable=True),
    sa.Column('root_path', sa.Text(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(scope_kind = 'assistant_workspace' AND source = 'platform_runtime' AND assistant_id IS NOT NULL AND user_id IS NOT NULL AND session_id IS NULL) OR (scope_kind = 'session' AND source = 'prepared_slot' AND assistant_id IS NULL)", name=op.f('ck_platform_mcp_bindings_kind')),
    sa.ForeignKeyConstraint(['assistant_id'], ['assistant_workspaces.assistant_id'], name=op.f('fk_platform_mcp_bindings_assistant_id_assistant_workspaces'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_platform_mcp_bindings_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('deployment_id', name=op.f('pk_platform_mcp_bindings'))
    )
    op.create_index(op.f('ix_platform_mcp_bindings_assistant_id'), 'platform_mcp_bindings', ['assistant_id'], unique=False)
    op.create_index(op.f('ix_platform_mcp_bindings_session_id'), 'platform_mcp_bindings', ['session_id'], unique=False)
    op.create_index('ix_platform_mcp_bindings_unclaimed_slots', 'platform_mcp_bindings', ['created_at'], unique=False, postgresql_where=sa.text("source = 'prepared_slot' AND session_id IS NULL"))
    op.create_table('process_summaries',
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('response_id', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('claim_token', sa.Text(), nullable=True),
    sa.Column('through_seq', sa.BigInteger(), nullable=True),
    sa.Column('response_completed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_process_summaries_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('session_id', 'response_id', name=op.f('pk_process_summaries'))
    )
    op.create_index('ix_process_summaries_generating', 'process_summaries', ['expires_at'], unique=False, postgresql_where=sa.text("status = 'generating'"))
    op.create_table('session_controls',
    sa.Column('control_id', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('target', sa.Text(), nullable=True),
    sa.Column('reader_generation', sa.BigInteger(), nullable=False),
    sa.Column('requested_by', sa.Text(), nullable=True),
    sa.Column('state', sa.Text(), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('requested_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('applied_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_session_controls_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('control_id', name=op.f('pk_session_controls'))
    )
    op.create_index('ix_session_controls_pending', 'session_controls', ['session_id', 'requested_at'], unique=False, postgresql_where=sa.text("state = 'pending'"))
    op.create_table('session_inputs',
    sa.Column('input_id', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('seq', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('client_message_id', sa.Text(), nullable=False),
    sa.Column('author_user_id', sa.Text(), nullable=True),
    sa.Column('source', sa.Text(), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_blocks', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('state', sa.Text(), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('response_id', sa.Text(), nullable=True),
    sa.Column('failure', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('import_record', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('delivered_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('admitted_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_session_inputs_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('input_id', name=op.f('pk_session_inputs')),
    sa.UniqueConstraint('session_id', 'client_message_id', name=op.f('uq_session_inputs_session_id_client_message_id'))
    )
    op.create_index('ix_session_inputs_open', 'session_inputs', ['session_id', 'seq'], unique=False, postgresql_where=sa.text("state IN ('pending', 'delivered')"))
    op.create_index(op.f('ix_session_inputs_session_id_created_at_seq'), 'session_inputs', ['session_id', 'created_at', 'seq'], unique=False)
    op.create_table('session_interactions',
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('interaction_id', sa.Text(), nullable=False),
    sa.Column('response_id', sa.Text(), nullable=False),
    sa.Column('contract', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('answer', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('answered_by', sa.Text(), nullable=True),
    sa.Column('answered_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('opened_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('closed_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_session_interactions_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('session_id', 'interaction_id', name=op.f('pk_session_interactions'))
    )
    op.create_index('ix_session_interactions_open', 'session_interactions', ['session_id'], unique=False, postgresql_where=sa.text("status = 'open'"))
    op.create_table('session_output',
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('seq', sa.BigInteger(), nullable=False),
    sa.Column('response_id', sa.Text(), nullable=True),
    sa.Column('part', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_session_output_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('session_id', 'seq', name=op.f('pk_session_output'))
    )
    op.create_index('ix_session_output_child_runs', 'session_output', ['session_id', 'seq'], unique=False, postgresql_where=sa.text("part->>'type' = 'data-subagent'"))
    op.create_index(op.f('ix_session_output_session_id_response_id_seq'), 'session_output', ['session_id', 'response_id', 'seq'], unique=False)
    op.create_table('session_readers',
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('owner', sa.Text(), nullable=False),
    sa.Column('generation', sa.BigInteger(), nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_session_readers_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('session_id', name=op.f('pk_session_readers'))
    )
    op.create_table('session_responses',
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('response_id', sa.Text(), nullable=False),
    sa.Column('seq', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('origin', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('outcome', sa.Text(), nullable=True),
    sa.Column('native_reason', sa.Text(), nullable=True),
    sa.Column('usage', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('import_record', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('started_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('ended_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_session_responses_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('session_id', 'response_id', name=op.f('pk_session_responses'))
    )
    op.create_index('ix_session_responses_open', 'session_responses', ['session_id'], unique=False, postgresql_where=sa.text("status <> 'ended'"))
    op.create_index(op.f('ix_session_responses_session_id_started_at_seq'), 'session_responses', ['session_id', 'started_at', 'seq'], unique=False)
    op.create_table('session_shares',
    sa.Column('token', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('allow_download', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('created_by', sa.Text(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_session_shares_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('token', name=op.f('pk_session_shares')),
    sa.UniqueConstraint('session_id', name=op.f('uq_session_shares_session_id'))
    )
    op.create_table('session_vault_bindings',
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('vault_id', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_session_vault_bindings_session_id_sessions'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vault_id'], ['vaults.vault_id'], name=op.f('fk_session_vault_bindings_vault_id_vaults'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('session_id', 'position', name=op.f('pk_session_vault_bindings'))
    )
    op.create_table('terminal_commands',
    sa.Column('command_id', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('command', sa.Text(), nullable=False),
    sa.Column('cwd', sa.Text(), nullable=True),
    sa.Column('next_cwd', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('execution_id', sa.Text(), nullable=True),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('interrupt_requested_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('started_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('ended_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_terminal_commands_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('command_id', name=op.f('pk_terminal_commands'))
    )
    op.create_index('ix_terminal_commands_running', 'terminal_commands', ['session_id'], unique=True, postgresql_where=sa.text("status = 'running'"))
    op.create_index(op.f('ix_terminal_commands_session_id_started_at'), 'terminal_commands', ['session_id', 'started_at'], unique=False)
    op.create_table('tool_cards',
    sa.Column('card_id', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=True),
    sa.Column('assistant_id', sa.Text(), nullable=True),
    sa.Column('server', sa.Text(), nullable=False),
    sa.Column('tool', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('num_nonnulls(session_id, assistant_id) = 1', name=op.f('ck_tool_cards_one_owner')),
    sa.ForeignKeyConstraint(['assistant_id'], ['assistants.assistant_id'], name=op.f('fk_tool_cards_assistant_id_assistants'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.session_id'], name=op.f('fk_tool_cards_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('card_id', name=op.f('pk_tool_cards'))
    )
    op.create_table('transcript_scopes',
    sa.Column('scope_id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('platform_session_id', sa.Text(), nullable=False),
    sa.Column('project_key', sa.Text(), nullable=False),
    sa.Column('native_session_id', sa.Text(), nullable=False),
    sa.Column('subpath', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('last_sequence', sa.BigInteger(), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['platform_session_id'], ['sessions.session_id'], name=op.f('fk_transcript_scopes_platform_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('scope_id', name=op.f('pk_transcript_scopes')),
    sa.UniqueConstraint('platform_session_id', 'project_key', 'native_session_id', 'subpath', name=op.f('uq_transcript_scopes_platform_session_id_project_key_native_session_id_subpath'))
    )
    op.create_table('channel_interaction_answers',
    sa.Column('deployment_id', sa.Text(), nullable=False),
    sa.Column('ref', sa.Text(), nullable=False),
    sa.Column('question_index', sa.Integer(), nullable=False),
    sa.Column('answer_row', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('choice', sa.Text(), nullable=True),
    sa.Column('participant', sa.Text(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['deployment_id', 'ref'], ['channel_interactions.deployment_id', 'channel_interactions.ref'], name=op.f('fk_channel_interaction_answers_deployment_id_ref_channel_interactions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('deployment_id', 'ref', 'question_index', name=op.f('pk_channel_interaction_answers'))
    )
    op.create_table('session_response_inputs',
    sa.Column('session_id', sa.Text(), nullable=False),
    sa.Column('response_id', sa.Text(), nullable=False),
    sa.Column('input_id', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['input_id'], ['session_inputs.input_id'], name=op.f('fk_session_response_inputs_input_id_session_inputs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id', 'response_id'], ['session_responses.session_id', 'session_responses.response_id'], name=op.f('fk_session_response_inputs_session_id_response_id_session_responses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('session_id', 'response_id', 'input_id', name=op.f('pk_session_response_inputs'))
    )
    op.create_index(op.f('ix_session_response_inputs_input_id'), 'session_response_inputs', ['input_id'], unique=False)
    op.create_table('terminal_output',
    sa.Column('seq', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('command_id', sa.Text(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['command_id'], ['terminal_commands.command_id'], name=op.f('fk_terminal_output_command_id_terminal_commands'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('seq', name=op.f('pk_terminal_output'))
    )
    op.create_index(op.f('ix_terminal_output_command_id_seq'), 'terminal_output', ['command_id', 'seq'], unique=False)
    op.create_table('transcript_entries',
    sa.Column('scope_id', sa.BigInteger(), nullable=False),
    sa.Column('seq', sa.BigInteger(), nullable=False),
    sa.Column('append_id', sa.Text(), nullable=False),
    sa.Column('batch_index', sa.Integer(), nullable=False),
    sa.Column('entry_json', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['scope_id'], ['transcript_scopes.scope_id'], name=op.f('fk_transcript_entries_scope_id_transcript_scopes'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('scope_id', 'seq', name=op.f('pk_transcript_entries')),
    sa.UniqueConstraint('scope_id', 'append_id', 'batch_index', name=op.f('uq_transcript_entries_scope_id_append_id_batch_index'))
    )
    # Assistants and sessions name each other, so this key is added once both
    # tables exist.
    op.create_foreign_key(
        op.f('fk_assistants_main_session_id_sessions'), 'assistants', 'sessions',
        ['main_session_id'], ['session_id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint(op.f('fk_assistants_main_session_id_sessions'), 'assistants', type_='foreignkey')
    op.drop_table('transcript_entries')
    op.drop_index(op.f('ix_terminal_output_command_id_seq'), table_name='terminal_output')
    op.drop_table('terminal_output')
    op.drop_index(op.f('ix_session_response_inputs_input_id'), table_name='session_response_inputs')
    op.drop_table('session_response_inputs')
    op.drop_table('channel_interaction_answers')
    op.drop_table('transcript_scopes')
    op.drop_table('tool_cards')
    op.drop_index(op.f('ix_terminal_commands_session_id_started_at'), table_name='terminal_commands')
    op.drop_index('ix_terminal_commands_running', table_name='terminal_commands', postgresql_where=sa.text("status = 'running'"))
    op.drop_table('terminal_commands')
    op.drop_table('session_vault_bindings')
    op.drop_table('session_shares')
    op.drop_index(op.f('ix_session_responses_session_id_started_at_seq'), table_name='session_responses')
    op.drop_index('ix_session_responses_open', table_name='session_responses', postgresql_where=sa.text("status <> 'ended'"))
    op.drop_table('session_responses')
    op.drop_table('session_readers')
    op.drop_index(op.f('ix_session_output_session_id_response_id_seq'), table_name='session_output')
    op.drop_index('ix_session_output_child_runs', table_name='session_output', postgresql_where=sa.text("part->>'type' = 'data-subagent'"))
    op.drop_table('session_output')
    op.drop_index('ix_session_interactions_open', table_name='session_interactions', postgresql_where=sa.text("status = 'open'"))
    op.drop_table('session_interactions')
    op.drop_index(op.f('ix_session_inputs_session_id_created_at_seq'), table_name='session_inputs')
    op.drop_index('ix_session_inputs_open', table_name='session_inputs', postgresql_where=sa.text("state IN ('pending', 'delivered')"))
    op.drop_table('session_inputs')
    op.drop_index('ix_session_controls_pending', table_name='session_controls', postgresql_where=sa.text("state = 'pending'"))
    op.drop_table('session_controls')
    op.drop_index('ix_process_summaries_generating', table_name='process_summaries', postgresql_where=sa.text("status = 'generating'"))
    op.drop_table('process_summaries')
    op.drop_index('ix_platform_mcp_bindings_unclaimed_slots', table_name='platform_mcp_bindings', postgresql_where=sa.text("source = 'prepared_slot' AND session_id IS NULL"))
    op.drop_index(op.f('ix_platform_mcp_bindings_session_id'), table_name='platform_mcp_bindings')
    op.drop_index(op.f('ix_platform_mcp_bindings_assistant_id'), table_name='platform_mcp_bindings')
    op.drop_table('platform_mcp_bindings')
    op.drop_index(op.f('ix_hermes_cron_jobs_session_id_profile_key'), table_name='hermes_cron_jobs')
    op.drop_table('hermes_cron_jobs')
    op.drop_index(op.f('ix_channel_output_subscriptions_session_id'), table_name='channel_output_subscriptions')
    op.drop_table('channel_output_subscriptions')
    op.drop_index('ix_channel_outbox_undelivered', table_name='channel_outbox', postgresql_where=sa.text("state IN ('PENDING', 'SENDING')"))
    op.drop_index(op.f('ix_channel_outbox_session_id'), table_name='channel_outbox')
    op.drop_table('channel_outbox')
    op.drop_index(op.f('ix_channel_interactions_session_id'), table_name='channel_interactions')
    op.drop_table('channel_interactions')
    op.drop_index(op.f('ix_channel_inbound_session_id'), table_name='channel_inbound', postgresql_where=sa.text('session_id IS NOT NULL'))
    op.drop_index('ix_channel_inbound_recoverable', table_name='channel_inbound', postgresql_where=sa.text("state IN ('RECEIVED', 'DISPATCHING')"))
    op.drop_index('ix_channel_inbound_participation', table_name='channel_inbound', postgresql_where=sa.text("state = 'SETTLED'"))
    op.drop_index('ix_channel_inbound_context', table_name='channel_inbound', postgresql_where=sa.text('retain_context AND NOT context_submitted AND NOT context_excluded AND provider_ignore_reason IS NULL'))
    op.drop_index('ix_channel_inbound_authorizations', table_name='channel_inbound', postgresql_where=sa.text("state = 'AUTHORIZED'"))
    op.drop_table('channel_inbound')
    op.drop_index(op.f('ix_channel_conversations_session_id'), table_name='channel_conversations')
    op.drop_table('channel_conversations')
    op.drop_table('channel_conversation_locks')
    op.drop_index(op.f('ix_channel_aliases_session_id'), table_name='channel_aliases')
    op.drop_table('channel_aliases')
    op.drop_table('channel_account_links')
    op.drop_table('assistant_profiles')
    op.drop_index(op.f('ix_agent_task_references_session_id'), table_name='agent_task_references')
    op.drop_index(op.f('ix_agent_task_references_agent_id_user_id_org_id_created_at_reference_id'), table_name='agent_task_references')
    op.drop_table('agent_task_references')
    op.drop_index('ix_sessions_user_list', table_name='sessions')
    op.drop_index(op.f('ix_sessions_state_updated_at'), table_name='sessions')
    op.drop_index(op.f('ix_sessions_sandbox_id'), table_name='sessions')
    op.drop_index(op.f('ix_sessions_owner_type_owner_id'), table_name='sessions')
    op.drop_index(op.f('ix_sessions_assistant_id_user_id'), table_name='sessions')
    op.drop_index('ix_sessions_agent_list', table_name='sessions')
    op.drop_table('sessions')
    op.drop_index(op.f('ix_deployments_source_assistant_id'), table_name='deployments', postgresql_where=sa.text('source_assistant_id IS NOT NULL'))
    op.drop_index('ix_deployments_schedules', table_name='deployments', postgresql_where=sa.text("scene = 'schedule'"))
    op.drop_index('ix_deployments_personal_owner', table_name='deployments', postgresql_where=sa.text("scope = 'personal'"))
    op.drop_index('ix_deployments_channel_bindings', table_name='deployments', postgresql_where=sa.text("enabled AND scene LIKE 'channel:%'"))
    op.drop_index(op.f('ix_deployments_assistant_id_created_at'), table_name='deployments')
    op.drop_index(op.f('ix_deployments_agent_id_created_at'), table_name='deployments')
    op.drop_table('deployments')
    op.drop_index(op.f('ix_assistant_workspaces_current_sandbox_id'), table_name='assistant_workspaces')
    op.drop_index('ix_assistant_workspaces_bound', table_name='assistant_workspaces', postgresql_where=sa.text('current_sandbox_id IS NOT NULL'))
    op.drop_table('assistant_workspaces')
    op.drop_index(op.f('ix_assistant_vault_bindings_vault_id'), table_name='assistant_vault_bindings')
    op.drop_table('assistant_vault_bindings')
    op.drop_table('assistant_mcp_assignments')
    op.drop_index(op.f('ix_assistant_default_vault_bindings_vault_id'), table_name='assistant_default_vault_bindings')
    op.drop_table('assistant_default_vault_bindings')
    op.drop_table('assistant_default_mcp_assignments')
    op.drop_table('assistant_connectors')
    op.drop_table('artifact_version_contents')
    op.drop_table('artifact_shares')
    op.drop_index(op.f('ix_agent_vault_bindings_vault_id'), table_name='agent_vault_bindings')
    op.drop_table('agent_vault_bindings')
    op.drop_table('agent_undestroyed_sandboxes')
    op.drop_table('agent_slot_gateway_entries')
    op.drop_index(op.f('ix_agent_runtimes_sandbox_id'), table_name='agent_runtimes', postgresql_where=sa.text('sandbox_id IS NOT NULL'))
    op.drop_index('ix_agent_runtimes_resident_sweep', table_name='agent_runtimes', postgresql_where=sa.text('sandbox_id IS NOT NULL'))
    op.drop_table('agent_runtimes')
    op.drop_table('agent_retiring_client_pools')
    op.drop_index(op.f('ix_agent_prepared_slots_sandbox_id'), table_name='agent_prepared_slots')
    op.drop_index(op.f('ix_agent_prepared_slots_agent_id_prepared_at'), table_name='agent_prepared_slots')
    op.drop_table('agent_prepared_slots')
    op.drop_table('agent_mcp_assignments')
    op.drop_index(op.f('ix_agent_favorites_agent_id'), table_name='agent_favorites')
    op.drop_table('agent_favorites')
    op.drop_table('agent_connector_servers')
    op.drop_index(op.f('ix_agent_category_links_category_id'), table_name='agent_category_links')
    op.drop_table('agent_category_links')
    op.drop_index(op.f('ix_agent_box_admissions_sandbox_id'), table_name='agent_box_admissions')
    op.drop_table('agent_box_admissions')
    op.drop_index(op.f('ix_vault_credentials_vault_id_url'), table_name='vault_credentials', postgresql_where=sa.text('archived_at IS NULL AND url IS NOT NULL'))
    op.drop_index(op.f('ix_vault_credentials_vault_id_secret_name'), table_name='vault_credentials', postgresql_where=sa.text('archived_at IS NULL AND secret_name IS NOT NULL'))
    op.drop_index(op.f('ix_vault_credentials_vault_id_mcp_server_url'), table_name='vault_credentials', postgresql_where=sa.text('archived_at IS NULL AND mcp_server_url IS NOT NULL'))
    op.drop_index(op.f('ix_vault_credentials_vault_id_created_at'), table_name='vault_credentials')
    op.drop_table('vault_credentials')
    op.drop_table('sandbox_reservations')
    op.drop_table('sandbox_inventory')
    op.drop_table('runtime_state_chunks')
    op.drop_index(op.f('ix_assistants_owner_id_updated_at_assistant_id'), table_name='assistants')
    op.drop_index('ix_assistants_live_primary', table_name='assistants', postgresql_where=sa.text('is_primary AND NOT deleted'))
    op.drop_index(op.f('ix_assistants_environment_name'), table_name='assistants')
    op.drop_table('assistants')
    op.drop_table('assistant_defaults')
    op.drop_table('artifact_viewers')
    op.drop_table('artifact_versions')
    op.drop_index(op.f('ix_artifact_sample_reservations_budget_key_created_at'), table_name='artifact_sample_reservations')
    op.drop_table('artifact_sample_reservations')
    op.drop_index(op.f('ix_artifact_run_reservations_budget_key_created_at'), table_name='artifact_run_reservations')
    op.drop_table('artifact_run_reservations')
    op.drop_index(op.f('ix_artifact_events_kind_created_at'), table_name='artifact_events', postgresql_where=sa.text("kind IN ('room', 'presence')"))
    op.drop_index(op.f('ix_artifact_events_artifact_id_seq'), table_name='artifact_events', postgresql_where=sa.text("kind = 'presence'"))
    op.drop_index(op.f('ix_artifact_events_artifact_id_path'), table_name='artifact_events', postgresql_where=sa.text("kind = 'data'"))
    op.drop_index(op.f('ix_artifact_events_artifact_id'), table_name='artifact_events', postgresql_where=sa.text("kind = 'version'"))
    op.drop_table('artifact_events')
    op.drop_index(op.f('ix_artifact_documents_artifact_id_collection_path'), table_name='artifact_documents')
    op.drop_table('artifact_documents')
    op.drop_index(op.f('ix_agents_updated_at_agent_id'), table_name='agents', postgresql_where=sa.text("state <> 'DELETED'"))
    op.drop_index('ix_agents_prepared_capacity_sweep', table_name='agents', postgresql_where=sa.text("prepared_capacity > 0 AND state = 'ACTIVE'"))
    op.drop_index(op.f('ix_agents_name_key_agent_id'), table_name='agents', postgresql_where=sa.text("state <> 'DELETED'"))
    op.drop_index(op.f('ix_agents_environment_name_agent_id'), table_name='agents')
    op.drop_table('agents')
    op.drop_index(op.f('ix_vaults_org_id_created_at'), table_name='vaults')
    op.drop_table('vaults')
    op.drop_table('user_profiles')
    op.drop_table('secrets')
    op.drop_table('sandbox_capacity')
    op.drop_table('runtime_state_heads')
    op.drop_index(op.f('ix_runtime_errors_last_seen'), table_name='runtime_errors')
    op.drop_table('runtime_errors')
    op.drop_index(op.f('ix_mcp_servers_org_id_updated_at'), table_name='mcp_servers')
    op.drop_table('mcp_servers')
    op.drop_index(op.f('ix_mcp_client_tokens_user_id_created_at'), table_name='mcp_client_tokens')
    op.drop_table('mcp_client_tokens')
    op.drop_table('installation')
    op.drop_table('environments')
    op.drop_index(op.f('ix_artifacts_owner_id_updated_at_artifact_id'), table_name='artifacts')
    op.drop_index(op.f('ix_artifacts_owner_id_origin_agent_id_updated_at_artifact_id'), table_name='artifacts', postgresql_where=sa.text('origin_agent_id IS NOT NULL'))
    op.drop_index(op.f('ix_artifacts_origin_session_id_updated_at_artifact_id'), table_name='artifacts', postgresql_where=sa.text('origin_session_id IS NOT NULL'))
    op.drop_index(op.f('ix_artifacts_origin_assistant_id_updated_at_artifact_id'), table_name='artifacts', postgresql_where=sa.text('origin_assistant_id IS NOT NULL'))
    op.drop_table('artifacts')
    op.drop_index(op.f('ix_artifact_budgets_kind_updated_at'), table_name='artifact_budgets')
    op.drop_table('artifact_budgets')
    op.drop_table('agent_categories')
