// The console reads the path it is published under from the meta element the
// AstraBox server writes into index.html (MODULE_BASE in src/api.ts) and stops
// loading without it. Component tests render without that page, so each jsdom
// document gets the element a page served at the root of its host carries.
if (typeof document !== 'undefined') {
  const meta = document.createElement('meta');
  meta.name = 'astrabox-base-path';
  meta.content = '';
  document.head.append(meta);
}

export {};
