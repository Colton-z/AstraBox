/** Register platform endpoints with Pi's built-in MCP support before session_start. */
export default function platformMcp(pi) {
  try {
    const servers = JSON.parse(process.env.ASTRABOX_PLATFORM_MCP_SERVERS ?? '[]');
    if (!Array.isArray(servers)) throw new Error('Platform MCP servers must be an array');
    for (const server of servers) {
      if (!server || typeof server.name !== 'string' || typeof server.url !== 'string') {
        throw new Error('Platform MCP server requires name and URL');
      }
      pi.registerMcpServer(server.name, { url: server.url, exposure: 'direct' });
    }
  } catch (error) {
    // Pi records extension-load errors and continues. Required registrations
    // must instead make startup fail; native MCP owns subsequent connections.
    console.error('astrabox-platform-mcp registration failed:', error);
    process.exit(1);
  }
}
