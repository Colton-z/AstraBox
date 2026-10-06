/** Feed platform bindings to the pinned harness's native MCP client. */
import { Config, apply as applyMcp, inject } from '@deepseek-ai/dsh-mcp-client';

export { inject };

export async function apply(ctx) {
  try {
    const servers = JSON.parse(process.env.ASTRABOX_PLATFORM_MCP_SERVERS ?? '[]');
    if (!Array.isArray(servers)) throw new Error('platform MCP servers must be an array');
    for (const server of servers) {
      if (!server || typeof server.name !== 'string' || typeof server.url !== 'string' || !server.url) {
        throw new Error('platform MCP server requires a name and URL');
      }
      await applyMcp(ctx, Config({
        serverName: server.name,
        transport: 'streamable-http',
        url: server.url,
        failOnStartupError: true,
        reconnect: { enabled: false },
      }));
    }
  } catch (error) {
    // App-boot permits optional plugin failures; platform tools are required.
    console.error('AstraBox platform MCP initialization failed:', error);
    process.exit(1);
  }
}
