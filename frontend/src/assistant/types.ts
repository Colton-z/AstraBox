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
  environment_name: string;
  permission_mode_default: string;
  workspace_state?: AssistantWorkspaceState | string;
  current_sandbox_id?: string | null;
  created_at?: string;
  updated_at?: string;
}
