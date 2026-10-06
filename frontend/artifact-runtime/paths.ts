export function runtimeSegment(value: unknown): string {
  if (
    typeof value !== 'string' || !value || value === '.' || value === '..' ||
    /[/\\\x00]/.test(value) || /%(?:2e|2f|5c|00|25)/i.test(value)
  ) throw { code: 'invalid_argument', message: 'Invalid runtime path segment.' };
  return value;
}

export function runtimeDataPath(value: unknown, document: boolean): string {
  if (typeof value !== 'string')
    throw { code: 'invalid_argument', message: 'A data path must be a string.' };
  const segments = value.split('/').map(runtimeSegment);
  if ((segments.length % 2 === 0) !== document)
    throw { code: 'invalid_argument', message: 'Document paths have even segments; collections have odd segments.' };
  return segments.join('/');
}
