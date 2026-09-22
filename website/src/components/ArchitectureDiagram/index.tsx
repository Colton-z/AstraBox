import type {ReactNode} from 'react';
import Translate, {translate} from '@docusaurus/Translate';

import styles from './styles.module.css';

type BoxProps = {
  x: number;
  y: number;
  w: number;
  h: number;
  title: string;
  sub?: string;
  accent?: boolean;
};

function Box({x, y, w, h, title, sub, accent}: BoxProps) {
  const cx = x + w / 2;
  const cy = y + h / 2;
  return (
    <g>
      <rect
        x={x}
        y={y}
        width={w}
        height={h}
        rx={8}
        className={accent ? styles.boxAccent : styles.box}
      />
      <text x={cx} y={sub ? cy - 4 : cy + 5} className={styles.boxTitle}>
        {title}
      </text>
      {sub && (
        <text x={cx} y={cy + 15} className={styles.boxSub}>
          {sub}
        </text>
      )}
    </g>
  );
}

function EdgeLabel({
  x,
  y,
  width,
  text,
}: {
  x: number;
  y: number;
  width: number;
  text: string;
}) {
  return (
    <g>
      <rect
        x={x - width / 2}
        y={y - 9}
        width={width}
        height={16}
        rx={4}
        className={styles.chip}
      />
      <text x={x} y={y + 3} className={styles.edgeLabel}>
        {text}
      </text>
    </g>
  );
}

