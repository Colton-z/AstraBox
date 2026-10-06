/** Database custody around the supplier's complete local attachment backend. */
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { LocalAttachmentStore, commitPreparedImageFile } from '@deepseek-ai/dsh-attachment-local';

const digest = (data) => createHash('sha256').update(data).digest('hex');
const isDigest = (value) => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
const recordKey = ({ kind, ref }) => `${kind}:${ref.attachmentId}:${kind === 'file' ? ref.name : ''}`;

/** Preserve normalized bytes rather than submitting them to admission again. */
export async function restoreRecords(store, payload) {
  const records = JSON.parse(payload.toString('utf8'));
  if (!Array.isArray(records)) throw new Error('Invalid DSH attachment archive');
  const restored = new Map();
  for (const record of records) {
    if (!record || !['image', 'file'].includes(record.kind) || !record.ref
      || typeof record.data !== 'string') throw new Error('Invalid DSH attachment record');
    const data = Buffer.from(record.data, 'base64');
    const ref = record.ref;
    if (data.toString('base64') !== record.data || ref.attachmentId !== `sha256:${digest(data)}`
      || ref.bytes !== data.length || restored.has(recordKey(record))) {
      throw new Error('DSH attachment archive integrity mismatch');
    }
    if (record.kind === 'image') {
      await commitPreparedImageFile(store.root, { data, ref });
      await LocalAttachmentStore.prototype.readImage.call(store, ref);
    } else {
      const actual = await LocalAttachmentStore.prototype.saveFile.call(store, { data, name: ref.name });
      if (actual.attachmentId !== ref.attachmentId || actual.bytes !== ref.bytes || actual.name !== ref.name) {
        throw new Error('DSH file attachment archive integrity mismatch');
      }
    }
    restored.set(recordKey(record), record);
  }
  return restored;
}

export default class DatabaseAttachmentStore extends LocalAttachmentStore {
  constructor(ctx, config) {
    super(ctx, config);
    this.targetPath = join(dirname(dirname(this.root)), 'astrabox-runtime-state.json');
    this.records = new Map();
    this.head = null;
    this.headDigest = null;
    this.initialized = false;
    this.queue = Promise.resolve();
    ctx.inject(['connection'], (connectionCtx) => {
      connectionCtx.connection.fetch.register({
        path: '/api/astrabox.attachments.restore', methods: ['POST'], requestBody: 'buffered',
        fetch: async () => {
          try {
            await this.initialize();
            return Response.json({ restored: true, snapshot_id: this.head });
          } catch {
            return Response.json({ error: 'DSH_ATTACHMENT_RESTORE_FAILED' }, { status: 500 });
          }
        },
      });
    });
  }

  async post(operation, fields) {
    let target;
    try {
      target = JSON.parse(await readFile(this.targetPath, 'utf8'));
    } catch {
      throw new Error('DSH attachment state target is unavailable');
    }
    const owner = target.owner;
    if (!owner || owner.subject_kind !== 'session'
      || ['user_id', 'subject_id', 'engine_kind'].some((key) => typeof owner[key] !== 'string' || !owner[key])
      || typeof target.base_url !== 'string' || !target.base_url
      || (this.owner && JSON.stringify(owner) !== this.owner)) {
      throw new Error('Invalid DSH attachment state owner');
    }
    let response;
    try {
      response = await fetch(`${target.base_url}/${operation}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ owner, ...fields }), signal: AbortSignal.timeout(30_000),
      });
    } catch {
      throw new Error(`DSH attachment state ${operation} transport failed`);
    }
    if (!response.ok) throw new Error(`DSH attachment state ${operation} failed: HTTP ${response.status}`);
    const envelope = await response.json();
    if (envelope?.code !== 'OK' || !envelope.data || typeof envelope.data !== 'object') {
      throw new Error('Invalid DSH attachment state response');
    }
    this.owner = JSON.stringify(owner);
    return envelope.data;
  }

  async initialize() {
    if (this.initialized) return;
    if (!this.initializing) {
      this.initializing = this.load().finally(() => { this.initializing = undefined; });
    }
    await this.initializing;
  }

  async load() {
    const snapshot = await this.post('load', {});
    if (snapshot.snapshot_id === null) {
      if (snapshot.payload_b64 !== null || snapshot.sha256 !== null || snapshot.size !== 0) {
        throw new Error('Invalid empty DSH attachment state');
      }
    } else {
      if (!isDigest(snapshot.snapshot_id) || !isDigest(snapshot.sha256) || typeof snapshot.payload_b64 !== 'string') {
        throw new Error('Invalid DSH attachment state metadata');
      }
      const payload = Buffer.from(snapshot.payload_b64, 'base64');
      if (payload.toString('base64') !== snapshot.payload_b64 || payload.length !== snapshot.size
        || digest(payload) !== snapshot.sha256) throw new Error('DSH attachment state integrity mismatch');
      this.records = await restoreRecords(this, payload);
      this.head = snapshot.snapshot_id;
      this.headDigest = snapshot.sha256;
    }
    this.initialized = true;
  }

  async commitPending() {
    const pending = this.pending;
    if (!pending) return;
    const result = await this.post('save', pending);
    if (!isDigest(result.snapshot_id)) throw new Error('Invalid DSH attachment snapshot identity');
    this.head = result.snapshot_id;
    this.headDigest = pending.sha256;
    this.pending = undefined;
  }

  persist(records) {
    const operation = this.queue.then(async () => {
      // An ambiguous acknowledgement must settle against the same immutable
      // bytes before another write can use that snapshot as its predecessor.
      await this.commitPending();
      for (const record of records) this.records.set(recordKey(record), record);
      const sorted = [...this.records.entries()].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0);
      const payload = Buffer.from(JSON.stringify(sorted.map(([, record]) => record)));
      const sha256 = digest(payload);
      if (sha256 === this.headDigest) return;
      this.pending = { payload_b64: payload.toString('base64'), sha256, expected_snapshot_id: this.head };
      await this.commitPending();
    });
    this.queue = operation.catch(() => {});
    return operation;
  }

  async imageRecord(ref) {
    const { data } = await super.readImage(ref);
    return { kind: 'image', ref, data: Buffer.from(data).toString('base64') };
  }

  async fileRecord(ref) {
    const chunks = [];
    for await (const chunk of super.readFileStream(ref)) chunks.push(Buffer.from(chunk));
    return { kind: 'file', ref, data: Buffer.concat(chunks).toString('base64') };
  }

  async saveImages(inputs) {
    await this.initialize();
    const refs = await super.saveImages(inputs);
    await this.persist(await Promise.all(refs.map((ref) => this.imageRecord(ref))));
    return refs;
  }

  async saveImage(input) {
    await this.initialize();
    const ref = await super.saveImage(input);
    await this.persist([await this.imageRecord(ref)]);
    return ref;
  }

  async saveFile(input) {
    await this.initialize();
    const ref = await super.saveFile(input);
    await this.persist([await this.fileRecord(ref)]);
    return ref;
  }

  async saveFileStream(input) {
    await this.initialize();
    const ref = await super.saveFileStream(input);
    await this.persist([await this.fileRecord(ref)]);
    return ref;
  }
}
