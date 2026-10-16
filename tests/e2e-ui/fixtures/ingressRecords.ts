/**
 * What the platform recorded about work that entered a conversation from
 * outside the console: the channel spine's messages, subscriptions and
 * deliveries (`astrabox/persistence/sql/models/channels.py`), the bindings
 * themselves (`deployments`), the sessions they started (`sessions`) and A2A
 * task references (`agent_task_references`).
 *
 * These are the platform's own rows, read from the deployment database, so a
 * spec can compare them with what a provider callback received and what the
 * session's history shows without trusting either projection. The writers at
 * the end are the faults specs inject into the same rows; each changes only
 * the named row and returns it as written.
 */
import { postgresRows, postgresTextRows, sqlLiteral } from './dbOracle';
import { POSTGRES_CONTAINER_HANDLE, requireServiceContainer } from './serviceContainer';

// ── Channel inbound messages (`channel_inbound`) ───────────────────────────

/**
 * One platform message on one binding, and its unit of work.
 *
 * `state` is `RECEIVED`, `DISPATCHING`, `SETTLED`, `IGNORED`, `DEAD`,
 * `OBSERVED` (retained context only) or `AUTHORIZED` (admitted, waiting for
 * its observation). A message that ran was sent into its Session as the input
 * whose `client_message_id` is `item_id`. `frozen_context` names the retained
 * messages its input quoted, by their `dedup_key`, in the order quoted.
 */
export interface ChannelInboundRow {
  item_id: string;
  deployment_id: string;
  dedup_key: string | null;
  channel_name: string;
  state: string;
  session_id: string | null;
  owner_token: string | null;
  generation: number;
  /** The drive's lease has not lapsed (by the database clock). */
  lease_live: boolean;
  attempts: number;
  last_error: string | null;
  ignored_reason: string | null;
  /** The input as admitted, binding prompt prefix included. */
  content: string;
  /** What the participant typed. */
  reply_text: string | null;
  participant: string | null;
  conversation: string | null;
  retain_context: boolean;
  provider_ignore_reason: string | null;
  selection: string | null;
  selection_reason: string | null;
  participated: boolean | null;
  judgment_respond: boolean | null;
  judgment_reason: string | null;
  judgment_judge: string | null;
  judged_at: string | null;
  explicit_attention: boolean;
  recall_event_id: string | null;
  recall_timestamp: string | null;
  /** The input exactly as sent, frozen before the first send; `null` until then. */
  frozen_content: string | null;
  frozen_context: string[];
  context_excluded: boolean;
  context_submitted: boolean;
  created_at: string;
}

const INBOUND_COLUMNS = `i.item_id, i.deployment_id, i.dedup_key, i.channel_name, i.state, i.session_id, `
  + `i.owner_token, i.generation, COALESCE(i.lease_expires_at > now(), false) AS lease_live, i.attempts, `
  + `i.last_error, i.ignored_reason, i.content, i.reply_text, i.participant, i.conversation, i.retain_context, `
  + `i.provider_ignore_reason, i.selection, i.selection_reason, i.participated, i.judgment_respond, `
  + `i.judgment_reason, i.judgment_judge, i.judged_at, i.explicit_attention, i.recall_event_id, `
  + `i.recall_timestamp, i.frozen_content, `
  + `COALESCE((SELECT array_agg(q.dedup_key ORDER BY f.ord) FROM unnest(i.frozen_context_ids) `
  + `WITH ORDINALITY AS f(item_id, ord) JOIN channel_inbound q ON q.item_id = f.item_id), `
  + `ARRAY[]::text[]) AS frozen_context, i.context_excluded, i.context_submitted, i.created_at`;

/** Every message a binding recorded, in arrival order. */
export function channelInboundRows(deploymentId: string): ChannelInboundRow[] {
  return postgresRows<ChannelInboundRow>(
    `SELECT ${INBOUND_COLUMNS} FROM channel_inbound i `
      + `WHERE i.deployment_id = ${sqlLiteral(deploymentId)} ORDER BY i.created_at, i.item_id`,
  );
}

/** The messages that ran in one Session, in arrival order. */
export function sessionChannelInboundRows(sessionId: string): ChannelInboundRow[] {
  return postgresRows<ChannelInboundRow>(
    `SELECT ${INBOUND_COLUMNS} FROM channel_inbound i `
      + `WHERE i.session_id = ${sqlLiteral(sessionId)} ORDER BY i.created_at, i.item_id`,
  );
}

/** The binding's record of one platform message, or `undefined` before it arrives. */
export function channelInbound(deploymentId: string, dedupKey: string): ChannelInboundRow | undefined {
  return channelInboundRows(deploymentId).find((row) => row.dedup_key === dedupKey);
}

/** An input a channel message became, in whichever Session holds it. */
export interface ChannelMessageInputRow {
  session_id: string;
  input_id: string;
  source: string | null;
  author_user_id: string | null;
  content: string;
  state: string;
  response_id: string | null;
}

