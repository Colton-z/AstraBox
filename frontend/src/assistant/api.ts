import i18n from '@/i18n';

import type {
  AssistantConversationResult,
  AssistantRecord,
  AssistantWorkspaceResult,
} from './types';
import { apiClient, sendApi, startConversationRequest } from '../api';

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
 * (display_name / icon / description / permission_mode_default / *_override) and
 * rejects identity fields (engine_kind / template_name) with 409 once the
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
