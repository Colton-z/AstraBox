/**
 * The deployment's Agent engine profiles, for specs that must drive every engine.
 *
 * This is the browser counterpart of `tests/e2e/_engine_profile.py` and reads
 * the same evidence file the Python lane reads, so the two lanes cannot drift
 * into disagreeing about what an engine declares. The difference is the unit:
 * a Python run drives the one engine named by `ASTRABOX_E2E_ENGINE_KIND`, while
 * a browser spec parameterises itself over every engine declaring the contract
 * it exercises.
 *
 * Why it exists: the browser lane was claude-only, not by decision but because
 * nothing carried the matrix to it. A Codex approval card could therefore
 * render no options at all while every lane reported green — the Python lane
 * answers interactions through `interaction-respond` and never renders a card,
 * and the one browser spec that touches a decision drives Claude's exit-plan
 * confirmation, whose option ids are the two the console happened to know.
 */
import fs from 'node:fs';
import path from 'node:path';

const MATRIX_EVIDENCE_ENV = 'ASTRABOX_E2E_AGENT_MATRIX_FILE';

/** A tool role the matrix declares per engine, e.g. `write` → `commandExecution`. */
export type ToolRole = 'command' | 'question' | 'subagent' | 'write';

/** A permission-mode role, e.g. `gated` → Codex's `read-only`. */
export type ModeRole = 'alternate' | 'gated' | 'plan' | 'unattended';

/** A decision role, e.g. `approve` → Codex's `accept`. */
export type DecisionRole = 'approve' | 'reject';

export interface EngineProfile {
  agent_id: string;
  agent_name: string;
  engine_kind: string;
  environment_name: string;
  model: string;
  /** What this engine can be asked to do at all; a false one is not a gap. */
  contracts: Record<string, boolean>;
  /** The engine's own decision ids, by role. Empty when it raises none. */
  decisions: Partial<Record<DecisionRole, string>>;
  /** The engine's own tool names, by role. */
  tools: Partial<Record<ToolRole, string[]>>;
  /** The engine's own permission-mode names, by role. */
  modes: Partial<Record<ModeRole, string>>;
  /** Which card an approval reaches a person through: `decision`, `tool_approval`, or none. */
  presentation: string | null;
  [key: string]: unknown;
}

function text(value: unknown, label: string): string {
  const resolved = String(value ?? '').trim();
  if (!resolved) throw new Error(`Agent E2E profile ${label} must be a non-empty string`);
  return resolved;
}

let cached: EngineProfile[] | null = null;

export type EngineCase = Pick<EngineProfile, 'engine_kind' | 'contracts' | 'tools'>;

/** Enumerate tests from the same checked-in catalog that configures the deployment. */
export function engineCases(): EngineCase[] {
  const catalog = JSON.parse(fs.readFileSync(path.resolve(
    __dirname, '../../../tests/e2e-contract/agent-engine-matrix.json',
  ), 'utf-8'));
  if (catalog.version !== 1 || !Array.isArray(catalog.profiles) || !catalog.profiles.length) {
    throw new Error('Agent E2E catalog must contain version 1 profiles');
  }
  const kinds = new Set<string>();
  return catalog.profiles.map((profile: EngineCase) => {
    const kind = text(profile.engine_kind, 'engine_kind');
    if (kinds.has(kind)) throw new Error(`Duplicate Agent E2E catalog engine: ${kind}`);
    kinds.add(kind);
    for (const key of ['contracts', 'tools'] as const) {
      if (!profile[key] || typeof profile[key] !== 'object' || Array.isArray(profile[key])) {
        throw new Error(`Agent E2E catalog ${kind}.${key} must be an object`);
      }
    }
    return { engine_kind: kind, contracts: profile.contracts, tools: profile.tools };
  });
}

/** Resolve execution inputs inside a test; collection never invents deployed identities. */
export function engineProfileFor(engineKind: string): EngineProfile {
  const matches = engineProfiles().filter((profile) => profile.engine_kind === engineKind);
  if (matches.length !== 1) {
    throw new Error(`Expected one deployed Agent profile for ${engineKind}; found ${matches.length}`);
  }
  return matches[0];
}

/**
 * Every configured engine profile, validated.
 *
 * Fails loudly rather than returning an empty list: a spec that silently drives
 * no engine is the failure this whole fixture exists to make impossible.
 */
export function engineProfiles(): EngineProfile[] {
  if (cached) return cached;
  const raw = (process.env[MATRIX_EVIDENCE_ENV] || '').trim();
  if (!raw || !path.isAbsolute(raw)) {
    throw new Error(`${MATRIX_EVIDENCE_ENV} must name an absolute evidence file`);
  }
  const stat = fs.lstatSync(raw, { throwIfNoEntry: false });
  if (!stat || !stat.isFile()) {
    throw new Error(`${MATRIX_EVIDENCE_ENV} is not a regular file: ${raw}`);
  }
  const evidence = JSON.parse(fs.readFileSync(raw, 'utf-8')) as Record<string, unknown>;
  if (evidence.version !== 1) {
    throw new Error('Agent E2E matrix evidence must use version 1');
  }
  if (evidence.state !== 'CONFIGURED') {
    throw new Error(`Agent E2E matrix evidence is not CONFIGURED: state=${String(evidence.state)}`);
  }
  const profiles = evidence.profiles;
  if (!Array.isArray(profiles) || !profiles.length) {
    throw new Error('Agent E2E matrix evidence has no profiles');
  }
  cached = profiles.map((item) => {
    const profile = item as EngineProfile;
    for (const key of ['agent_id', 'agent_name', 'engine_kind', 'environment_name', 'model']) {
      text(profile[key], key);
    }
    for (const key of ['contracts', 'decisions', 'modes', 'tools']) {
      if (!profile[key] || typeof profile[key] !== 'object' || Array.isArray(profile[key])) {
        throw new Error(`Agent E2E profile ${key} must be an object`);
      }
    }
    return profile;
  });
  return cached;
}

/**
 * The engines that declare `contract`, in matrix order.
 *
 * An engine that declares it false is not covered by that spec and is not a
 * hole either — it is the matrix saying the behaviour does not exist there.
 * A spec still has to say so out loud rather than simply not running.
 */
export function profilesSupporting(contract: string): EngineProfile[] {
  return engineProfiles().filter((profile) => profile.contracts?.[contract] === true);
}

/** The engines that declare `contract` false, so a spec can record what it skipped. */
export function profilesWithout(contract: string): EngineProfile[] {
  return engineProfiles().filter((profile) => profile.contracts?.[contract] !== true);
}

/** The engine's own name for a tool role, e.g. `write` → Codex's `commandExecution`. */
export function toolName(profile: EngineProfile, role: ToolRole): string {
  const names = profile.tools?.[role];
  if (!Array.isArray(names) || !names.length) {
    throw new Error(`engine ${profile.engine_kind} declares no tool for role ${role}`);
  }
  return text(names[0], `tools.${role}[0]`);
}

/** The engine's own name for a permission-mode role, e.g. `gated`. */
export function modeName(profile: EngineProfile, role: ModeRole): string {
  return text(profile.modes?.[role], `${profile.engine_kind} modes.${role}`);
}

/** The engine's own decision id for a role, carried back verbatim when answering. */
export function decisionId(profile: EngineProfile, role: DecisionRole): string {
  return text(profile.decisions?.[role], `${profile.engine_kind} decisions.${role}`);
}