/**
 * The inputs any Session accepted for one channel message: its item id is the
 * `client_message_id` of the one input it sends, so a message that ran has
 * exactly one and a message that never ran has none.
 */
export function channelMessageInputs(itemId: string): ChannelMessageInputRow[] {
  return postgresRows<ChannelMessageInputRow>(
    `SELECT session_id, input_id, source, author_user_id, content, state, response_id FROM session_inputs `
      + `WHERE client_message_id = ${sqlLiteral(itemId)} ORDER BY seq`,
  );
}

// ── Output subscriptions and deliveries ────────────────────────────────────

/**
 * A binding's destination for one Session's replies. `after_seq` is the
 * Session output position through which its replies became deliveries.
 */
export interface ChannelOutputSubscriptionRow {
  deployment_id: string;
  session_id: string;
  channel_name: string;
  conversation_key: string | null;
  streaming: boolean;
  after_seq: number;
}

/** The subscriptions that follow one Session's output. */
export function channelOutputSubscriptions(sessionId: string): ChannelOutputSubscriptionRow[] {
  return postgresRows<ChannelOutputSubscriptionRow>(
    `SELECT deployment_id, session_id, channel_name, conversation_key, streaming, after_seq `
      + `FROM channel_output_subscriptions WHERE session_id = ${sqlLiteral(sessionId)} ORDER BY deployment_id`,
  );
}

/**
 * The delivery of one Session reply to one binding. `response_id` is the
 * reply: the response id, or `<response_id>~<interaction_id>` for what the
 * response says after that interaction. `state` is `PENDING`, `SENDING`,
 * `DELIVERED` or `DEAD`.
 */
export interface ChannelOutboxRow {
  outbox_id: string;
  deployment_id: string;
  session_id: string;
  response_id: string;
  response_seq: number;
  channel_name: string;
  streaming: boolean;
  stream_started: boolean;
  state: string;
  lease_owner: string | null;
  lease_generation: number;
  /** The deliverer's lease has not lapsed (by the database clock). */
  lease_live: boolean;
  attempts: number;
  last_error: string | null;
  next_attempt_at: string | null;
  delivery_aliases: string[];
}

const OUTBOX_COLUMNS = `outbox_id, deployment_id, session_id, response_id, response_seq, channel_name, `
  + `streaming, stream_started, state, lease_owner, lease_generation, `
  + `COALESCE(lease_expires_at > now(), false) AS lease_live, attempts, last_error, next_attempt_at, `
  + `delivery_aliases`;

/** One Session's deliveries, in the order its replies started. */
export function channelOutboxRows(sessionId: string): ChannelOutboxRow[] {
  return postgresRows<ChannelOutboxRow>(
    `SELECT ${OUTBOX_COLUMNS} FROM channel_outbox WHERE session_id = ${sqlLiteral(sessionId)} `
      + `ORDER BY response_seq, deployment_id`,
  );
}

/** A platform message the spine delivered and the conversation it continues. */
export interface ChannelAliasRow {
  deployment_id: string;
  alias: string;
  conversation_key: string | null;
  session_id: string;
  interaction_ref: string | null;
  question_index: number | null;
}

export function channelAliasRows(deploymentId: string): ChannelAliasRow[] {
  return postgresRows<ChannelAliasRow>(
    `SELECT deployment_id, alias, conversation_key, session_id, interaction_ref, question_index `
      + `FROM channel_aliases WHERE deployment_id = ${sqlLiteral(deploymentId)} ORDER BY created_at, alias`,
  );
}

/** The Session a binding's conversation continues in, per execution account. */
export interface ChannelConversationRow {
  deployment_id: string;
  execution_user_id: string;
  conversation_key: string;
  session_id: string;
}

export function channelConversationRows(deploymentId: string): ChannelConversationRow[] {
  return postgresRows<ChannelConversationRow>(
    `SELECT deployment_id, execution_user_id, conversation_key, session_id FROM channel_conversations `
      + `WHERE deployment_id = ${sqlLiteral(deploymentId)} ORDER BY created_at, conversation_key`,
  );
}

// ── Bindings, sessions and A2A references ──────────────────────────────────

/** A webhook, schedule or channel binding as stored. */
export interface DeploymentRow {
  deployment_id: string;
  agent_id: string | null;
  assistant_id: string | null;
  created_by: string;
  enabled: boolean;
  scene: string;
  name: string | null;
  prompt_prefix: string;
  scope: string | null;
  owner_user_id: string | null;
  owner_org_id: string | null;
  source_assistant_id: string | null;
  schedule_cron: string | null;
  schedule_timezone: string | null;
  execution_mode: string | null;
  execution_user_id: string | null;
  /** The last source position a channel source acknowledged durably. */
  source_cursor: number | null;
}

