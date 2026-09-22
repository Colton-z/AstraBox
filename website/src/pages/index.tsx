import type {ReactNode} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import Translate, {translate} from '@docusaurus/Translate';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import CodeBlock from '@theme/CodeBlock';

import ArchitectureDiagram, {
  ArchitectureNote,
} from '@site/src/components/ArchitectureDiagram';
import styles from './index.module.css';

const INSTALL =
  'curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh | bash';

const ICONS: Record<string, ReactNode> = {
  terminal: (
    <>
      <path d="M4 7l4 4-4 4" />
      <path d="M12.5 15.5H20" />
    </>
  ),
  cube: (
    <>
      <path d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z" />
      <path d="M4 7.5L12 12l8-4.5M12 12v9" />
    </>
  ),
  gateway: (
    <>
      <path d="M3 6h6a3 3 0 0 1 3 3v6a3 3 0 0 0 3 3h6" />
      <path d="M3 18h6a3 3 0 0 0 3-3" />
      <path d="M18 3l3 3-3 3" />
    </>
  ),
  shield: (
    <>
      <path d="M12 3l7 3v6c0 4.2-2.9 7.6-7 9-4.1-1.4-7-4.8-7-9V6l7-3z" />
      <path d="M8.5 12l2.2 2.2 4.8-5" />
    </>
  ),
  messages: (
    <>
      <path d="M4 5.5h11a3 3 0 0 1 3 3v4a3 3 0 0 1-3 3H9l-4.5 3v-3.5A3 3 0 0 1 2 12V8.5a3 3 0 0 1 2-3z" />
      <path d="M8 9.5h6M8 12.5h3.5" />
    </>
  ),
};

function Icon({name, tone}: {name: string; tone: string}) {
  return (
    <span className={styles.iconTile} data-tone={tone} aria-hidden="true">
      <svg
        viewBox="0 0 24 24"
        width="19"
        height="19"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round">
        {ICONS[name]}
      </svg>
    </span>
  );
}

function Hero() {
  return (
    <header className={styles.hero}>
      <div className={styles.starfield} />
      <div className={styles.heroInner}>
        <div className={styles.heroCopy}>
          <div className={styles.eyebrow}>
            <span className={styles.eyeDot} />
            <Translate id="home.hero.eyebrow">
              open source · self-hosted · apache-2.0
            </Translate>
          </div>
          <Heading as="h1" className={styles.display}>
            <Translate id="home.hero.title">
              The open-source alternative to
            </Translate>{' '}
            <span className={styles.soft}>
              <Translate id="home.hero.titleSoft">
                Claude Managed Agents.
              </Translate>
            </span>
          </Heading>
          <p className={styles.sub}>
            <Translate id="home.hero.sub">
              Run Claude Code, Codex, Hermes, DeepSeek Harness and Pi as
              managed Agents on your own infrastructure, with any model.
              Conversations start and resume in seconds, while Sessions,
              sandboxes, credentials and history stay under your control.
            </Translate>
          </p>
          <div className={styles.ctaRow}>
            <Link className={styles.btnPrimary} to="/docs/quickstart">
              <Translate id="home.hero.ctaPrimary">Get started</Translate>
              <span aria-hidden="true"> →</span>
            </Link>
            <Link className={styles.btnSecondary} to="/docs/overview">
              <Translate id="home.hero.ctaDocs">Read the overview</Translate>
            </Link>
          </div>
        </div>

        <div className={styles.heroTerminal}>
          <div className={styles.termBar}>
            <span className={styles.termDots} aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
            <span className={styles.termTitle}>
              <Translate id="home.hero.termTitle">
                start AstraBox on one machine
              </Translate>
            </span>
          </div>
          <CodeBlock language="bash" className={styles.termCode}>
            {INSTALL}
          </CodeBlock>
        </div>
      </div>
    </header>
  );
}

type Cell = {
  icon: string;
  tone: string;
  tag: ReactNode;
  title: ReactNode;
  body: ReactNode;
};