export default function ArchitectureDiagram(): ReactNode {
  const labels = {
    aria: translate({
      id: 'home.arch.diagram.aria',
      message:
        'AstraBox receives requests from the web console or API, creates or resumes a sandbox through OpenSandbox, sends tasks to an Agent program inside the sandbox, stores Session history outside the sandbox, and connects the Agent program to a configured model service.',
    }),
    backendGroup: translate({
      id: 'home.arch.diagram.backendGroup',
      message: 'AstraBox service',
    }),
    sandboxGroup: translate({
      id: 'home.arch.diagram.sandboxGroup',
      message: 'Agent sandbox',
    }),
    console: translate({
      id: 'home.arch.diagram.console',
      message: 'web console / API',
    }),
    consoleSub: translate({
      id: 'home.arch.diagram.consoleSub',
      message: 'requests · live output',
    }),
    sessionService: translate({
      id: 'home.arch.diagram.sessionService',
      message: 'Session service',
    }),
    sessionServiceSub: translate({
      id: 'home.arch.diagram.sessionServiceSub',
      message: 'streaming · approvals',
    }),
    openSandbox: translate({
      id: 'home.arch.diagram.openSandbox',
      message: 'OpenSandbox',
    }),
    openSandboxSub: translate({
      id: 'home.arch.diagram.openSandboxSub',
      message: 'sandbox lifecycle',
    }),
    sessionStore: translate({
      id: 'home.arch.diagram.sessionStore',
      message: 'Session storage',
    }),
    sessionStoreSub: translate({
      id: 'home.arch.diagram.sessionStoreSub',
      message: 'history · state',
    }),
    bridge: translate({
      id: 'home.arch.diagram.bridge',
      message: 'AstraBox sandbox service',
    }),
    bridgeSub: translate({
      id: 'home.arch.diagram.bridgeSub',
      message: 'connects to the Agent program',
    }),
    program: translate({
      id: 'home.arch.diagram.program',
      message: 'Agent program',
    }),
    programSub: translate({
      id: 'home.arch.diagram.programSub',
      message: 'selected by the Environment',
    }),
    workspace: translate({
      id: 'home.arch.diagram.workspace',
      message: 'workspace',
    }),
    workspaceSub: translate({
      id: 'home.arch.diagram.workspaceSub',
      message: 'files · commands',
    }),
    modelEndpoint: translate({
      id: 'home.arch.diagram.modelEndpoint',
      message: 'model service',
    }),
    modelEndpointSub: translate({
      id: 'home.arch.diagram.modelEndpointSub',
      message: 'configured by the operator',
    }),
    tasksEvents: translate({
      id: 'home.arch.diagram.tasksEvents',
      message: 'tasks · events',
    }),
    lifecycle: translate({
      id: 'home.arch.diagram.lifecycle',
      message: 'create · pause · resume',
    }),
    modelRequests: translate({
      id: 'home.arch.diagram.modelRequests',
      message: 'model requests',
    }),
    history: translate({
      id: 'home.arch.diagram.history',
      message: 'Session history',
    }),
  };

  const mobileSteps = [
    {title: labels.console, detail: labels.consoleSub},
    {
      title: labels.sessionService,
      detail: `${labels.sessionServiceSub} · ${labels.sessionStore}`,
    },
    {title: labels.openSandbox, detail: labels.openSandboxSub},
    {
      title: labels.program,
      detail: `${labels.bridge} · ${labels.workspace}`,
    },
    {title: labels.modelEndpoint, detail: labels.modelEndpointSub},
  ];

  return (
    <div className={styles.diagram}>
      <svg
        viewBox="0 0 1160 400"
        className={styles.svg}
        role="img"
        aria-label={labels.aria}>
      <defs>
        <marker
          id="arrowSolid"
          viewBox="0 0 10 10"
          refX="9"
          refY="5"
          markerWidth="6"
          markerHeight="6"
          orient="auto-start-reverse">
          <path d="M 0 1 L 9 5 L 0 9 z" className={styles.arrowHead} />
        </marker>
      </defs>

      <rect x={180} y={40} width={330} height={340} rx={12} className={styles.group} />
      <rect x={590} y={40} width={300} height={290} rx={12} className={styles.group} />

      <Box x={8} y={76} w={132} h={64} title={labels.console} sub={labels.consoleSub} />

      <Box
        x={210}
        y={76}
        w={270}
        h={64}
        title={labels.sessionService}
        sub={labels.sessionServiceSub}
      />
      <Box
        x={210}
        y={190}
        w={270}
        h={64}
        title={labels.openSandbox}
        sub={labels.openSandboxSub}
      />
      <Box
        x={210}
        y={300}
        w={270}
        h={56}
        title={labels.sessionStore}
        sub={labels.sessionStoreSub}
      />

      <Box x={620} y={76} w={240} h={54} title={labels.bridge} sub={labels.bridgeSub} />
      <Box
        x={620}
        y={160}
        w={240}
        h={64}
        title={labels.program}
        sub={labels.programSub}
        accent
      />
      <Box
        x={620}
        y={254}
        w={240}
        h={54}
        title={labels.workspace}
        sub={labels.workspaceSub}
      />

      <Box
        x={965}
        y={160}
        w={180}
        h={64}
        title={labels.modelEndpoint}
        sub={labels.modelEndpointSub}
      />

      <path d="M 140 108 H 204" className={styles.edge} markerEnd="url(#arrowSolid)" />
      <path d="M 480 108 H 614" className={styles.edge} markerEnd="url(#arrowSolid)" />
      <path d="M 345 140 V 184" className={styles.edge} markerEnd="url(#arrowSolid)" />
      <path d="M 740 130 V 154" className={styles.edge} markerEnd="url(#arrowSolid)" />
      <path d="M 740 224 V 248" className={styles.edge} markerEnd="url(#arrowSolid)" />
      <path d="M 860 192 H 959" className={styles.edge} markerEnd="url(#arrowSolid)" />
      <path
        d="M 620 125 H 550 V 328 H 486"
        className={styles.edgeDashed}
        markerEnd="url(#arrowSolid)"
      />

      <text x={190} y={30} className={styles.groupLabel}>
        {labels.backendGroup}
      </text>
      <text x={600} y={30} className={styles.groupLabel}>
        {labels.sandboxGroup}
      </text>

      <EdgeLabel x={550} y={96} width={112} text={labels.tasksEvents} />
      <EdgeLabel x={345} y={164} width={138} text={labels.lifecycle} />
      <EdgeLabel x={912} y={180} width={94} text={labels.modelRequests} />
        <EdgeLabel x={550} y={318} width={102} text={labels.history} />
      </svg>
      <ol
        className={styles.mobileFlow}
        data-testid="mobile-architecture-flow"
        aria-label={labels.aria}>
        {mobileSteps.map((step, index) => (
          <li key={step.title} className={styles.mobileStep}>
            <span className={styles.mobileIndex}>{String(index + 1).padStart(2, '0')}</span>
            <span>
              <strong className={styles.mobileTitle}>{step.title}</strong>
              <span className={styles.mobileDetail}>{step.detail}</span>
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

export function ArchitectureNote(): ReactNode {
  return (
    <p className={styles.note}>
      <Translate id="home.arch.note">
        Solid lines show live requests and events. The dashed line shows
        Session history stored by AstraBox. On snapshot-capable deployments,
        pausing preserves sandbox files; resuming starts fresh processes from
        those files.
      </Translate>
    </p>
  );
}
