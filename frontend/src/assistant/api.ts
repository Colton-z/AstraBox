import i18n from '@/i18n';

import type { ListPageQuery } from '@/types';

import type {
  AssistantConversationResult,
  AssistantDefaults,
  AssistantDefaultsExtensionCatalog,
  AssistantDefaultsFields,
  AssistantExtensionCatalog,
  AssistantListPage,
  AssistantRecord,
  AssistantWorkspaceResult,
} from './types';
import { apiClient, listPageQuery, sendApi, startConversationRequest } from '../api';

export async function listAssistants(): Promise<AssistantRecord[]> {
  const data = await sendApi((wire) => apiClient.GET('/api/v1/assistants', wire));
  if (Array.isArray(data)) return data as AssistantRecord[];
  // Same reasoning as api.ts's expectList: failing here reaches the caller's
  // catch, while a null passes through to render and takes the page with it.
  throw new Error(
    i18n.t('misc:api_error.malformed_list', {
      endpoint: '/assistants',
      got: data === null ? 'null' : typeof data,
    }),
  );
}

/** One page of the caller's Assistants, narrowed and counted by the server. */
export async function listAssistantsPage(query: ListPageQuery = {}): Promise<AssistantListPage> {
  const data = await sendApi((wire) => apiClient.GET('/api/v1/assistants', {
    ...wire,
    params: { query: listPageQuery(query) },
  }));
  if (data && typeof data === 'object' && 'assistants' in data) return data as AssistantListPage;
  throw new Error(
    i18n.t('misc:api_error.malformed_record', {
      endpoint: '/assistants',
      got: data === null ? 'null' : typeof data,
    }),
  );
}

export async function getAssistant(assistantId: string): Promise<AssistantRecord> {
  const data = await sendApi((wire) => apiClient.GET('/api/v1/assistants/{assistant_id}', {
    ...wire,
    params: { path: { assistant_id: assistantId } },
  }));
  // The list page fans out over these and falls back to the summary record on
  // failure — but only on a thrown failure. A null returned quietly passes the
  // fallback by and reaches render as `assistant.assistant_id`, which unmounts
  // the tree and leaves a white page.
  if (data && typeof data === 'object' && 'assistant_id' in data) {
    return data as AssistantRecord;
  }
  throw new Error(
    i18n.t('misc:api_error.malformed_record', {
      endpoint: `/assistants/${assistantId}`,
      got: data === null ? 'null' : typeof data,
    }),
  );
}

export function createAssistant(config: Record<string, unknown>): Promise<AssistantRecord> {
  return sendApi((wire) => apiClient.POST('/api/v1/assistants', {
    ...wire,
    body: config,
  })) as Promise<AssistantRecord>;
}

/**
 * Patch an existing assistant's mutable fields. The backend
 * (PATCH /api/v1/assistants/{id}) sanitizes the body to the editable set
 * (display_name / icon / description / permission_mode_default / system /
 * *_override) and rejects identity fields (engine_kind / template_name) with 409 once the
 * workspace is materialized — so this only ever sends the mutable subset.
 */
export function updateAssistant(
  assistantId: string,
  updates: Record<string, unknown>,
): Promise<AssistantRecord> {
  return sendApi((wire) => apiClient.PATCH('/api/v1/assistants/{assistant_id}', {
    ...wire,
    params: { path: { assistant_id: assistantId } },
    body: updates,
  })) as Promise<AssistantRecord>;
}

export function deleteAssistant(assistantId: string): Promise<{ assistant_id: string; deleted: boolean }> {
  return sendApi((wire) => apiClient.DELETE('/api/v1/assistants/{assistant_id}', {
    ...wire,
    params: { path: { assistant_id: assistantId } },
  })) as Promise<{ assistant_id: string; deleted: boolean }>;
}

export function wakeAssistantWorkspace(assistantId: string): Promise<AssistantWorkspaceResult> {
  return sendApi((wire) => apiClient.POST('/api/v1/assistants/{assistant_id}/workspace/wake', {
    ...wire,
    params: { path: { assistant_id: assistantId } },
  }));
}

export function hibernateAssistantWorkspace(assistantId: string): Promise<{ assistant_id: string; hibernated: boolean }> {
  return sendApi((wire) => apiClient.POST('/api/v1/assistants/{assistant_id}/workspace/hibernate', {
    ...wire,
    params: { path: { assistant_id: assistantId } },
  })) as Promise<{ assistant_id: string; hibernated: boolean }>;
}

