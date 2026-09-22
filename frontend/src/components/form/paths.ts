// Dotted-path get/set helpers for schema-driven form fields.
//
// Schema fields like display_name carry `path: "display_meta.display_name"`.
// The form shows them flat but reads/writes the nested location on the draft,
// so display_meta stays the canonical storage shape (see
// astrabox/core/service/orchestrator/agent_schema.py).
//
// setByPath always returns a new object and never mutates the input: the draft
// is React state, and mutating it in place would not re-render.

export function getByPath(obj: Record<string, unknown> | undefined | null, path: string): unknown {
  if (!obj) return undefined;
  let node: unknown = obj;
  for (const part of path.split('.')) {
    if (!node || typeof node !== 'object') return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return node;
}

export function setByPath(
  obj: Record<string, unknown>,
  path: string,
  value: unknown,
): Record<string, unknown> {
  const parts = path.split('.');
  const root: Record<string, unknown> = { ...obj };
  let node = root;
  for (let i = 0; i < parts.length - 1; i += 1) {
    const part = parts[i];
    const existing = node[part];
    const cloned: Record<string, unknown> =
      existing && typeof existing === 'object' && !Array.isArray(existing)
        ? { ...(existing as Record<string, unknown>) }
        : {};
    node[part] = cloned;
    node = cloned;
  }
  node[parts[parts.length - 1]] = value;
  return root;
}

// Remove a (possibly nested) key, pruning empty parent objects created solely
// for it, so clearing a flat field does not leave `display_meta: {}` debris.
// Returns a new object.
export function deleteByPath(
  obj: Record<string, unknown>,
  path: string,
): Record<string, unknown> {
  const parts = path.split('.');
  if (parts.length === 1) {
    const next = { ...obj };
    delete next[parts[0]];
    return next;
  }
  const [head, ...rest] = parts;
  const child = obj[head];
  if (!child || typeof child !== 'object' || Array.isArray(child)) {
    return obj;
  }
  const prunedChild = deleteByPath(child as Record<string, unknown>, rest.join('.'));
  const next = { ...obj };
  if (Object.keys(prunedChild).length === 0) {
    delete next[head];
  } else {
    next[head] = prunedChild;
  }
  return next;
}
