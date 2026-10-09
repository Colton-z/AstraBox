/**
 * A test-owned copy of a deployed Environment whose limited network admits
 * more, as an administrator would grant it, so the shared Environment keeps
 * its policy. Only the fields the Environment schema declares are copied.
 * The caller disables the copy when its test passes.
 */
import type { AstraApi } from './astraApi';

export async function copyEnvironment(
  api: AstraApi,
  source: Record<string, unknown>,
  name: string,
  grant: { hosts: string[]; allowMcpServers?: boolean },
): Promise<Record<string, unknown>> {
  const networking = (source.networking ?? {}) as Record<string, unknown>;
  const schema = await api.data<{ fields?: Array<Record<string, unknown>> }>('GET', '/admin/environment-schema');
  const fields = new Set((schema.fields ?? []).map((field) => String(field.key)));
  const copy: Record<string, unknown> = {
    ...Object.fromEntries(Object.entries(source).filter(([key]) => fields.has(key))),
    name,
    display_name: name,
    enabled: true,
    networking: {
      ...networking,
      ...(grant.allowMcpServers ? { allow_mcp_servers: true } : {}),
      allowed_hosts: [...(Array.isArray(networking.allowed_hosts) ? networking.allowed_hosts : []), ...grant.hosts],
    },
  };
  await api.data('PUT', `/admin/environments/${encodeURIComponent(name)}`, copy);
  return copy;
}
