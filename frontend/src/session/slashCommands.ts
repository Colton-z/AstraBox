import type { SlashCommandDetail } from '../types';

export interface DisplaySlashCommand {
  name: string;
  description: string;
}

function cleanCommandName(value: unknown): string {
  const text = String(value ?? '').trim().replace(/^\/+/, '');
  return text ? `/${text}` : '';
}

function cleanDescription(value: unknown): string {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}

function readDetail(item: unknown): { name: unknown; description: string; aliases: unknown[] } {
  if (!item || typeof item !== 'object') {
    return { name: item, description: '', aliases: [] };
  }
  const record = item as SlashCommandDetail;
  return {
    name: record.name ?? record.command,
    description: cleanDescription(record.description),
    aliases: Array.isArray(record.aliases) ? record.aliases : [],
  };
}

export function normalizeSessionSlashCommands(
  slashCommandDetails: unknown,
): DisplaySlashCommand[] {
  const byKey = new Map<string, DisplaySlashCommand>();
  const order: string[] = [];

  function addCommand(rawName: unknown, description: string): void {
    const name = cleanCommandName(rawName);
    if (!name) return;
    const key = name.toLowerCase();
    const existing = byKey.get(key);
    if (!existing) {
      byKey.set(key, { name, description });
      order.push(key);
      return;
    }
    if (description && !existing.description) {
      existing.description = description;
    }
  }

  function consume(value: unknown): void {
    if (!Array.isArray(value)) return;
    for (const item of value) {
      const detail = readDetail(item);
      addCommand(detail.name, detail.description);
      for (const alias of detail.aliases) {
        addCommand(alias, detail.description);
      }
    }
  }

  consume(slashCommandDetails);

  return order.map((key) => byKey.get(key)).filter((item): item is DisplaySlashCommand => Boolean(item));
}
