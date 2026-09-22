/** Real in-box probes for Claude's spooled SessionStore path. */
import { sandboxExec, type SandboxHandle } from './sandboxOps';

export interface TranscriptScopeKey {
  project_key: string;
  session_id: string;
  subpath?: string;
}

export interface LargeTranscriptBatch {
  appendId: string;
  batchPath: string;
  batchBytes: number;
  entryPayloadBytes: number;
  entryPayloadSha256: string;
}

/**
 * Put one oversized SessionStore entry into the live runner's durable spool.
 *
 * The platform now hands Claude its Session-scoped store capability over the
 * typed runner activation protocol. It deliberately does not copy that secret
 * into a process environment. Writing the same fsync-backed queue format that
 * `SpoolSessionStore.append()` writes lets the resident runner perform the real
 * authenticated HTTP flush after its next SDK append wakes the flusher; the
 * capability never leaves the runner process.
 */
export function enqueueLargeTranscriptEntry(
  sandbox: SandboxHandle,
  key: TranscriptScopeKey,
  marker: string,
  payloadBytes = 17_000_000,
): LargeTranscriptBatch {
  if (!key.project_key.trim() || !key.session_id.trim()) {
    throw new Error('large transcript probe requires one concrete SessionStore scope');
  }
  const script = [
    "runuser -u \"$ASTRABOX_WORKLOAD_USER\" -- python3 - <<'PY'",
    'import hashlib',
    'import json',
    'import os',
    'import time',
    'from pathlib import Path',
    `key = ${JSON.stringify(key)}`,
    `marker = ${JSON.stringify(marker)}`,
    `payload_bytes = ${payloadBytes}`,
    "spool = Path('/tmp/astrabox-runner-spool')",
    "if not spool.is_dir() or spool.is_symlink(): raise RuntimeError('Claude runner spool is unavailable')",
    'deadline = time.monotonic() + 60',
    "while list(spool.glob('*.batch.json')) and time.monotonic() < deadline:",
    '    time.sleep(0.1)',
    "pending = sorted(path.name for path in spool.glob('*.batch.json'))",
    "if pending: raise RuntimeError(f'Claude runner spool did not drain before injection: {pending}')",
    "payload_text = 'x' * payload_bytes",
    "entry = {'type': 'e2e_large_entry', 'marker': marker, 'payload': payload_text}",
    "entry['toolUseResult'] = {'durationSeconds': 1.8768265600000014, 'query': 'duration float fidelity', 'results': [], 'searchCount': 0}",
    "append_id = 'append-' + marker",
    "batch = {'key': key, 'entries': [entry], 'append_id': append_id}",
    "raw = json.dumps(batch, ensure_ascii=False, separators=(',', ':')).encode('utf-8')",
    "target = spool / ('000000000000-e2e-' + hashlib.sha256(marker.encode()).hexdigest()[:12] + '.batch.json')",
    "temporary = target.with_name(target.name + '.tmp')",
    "if target.exists() or temporary.exists(): raise RuntimeError(f'large transcript probe path already exists: {target}')",
    "with temporary.open('xb') as stream:",
    '    stream.write(raw)',
    '    stream.flush()',
    '    os.fsync(stream.fileno())',
    'os.chmod(temporary, 0o600)',
    'temporary.replace(target)',
    'directory = os.open(spool, os.O_RDONLY)',
    'try:',
    '    os.fsync(directory)',
    'finally:',
    '    os.close(directory)',
    'print(json.dumps({',
    "    'appendId': append_id,",
    "    'batchPath': str(target),",
    "    'batchBytes': len(raw),",
    "    'entryPayloadBytes': len(payload_text.encode('utf-8')),",
    "    'entryPayloadSha256': hashlib.sha256(payload_text.encode('utf-8')).hexdigest(),",
    "}, separators=(',', ':')))",
    'PY',
  ].join('\n');
  const raw = sandboxExec(sandbox, script, 90_000).trim();
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw new Error(
      `large transcript probe returned invalid JSON ${JSON.stringify(raw.slice(0, 500))}: `
        + (error as Error).message,
    );
  }
  if (!parsed || typeof parsed !== 'object') {
    throw new Error('large transcript probe did not return an object');
  }
  const evidence = parsed as Record<string, unknown>;
  const result: LargeTranscriptBatch = {
    appendId: String(evidence.appendId || '').trim(),
    batchPath: String(evidence.batchPath || '').trim(),
    batchBytes: Number(evidence.batchBytes),
    entryPayloadBytes: Number(evidence.entryPayloadBytes),
    entryPayloadSha256: String(evidence.entryPayloadSha256 || '').trim(),
  };
  if (
    !result.appendId
    || !/^\/tmp\/astrabox-runner-spool\/[a-z0-9-]+\.batch\.json$/.test(result.batchPath)
    || !Number.isInteger(result.batchBytes)
    || !Number.isInteger(result.entryPayloadBytes)
    || !/^[a-f0-9]{64}$/.test(result.entryPayloadSha256)
  ) {
    throw new Error(`large transcript probe returned invalid evidence ${JSON.stringify(evidence)}`);
  }
  return result;
}
