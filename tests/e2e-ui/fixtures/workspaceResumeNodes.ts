/** Scheduling fault for persistent-workspace recovery across two real nodes. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect } from '@playwright/test';

const ROLE = 'astrabox.dev/workspace-resume-role';
const OWNER = 'astrabox.dev/workspace-resume-owner';
type Role = 'source' | 'target';
interface Node {
  metadata: { name: string; uid: string; resourceVersion: string;
    labels?: Record<string, string>; annotations?: Record<string, string> };
  spec: { unschedulable?: boolean };
  status: { conditions?: Array<{ type: string; status: string }> };
}

function kubectl(args: string[]): any {
  return JSON.parse(execFileSync('kubectl', [...args, '-o', 'json'], {
    encoding: 'utf8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'],
  }));
}

export class WorkspaceResumeNodes {
  private readonly nodes: Record<Role, Node>;
  private readonly changes = new Map<Role, Array<{ owner: string; unschedulable: boolean }>>();
  readonly evidence: Record<string, unknown> = { changes: [], restoration: [] };

  constructor() {
    const items: Node[] = kubectl(['get', 'nodes', '-l', ROLE]).items;
    const select = (role: Role): Node => {
      const matches = items.filter((node) => node.metadata.labels?.[ROLE] === role);
      expect(matches, `cross-host workspace resume requires exactly one ${ROLE}=${role} node`).toHaveLength(1);
      const node = matches[0];
      expect(node.status.conditions?.some((item) => item.type === 'Ready' && item.status === 'True'),
        `${role} node ${node.metadata.name} must be Ready`).toBe(true);
      expect(node.metadata.annotations?.[OWNER], 'another scheduling fixture owns this node').toBeUndefined();
      return node;
    };
    this.nodes = { source: select('source'), target: select('target') };
    expect(this.name('source')).not.toBe(this.name('target'));
    this.evidence.original = this.nodes;
  }

  name(role: Role): string { return this.nodes[role].metadata.name; }

  private read(role: Role): Node {
    const node: Node = kubectl(['get', 'node', this.name(role)]);
    expect(node.metadata.uid, `${role} node was replaced`).toBe(this.nodes[role].metadata.uid);
    return node;
  }

  private set(role: Role, unschedulable: boolean): void {
    const node = this.read(role);
    const changes = this.changes.get(role) ?? [];
    const previous = changes.at(-1);
    expect(node.metadata.annotations?.[OWNER], `${role} scheduling ownership changed`)
      .toBe(previous?.owner);
    expect(node.spec.unschedulable ?? false, `${role} schedulability changed outside this fixture`)
      .toBe(previous?.unschedulable ?? this.nodes[role].spec.unschedulable ?? false);
    // Record the attempted write before calling kubectl: a lost reply is uncertain.
    const owner = randomUUID();
    changes.push({ owner, unschedulable });
    this.changes.set(role, changes);
    const result: Node = kubectl(['patch', 'node', this.name(role), '--type=merge', '-p', JSON.stringify({
      metadata: { resourceVersion: node.metadata.resourceVersion, annotations: { [OWNER]: owner } },
      spec: { unschedulable },
    })]);
    expect(result.spec.unschedulable ?? false).toBe(unschedulable);
    (this.evidence.changes as unknown[]).push({ role, node: result.metadata.name,
      resourceVersion: result.metadata.resourceVersion, unschedulable });
  }

  prepareSource(): void {
    this.set('target', true);
    this.set('source', false);
  }

  prepareReplacement(): void {
    this.set('source', true);
    this.set('target', false);
  }

  restore(): void {
    const errors: unknown[] = [];
    for (const [role, changes] of this.changes) {
      try {
        const node = this.read(role);
        const original = this.nodes[role].spec.unschedulable;
        if (node.metadata.annotations?.[OWNER] === undefined
          && node.spec.unschedulable === original) continue;
        const owned = changes.find((change) => change.owner === node.metadata.annotations?.[OWNER]);
        expect(owned, `${role} scheduling ownership changed; refusing restore`).toBeDefined();
        expect(node.spec.unschedulable ?? false, `${role} schedulability changed; refusing restore`)
          .toBe(owned!.unschedulable);
        const restored: Node = kubectl(['patch', 'node', this.name(role), '--type=merge', '-p', JSON.stringify({
          metadata: { resourceVersion: node.metadata.resourceVersion, annotations: { [OWNER]: null } },
          spec: { unschedulable: original ?? null },
        })]);
        expect(restored.spec.unschedulable).toBe(original);
        expect(restored.metadata.annotations?.[OWNER]).toBeUndefined();
        (this.evidence.restoration as unknown[]).push({ role, node: restored.metadata.name,
          unschedulable: restored.spec.unschedulable ?? false });
      } catch (error) {
        errors.push(error);
        (this.evidence.restoration as unknown[]).push({ role, error: String(error) });
      }
    }
    if (errors.length) throw new AggregateError(errors, 'could not restore workspace-resume node scheduling');
  }
}