const WORKFLOW: Cell[] = [
  {
    icon: 'terminal',
    tone: 'astra',
    tag: <Translate id="home.workflow.deploy.tag">Step 1</Translate>,
    title: <Translate id="home.workflow.deploy.title">Deploy AstraBox</Translate>,
    body: (
      <Translate id="home.workflow.deploy.body">
        Run the AstraBox service and OpenSandbox on a Docker host, Kubernetes,
        or infrastructure you already operate.
      </Translate>
    ),
  },
  {
    icon: 'cube',
    tone: 'mint',
    tag: <Translate id="home.workflow.environment.tag">Step 2</Translate>,
    title: (
      <Translate id="home.workflow.environment.title">
        Configure an Environment
      </Translate>
    ),
    body: (
      <Translate id="home.workflow.environment.body">
        Choose the Agent program, sandbox image, model connection, network
        access, and lifecycle.
      </Translate>
    ),
  },
  {
    icon: 'shield',
    tone: 'citrine',
    tag: <Translate id="home.workflow.agent.tag">Step 3</Translate>,
    title: <Translate id="home.workflow.agent.title">Create an Agent</Translate>,
    body: (
      <Translate id="home.workflow.agent.body">
        Select the Environment and model. Add a system prompt, MCP servers,
        Plugins, Skills, or a repository only when the Agent needs them.
      </Translate>
    ),
  },
  {
    icon: 'messages',
    tone: 'plasma',
    tag: <Translate id="home.workflow.session.tag">Step 4</Translate>,
    title: (
      <Translate id="home.workflow.session.title">
        Start a Session and follow Events
      </Translate>
    ),
    body: (
      <Translate id="home.workflow.session.body">
        Send a message, follow live output, answer questions or approvals, and
        return later without keeping the original browser open.
      </Translate>
    ),
  },
];

const USE_CASES: Cell[] = [
  {
    icon: 'terminal',
    tone: 'astra',
    tag: <Translate id="home.use.long.tag">Long-running</Translate>,
    title: (
      <Translate id="home.use.long.title">
        Let work continue after you disconnect
      </Translate>
    ),
    body: (
      <Translate id="home.use.long.body">
        Run coding, research, operations, and other tasks without depending on
        a developer computer or browser staying online.
      </Translate>
    ),
  },
  {
    icon: 'gateway',
    tone: 'mint',
    tag: <Translate id="home.use.api.tag">API integration</Translate>,
    title: (
      <Translate id="home.use.api.title">Use Agents from your products</Translate>
    ),
    body: (
      <Translate id="home.use.api.body">
        Start and follow Agent work from an application without building and
        operating another Agent runtime.
      </Translate>
    ),
  },
  {
    icon: 'cube',
    tone: 'citrine',
    tag: <Translate id="home.use.batch.tag">Batch processing</Translate>,
    title: (
      <Translate id="home.use.batch.title">Run independent work in parallel</Translate>
    ),
    body: (
      <Translate id="home.use.batch.body">
        Start multiple Sessions for bulk requests while keeping each Session's
        state and output separate.
      </Translate>
    ),
  },
  {
    icon: 'messages',
    tone: 'plasma',
    tag: <Translate id="home.use.trigger.tag">Automations</Translate>,
    title: (
      <Translate id="home.use.trigger.title">
        Start work on a schedule or event
      </Translate>
    ),
    body: (
      <Translate id="home.use.trigger.body">
        Connect an Agent to schedules, webhooks, external systems, or messaging
        platforms and keep a Run record for every execution.
      </Translate>
    ),
  },
];

const NEXT_STEPS: Cell[] = [
  {
    icon: 'terminal',
    tone: 'astra',
    tag: <Translate id="home.next.start.tag">Quickstart</Translate>,
    title: <Translate id="home.next.start.title">Run AstraBox locally</Translate>,
    body: (
      <>
        <Translate id="home.next.start.body">
          Start the maintained Docker deployment, open the web console, and
          create an Agent.
        </Translate>{' '}
        <Link to="/docs/quickstart">
          <Translate id="home.next.start.link">Follow the quickstart →</Translate>
        </Link>
      </>
    ),
  },
  {
    icon: 'shield',
    tone: 'mint',
    tag: <Translate id="home.next.deploy.tag">Deployment</Translate>,
    title: <Translate id="home.next.deploy.title">Deploy for a team</Translate>,
    body: (
      <>
        <Translate id="home.next.deploy.body">
          Add authentication, durable storage, TLS, backups, and the sandbox
          capacity your team needs.
        </Translate>{' '}
        <Link to="/docs/deploy">
          <Translate id="home.next.deploy.link">Read the deployment guide →</Translate>
        </Link>
      </>
    ),
  },
  {
    icon: 'gateway',
    tone: 'plasma',
    tag: <Translate id="home.next.integrate.tag">Integration</Translate>,
    title: (
      <Translate id="home.next.integrate.title">
        Connect AstraBox to your systems
      </Translate>
    ),
    body: (
      <>
        <Translate id="home.next.integrate.body">
          Use the HTTP API with your applications, and connect your existing
          authentication, model, storage, and messaging services.
        </Translate>{' '}
        <Link to="/docs/api">
          <Translate id="home.next.integrate.link">Open the API guide →</Translate>
        </Link>
      </>
    ),
  },
];

function SectionHead({
  index,
  eyebrow,
  title,
}: {
  index: string;
  eyebrow: ReactNode;
  title: ReactNode;
}) {
  return (
    <>
      <div className={styles.sectionEyebrow}>
        {index} · {eyebrow}
      </div>
      <Heading as="h2" className={styles.sectionTitle}>
        {title}
      </Heading>
    </>
  );
}

