// Write Brotli and gzip copies of the built console's text files next to them
// (`main-<hash>.js` → `main-<hash>.js.br`, `main-<hash>.js.gz`).
//
// The AstraBox server answers a request for a built file with the copy the
// browser accepts (astrabox/api/frontend_cache.py `precompressed_variant`), so a
// deployment without a compressing proxy in front still sends the console
// compressed, and the server spends no CPU compressing per request. The shell
// (`index.html`) is rendered per request and is not compressed here.
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { brotliCompressSync, constants, gzipSync } from 'node:zlib';

const dist = resolve(import.meta.dirname, 'dist');
const COMPRESSIBLE = /\.(?:js|mjs|css|svg|json|wasm|map|txt)$/;
// Below this a compressed copy saves less than a packet.
const MIN_BYTES = 1024;

function* files(directory) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) yield* files(path);
    else if (entry.isFile()) yield path;
  }
}

let written = 0;
for (const path of files(dist)) {
  if (!COMPRESSIBLE.test(path) || statSync(path).size < MIN_BYTES) continue;
  const source = readFileSync(path);
  const brotli = brotliCompressSync(source, {
    params: {
      [constants.BROTLI_PARAM_QUALITY]: constants.BROTLI_MAX_QUALITY,
      [constants.BROTLI_PARAM_MODE]: constants.BROTLI_MODE_TEXT,
      [constants.BROTLI_PARAM_SIZE_HINT]: source.length,
    },
  });
  const gzip = gzipSync(source, { level: constants.Z_BEST_COMPRESSION });
  for (const [suffix, body] of [['.br', brotli], ['.gz', gzip]]) {
    if (body.length >= source.length) continue;
    writeFileSync(path + suffix, body);
    written += 1;
  }
}
console.log(`precompress: wrote ${written} compressed copies under ${dist}`);
