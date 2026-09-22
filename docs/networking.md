# IP Addresses

AstraBox does not use a fixed global IP address pool for outbound connections
to remote MCP servers. Because AstraBox is self-hosted, these connections leave
through the network configured for your sandbox deployment. You can use that
deployment's stable egress addresses to configure firewall allowlists so your
remote MCP servers accept traffic from AstraBox Agents.

These addresses are owned by your infrastructure, not by AstraBox. Depending on
your network topology, remote MCP connections and other sandbox public-network
egress can use the same addresses.

## Outbound IP Addresses (MCP)

Use the public egress IP address or CIDR configured for your sandbox
infrastructure. AstraBox does not provide a product-wide CIDR to list here.

How a remote MCP server is configured does not give it a separate network path.
The Agent program connects to every remote MCP endpoint from its sandbox.

An Environment controls which destinations the sandbox can reach:

| Mode | Behavior |
| --- | --- |
| `Limited` | Allows platform-required connections, destinations authorized by linked Credential Vaults, and the additional hosts you enter. Remote MCP endpoints declared by an Agent are allowed only when **Allow remote MCP servers** is enabled. |
| `Unrestricted` | Allows every outbound destination. |

## Firewall Configuration

To allow remote MCP connections from AstraBox to reach your server, determine
the stable public egress address of the sandbox deployment and add that IP or
CIDR to your server's inbound firewall rules. A NAT gateway or another managed
egress service can provide a stable address when the sandbox nodes themselves
are replaceable.

> The Environment network allowlist controls which destinations a sandbox may
> reach. It does not assign a stable public source IP. Additional allowed hosts
> can be exact hosts, leftmost wildcards such as `*.example.com`, IP addresses,
> or CIDRs; do not include a scheme, port, or path.

## FAQ

**Q: Will these IP addresses change?**

A: AstraBox does not assign or rotate them. They change when your deployment's
network configuration changes. Use a stable NAT or egress address if downstream
firewalls require a fixed source IP.

**Q: Do sandbox outbound connections also come from these IPs?**

A: Remote MCP connections are sandbox outbound connections in AstraBox. They
use the sandbox deployment's egress path, as does other sandbox traffic, unless
your infrastructure explicitly routes them differently. AstraBox does not
reserve a separate MCP IP pool.