export function startAssistantConversation(
  assistantId: string,
): Promise<AssistantConversationResult> {
  const endpoint = `/api/v1/assistants/${encodeURIComponent(assistantId)}/conversations`;
  return startConversationRequest(
    (headers, signal) => sendApi((wire) => apiClient.POST(
      '/api/v1/assistants/{assistant_id}/conversations',
      { ...wire, params: { path: { assistant_id: assistantId } }, headers, signal },
    )),
    endpoint,
  );
}

/**
 * The caller's primary Assistant, created from the deployment's Assistant
 * defaults on first use. A deployment without a RuntimeProfile for Assistants
 * answers ASSISTANT_DEFAULTS_UNAVAILABLE (409).
 */
export async function ensurePrimaryAssistant(
  body: { display_name: string; timezone: string | null },
): Promise<AssistantRecord> {
  const data = await sendApi((wire) => apiClient.POST('/api/v1/assistants/primary', {
    ...wire,
    body,
  }));
  // A null here would read as "this user has no Assistant" and the rail would
  // show nothing; it is a malformed answer and is reported as one.
  if (data && typeof data === 'object' && 'assistant_id' in data) return data as AssistantRecord;
  throw new Error(
    i18n.t('misc:api_error.malformed_record', {
      endpoint: '/assistants/primary',
      got: data === null ? 'null' : typeof data,
    }),
  );
}

/** The conversation opening this Assistant lands in, started when it has none. */
export async function openMainConversation(assistantId: string): Promise<AssistantConversationResult> {
  const data = await sendApi((wire) => apiClient.POST('/api/v1/assistants/{assistant_id}/main-conversation', {
    ...wire,
    params: { path: { assistant_id: assistantId } },
  }));
  if (data && typeof data === 'object' && 'session_id' in data) return data as AssistantConversationResult;
  throw new Error(
    i18n.t('misc:api_error.malformed_record', {
      endpoint: `/assistants/${assistantId}/main-conversation`,
      got: data === null ? 'null' : typeof data,
    }),
  );
}

/** The deployment's extension catalog with this Assistant's selection marked. */
export async function getAssistantExtensions(assistantId: string): Promise<AssistantExtensionCatalog> {
  return await sendApi((wire) => apiClient.GET('/api/v1/assistants/{assistant_id}/extensions', {
    ...wire,
    params: { path: { assistant_id: assistantId } },
  })) as AssistantExtensionCatalog;
}

/** Select catalog MCP servers and Skills for the Assistant, as an Agent does. */
export async function setAssistantExtensions(
  assistantId: string,
  selection: { mcp_server_ids: string[]; skill_ids: string[] },
): Promise<AssistantExtensionCatalog> {
  return await sendApi((wire) => apiClient.PUT('/api/v1/assistants/{assistant_id}/extensions', {
    ...wire,
    params: { path: { assistant_id: assistantId } },
    body: selection,
  })) as AssistantExtensionCatalog;
}

/** The deployment's Assistant defaults, with the RuntimeProfile new Assistants would use. */
export async function getAssistantDefaults(): Promise<AssistantDefaults> {
  return await sendApi((wire) => apiClient.GET('/api/v1/admin/assistant-defaults', wire)) as AssistantDefaults;
}

/** The authored fields of a defaults read, as the replacing PUT takes them. */
export function authoredDefaults(view: AssistantDefaults): AssistantDefaultsFields {
  return {
    runtime_profile_name: view.runtime_profile_name ?? null,
    model_name: view.model_name ?? null,
    instructions: view.instructions ?? null,
    skills: view.skills ?? [],
    mcp_servers: view.mcp_servers ?? {},
    packages: view.packages ?? [],
    engine_options: view.engine_options ?? {},
    credential_vault_ids: view.credential_vault_ids ?? [],
  };
}

/** Replace the defaults' authored fields; the catalog selection is set separately. */
export async function setAssistantDefaults(fields: AssistantDefaultsFields): Promise<AssistantDefaults> {
  return await sendApi((wire) => apiClient.PUT('/api/v1/admin/assistant-defaults', {
    ...wire,
    body: fields,
  })) as AssistantDefaults;
}

/** The extension catalog with the defaults' selection marked. */
export async function getAssistantDefaultsExtensions(): Promise<AssistantDefaultsExtensionCatalog> {
  return await sendApi((wire) => apiClient.GET('/api/v1/admin/assistant-defaults/extensions', wire)) as AssistantDefaultsExtensionCatalog;
}

/** Select catalog MCP servers and Skills for every Assistant in the deployment. */
export async function setAssistantDefaultsExtensions(
  selection: { mcp_server_ids: string[]; skill_ids: string[] },
): Promise<AssistantDefaultsExtensionCatalog> {
  return await sendApi((wire) => apiClient.PUT('/api/v1/admin/assistant-defaults/extensions', {
    ...wire,
    body: selection,
  })) as AssistantDefaultsExtensionCatalog;
}
