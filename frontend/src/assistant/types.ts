import type { SkillDescriptor } from '@/types';
import type { components } from '../api/schema';

/**
 * The assistant wire shapes, re-exported from the generated client where the
 * console already agrees with the server.
 *
 * See `src/types.ts` for the reasoning: the backend's response models are the
 * author, and `make check-api-client` keeps this file honest about them.
 *
 * `AssistantRecord` is the exception and stays written out. The declared
 * `Assistant` model is the truthful one — `display_name`, `icon` and
 * `description` are nullable there — while five pages read them as plain
 * strings. Adopting the declared model therefore means changing those pages
 * (five files, nineteen sites) as well as this type.
 */
type Schemas = components['schemas'];

export type AssistantConversationResult = Schemas['StartedAssistantConversation'];
export type AssistantWorkspaceResult = Schemas['AssistantWorkspace'];
export type AssistantExtensionCatalog = Schemas['AssistantExtensionCatalog'];
export type AssistantDefaults = Schemas['AssistantDefaults'];
export type AssistantScheduledJobs = Schemas['AssistantScheduledJobs'];
export type AssistantDefaultsFields = Schemas['AssistantDefaultsFields'];
export type AssistantDefaultsExtensionCatalog = Schemas['AssistantDefaultsExtensionCatalog'];

export type AssistantWorkspaceState =
  | 'NOT_MATERIALIZED'
  | 'MATERIALIZING'
  | 'READY'
  | 'HIBERNATING'
  | 'RECOVERY_REQUIRED';

export interface AssistantRecord {
  assistant_id: string;
  owner_id: string;
  display_name: string;
  icon: string | null;
  description: string | null;
  engine_kind: string;
  runtime_profile_name: string;
  permission_mode_default: string;
  system?: string | null;
  timezone?: string | null;
  mcp_servers?: Record<string, unknown> | null;
  skills?: SkillDescriptor[] | null;
  packages?: Record<string, unknown>[] | null;
  engine_options?: Record<string, unknown> | null;
  /** MCP servers selected from extension catalogs, as stored: `{provider, item_id}` rows. */
  mcp_assignments?: Record<string, unknown>[] | null;
  /** Skills selected from the extension catalog, as stored: `skill_ids` and their Git `skills`. */
  extension_catalog?: Record<string, unknown> | null;
  workspace_state?: AssistantWorkspaceState | string;
  current_sandbox_id?: string | null;
  /** The one Assistant the platform keeps for its owner. */
  primary?: boolean | null;
  /** Where opening the Assistant lands. */
  main_session_id?: string | null;
  /** What the deployment's Assistant defaults provide, read-only. */
  inherited?: Schemas['InheritedExtensions'] | null;
  created_at?: string;
  updated_at?: string;
}

/**
 * One page of the caller's Assistants, the most recently edited first. `total`
 * and `ready` count every Assistant the caller owns, whatever `q` and `status`
 * narrowed, and come with the first page only.
 */
export interface AssistantListPage {
  assistants: AssistantRecord[];
  has_more: boolean;
  next_cursor?: string | null;
  total?: number;
  ready?: number;
}
