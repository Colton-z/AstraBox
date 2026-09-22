import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

// The Agent workflow comes first. Self-hosting and extension guides remain
// separate categories so they do not interrupt it.
const sidebars: SidebarsConfig = {
  docs: [
    {
      type: 'category',
      label: 'Quick start',
      collapsed: false,
      items: ['overview', 'capabilities', 'quickstart'],
    },
    {
      type: 'category',
      label: 'Build Agent',
      collapsed: false,
      items: [
        'authoring-agents',
        'adding-tools',
        'agent-skills',
        'permission-modes',
        'models',
      ],
    },
    {
      type: 'category',
      label: 'Configure Agent environment',
      items: ['environments', 'container-reference', 'networking'],
    },
    {
      type: 'category',
      label: 'Delegate tasks',
      items: [
        'sessions',
        'events-stream',
        'working-with-repos',
        'credentials',
        'multi-agents',
      ],
    },
    {
      type: 'category',
      label: 'Integrate Agent',
      items: [
        'schedules',
        'channels',
        'webhooks',
        'deployments',
        'agent-mcp',
      ],
    },
    {
      type: 'category',
      label: 'Manage Agent context',
      items: ['files', 'assistants'],
    },
    {
      type: 'category',
      label: 'Best practices',
      items: ['example-investment-research', 'compare-claude-code-self-hosted'],
    },
    {
      type: 'category',
      label: 'CLI',
      items: ['cli/overview', 'cli/commands', 'cli/configuration'],
    },
    {
      type: 'category',
      label: 'API conventions',
      items: [
        'api',
        'api-authentication',
        'api-pagination',
        'api-errors',
        'api-data-structures',
      ],
    },
    {
      type: 'category',
      label: 'Self-host AstraBox',
      items: [
        'deploy',
        'deploy-distributed',
        'team-login',
        'egress-credential-injection',
        'providers/opensandbox',
        'providers/aws-efs',
        'architecture',
      ],
    },
    {
      type: 'category',
      label: 'Extend AstraBox',
      items: [
        'embedding',
        'writing-an-engine-adapter',
        'writing-a-channel-provider',
      ],
    },
  ],
};

export default sidebars;