export function deploymentRow(deploymentId: string): DeploymentRow | undefined {
  return postgresRows<DeploymentRow>(
    `SELECT deployment_id, agent_id, assistant_id, created_by, enabled, scene, name, prompt_prefix, scope, `
      + `owner_user_id, owner_org_id, source_assistant_id, schedule_cron, schedule_timezone, `
      + `execution_mode, execution_user_id, source_cursor `
      + `FROM deployments WHERE deployment_id = ${sqlLiteral(deploymentId)}`,
  )[0];
}

/** A conversation's identity, ownership and box, as the `sessions` row holds them. */
export interface SessionRow {
  session_id: string;
  user_id: string;
  org_id: string | null;
  agent_id: string | null;
  assistant_id: string | null;
  source_type: string | null;
  /** The platform lifecycle: CREATING, READY, TERMINATED or DELETED. */
  state: string;
  deleted: boolean;
  sandbox_id: string | null;
  runtime_unavailable: boolean;
  created_at: string;
}

const SESSION_COLUMNS = `session_id, user_id, org_id, agent_id, assistant_id, source_type, state, deleted, `
  + `sandbox_id, runtime_unavailable, created_at`;

export function sessionRow(sessionId: string): SessionRow | undefined {
  return postgresRows<SessionRow>(
    `SELECT ${SESSION_COLUMNS} FROM sessions WHERE session_id = ${sqlLiteral(sessionId)}`,
  )[0];
}

/** Every conversation of one Agent that is not deleted, oldest first. */
export function agentSessionRows(agentId: string): SessionRow[] {
  return postgresRows<SessionRow>(
    `SELECT ${SESSION_COLUMNS} FROM sessions WHERE agent_id = ${sqlLiteral(agentId)} AND NOT deleted `
      + `ORDER BY created_at, session_id`,
  );
}

/**
 * An A2A protocol identity: a task (`kind` `task`, its id is `reference_id`),
 * a message answering one (`answer`), or the conversation a context runs in
 * (`context`).
 */
export interface AgentTaskReferenceRow {
  reference_id: string;
  kind: string;
  agent_id: string;
  user_id: string;
  org_id: string;
  context_id: string;
  message_id: string | null;
  task_id: string | null;
  session_id: string | null;
  created_at: string;
}

export function agentTaskReferences(agentId: string): AgentTaskReferenceRow[] {
  return postgresRows<AgentTaskReferenceRow>(
    `SELECT reference_id, kind, agent_id, user_id, org_id, context_id, message_id, task_id, session_id, `
      + `created_at FROM agent_task_references WHERE agent_id = ${sqlLiteral(agentId)} `
      + `ORDER BY created_at, reference_id`,
  );
}

// ── Faults ─────────────────────────────────────────────────────────────────

/** Run one data-modifying statement whose `RETURNING` rows come back as objects. */
function writeRows<T>(statement: string): T[] {
  return postgresTextRows(
    requireServiceContainer(POSTGRES_CONTAINER_HANDLE),
    process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox',
    `WITH written AS (${statement}) SELECT row_to_json(written)::text FROM written`,
  ).map((line) => JSON.parse(line) as T);
}

/**
 * Move a Session's output subscriptions back to `afterSeq`, as if their
 * projector had never got past it. Returns the subscriptions as written.
 */
export function rewindOutputSubscriptions(sessionId: string, afterSeq: number): ChannelOutputSubscriptionRow[] {
  return writeRows<ChannelOutputSubscriptionRow>(
    `UPDATE channel_output_subscriptions SET after_seq = ${Math.trunc(afterSeq)} `
      + `WHERE session_id = ${sqlLiteral(sessionId)} `
      + `RETURNING deployment_id, session_id, channel_name, conversation_key, streaming, after_seq`,
  );
}

/** Remove a Session's output subscriptions, as for a conversation no channel follows yet. */
export function deleteOutputSubscriptions(sessionId: string): ChannelOutputSubscriptionRow[] {
  return writeRows<ChannelOutputSubscriptionRow>(
    `DELETE FROM channel_output_subscriptions WHERE session_id = ${sqlLiteral(sessionId)} `
      + `RETURNING deployment_id, session_id, channel_name, conversation_key, streaming, after_seq`,
  );
}

/**
 * Leave one delivery as a deliverer that stopped mid-send leaves it: `SENDING`
 * under `owner` at the next lease generation, with a lease that has lapsed.
 * Returns the row as written.
 */
export function abandonOutboxDelivery(outboxId: string, owner: string): ChannelOutboxRow[] {
  return writeRows<ChannelOutboxRow>(
    `UPDATE channel_outbox SET state = 'SENDING', lease_owner = ${sqlLiteral(owner)}, `
      + `lease_generation = lease_generation + 1, lease_expires_at = 'epoch', next_attempt_at = NULL `
      + `WHERE outbox_id = ${sqlLiteral(outboxId)} RETURNING ${OUTBOX_COLUMNS}`,
  );
}