function GridSection({
  index,
  eyebrow,
  title,
  cells,
  columns,
}: {
  index: string;
  eyebrow: ReactNode;
  title: ReactNode;
  cells: Cell[];
  columns: 2 | 3;
}) {
  return (
    <section className={styles.section}>
      <div className={styles.sectionInner}>
        <SectionHead index={index} eyebrow={eyebrow} title={title} />
        <div
          className={clsx(
            styles.grid,
            columns === 3 ? styles.grid3 : styles.grid2,
          )}>
          {cells.map((cell, index) => (
            <div key={index} className={styles.cell}>
              <div className={styles.cellHead}>
                <Icon name={cell.icon} tone={cell.tone} />
                <div className={styles.cellTag}>{cell.tag}</div>
              </div>
              <Heading as="h3" className={styles.cellTitle}>
                {cell.title}
              </Heading>
              <p className={styles.cellBody}>{cell.body}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Concepts() {
  return (
    <section className={styles.section}>
      <div className={styles.sectionInner}>
        <SectionHead
          index="01"
          eyebrow={<Translate id="home.concepts.eyebrow">Core concepts</Translate>}
          title={
            <Translate id="home.concepts.title">
              Agent, Environment, Session, and Event
            </Translate>
          }
        />
        <div className={styles.conceptTableWrap}>
          <table className={styles.conceptTable}>
            <thead>
              <tr>
                <th><Translate id="home.concepts.head.concept">Concept</Translate></th>
                <th><Translate id="home.concepts.head.description">Description</Translate></th>
                <th><Translate id="home.concepts.head.analogy">Analogy</Translate></th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <th scope="row">Agent</th>
                <td><Translate id="home.concepts.agent">A cloud Agent powered by an installed Agent program.</Translate></td>
                <td><Translate id="home.concepts.agent.analogy">Cloud teammate</Translate></td>
              </tr>
              <tr>
                <th scope="row">Environment</th>
                <td><Translate id="home.concepts.environment">The Agent program, sandbox, model connection, network access, and lifecycle used for a Session.</Translate></td>
                <td><Translate id="home.concepts.environment.analogy">Desk and toolbox</Translate></td>
              </tr>
              <tr>
                <th scope="row">Session</th>
                <td><Translate id="home.concepts.session">One stateful Agent execution, including its messages, Events, and current state.</Translate></td>
                <td><Translate id="home.concepts.session.analogy">A specific piece of work</Translate></td>
              </tr>
              <tr>
                <th scope="row">Event</th>
                <td><Translate id="home.concepts.event">The real-time output and state changes produced by a Session.</Translate></td>
                <td><Translate id="home.concepts.event.analogy">Live progress feed</Translate></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      description={translate({
        id: 'home.meta.description',
        message:
          'The open-source, self-hosted alternative to Claude Managed Agents: conversations start and resume in seconds, with any model.',
      })}>
      <Hero />
      <main>
        <Concepts />
        <GridSection
          index="02"
          eyebrow={<Translate id="home.workflow.eyebrow">Workflow</Translate>}
          title={
            <Translate id="home.workflow.title">
              From deployment to a running Agent
            </Translate>
          }
          cells={WORKFLOW}
          columns={2}
        />
        <GridSection
          index="03"
          eyebrow={<Translate id="home.use.eyebrow">When to use AstraBox</Translate>}
          title={
            <Translate id="home.use.title">
              Put cloud Agents to work in your products and operations
            </Translate>
          }
          cells={USE_CASES}
          columns={2}
        />
        <section className={styles.section}>
          <div className={styles.sectionInner}>
            <SectionHead
              index="04"
              eyebrow={<Translate id="home.arch.eyebrow">How it works</Translate>}
              title={
                <Translate id="home.arch.title">
                  AstraBox keeps the Agent available; OpenSandbox runs its sandbox
                </Translate>
              }
            />
            <p className={styles.archBody}>
              <Translate id="home.arch.body">
                Clients connect to AstraBox. AstraBox authenticates the caller,
                stores Session state, creates or resumes a sandbox through
                OpenSandbox, and sends work to the selected Agent program. Events
                stream back while Session history remains outside the sandbox.
              </Translate>
            </p>
            <div className={styles.diagramFrame}>
              <ArchitectureDiagram />
            </div>
            <ArchitectureNote />
          </div>
        </section>
        <GridSection
          index="05"
          eyebrow={<Translate id="home.next.eyebrow">Next steps</Translate>}
          title={
            <Translate id="home.next.title">Choose what you want to do next</Translate>
          }
          cells={NEXT_STEPS}
          columns={3}
        />
      </main>
    </Layout>
  );
}
