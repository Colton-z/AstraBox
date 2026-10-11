import { useTranslation } from 'react-i18next';

import { ErrorNote } from '@/components/shell';
import { ConsoleDangerButton } from './console';

/** One record's selection from extension catalogs, as the record stores it. */
export interface CatalogSelection {
  /** `{provider, item_id}` assignment rows. */
  mcpServers: Array<{ provider: string; itemId: string }>;
  /** The Git sources the selected catalog Skills compiled to, else their ids. */
  skills: string[];
}

function strings(raw: unknown): string[] {
  return Array.isArray(raw)
    ? raw.map((value) => String(value ?? '').trim()).filter((value) => value !== '')
    : [];
}

/**
 * Read `mcp_assignments` and `extension_catalog` off an Agent, an Assistant or
 * the Assistant defaults, skipping malformed rows the way the server's
 * `catalog_selection` does.
 */
export function catalogSelectionOf(record: Record<string, unknown> | null | undefined): CatalogSelection {
  const rows = Array.isArray(record?.mcp_assignments) ? record.mcp_assignments : [];
  const mcpServers = rows.flatMap((row) => {
    if (!row || typeof row !== 'object') return [];
    const provider = String((row as Record<string, unknown>).provider ?? '').trim();
    const itemId = String((row as Record<string, unknown>).item_id ?? '').trim();
    return provider && itemId ? [{ provider, itemId }] : [];
  });
  const snapshot = record?.extension_catalog;
  const stored = snapshot && typeof snapshot === 'object' ? (snapshot as Record<string, unknown>) : {};
  const sources = strings(stored.skills);
  return { mcpServers, skills: sources.length > 0 ? sources : strings(stored.skill_ids) };
}

export function hasCatalogSelection(selection: CatalogSelection): boolean {
  return selection.mcpServers.length > 0 || selection.skills.length > 0;
}

/**
 * A catalog selection the deployment cannot use, and the way to drop it.
 *
 * Shown only when the deployment runs no extension catalog and the record still
 * holds a selection made while one ran: new conversations refuse that record
 * (`EXTENSION_CATALOG_DISABLED`) until the selection is removed, so it is listed
 * rather than hidden. Removing it saves an empty selection, which the server
 * accepts only in that state. The provider name and the entry are machine
 * identities: the provider is what `ASTRABOX_EXTENSION_PROVIDER` would name.
 */
export function UnavailableCatalogSelection({
  selection,
  defaults = false,
  removing,
  onRemove,
}: {
  selection: CatalogSelection;
  /** The Assistant defaults, whose selection every Assistant inherits. */
  defaults?: boolean;
  removing: boolean;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  return (
    <ErrorNote title={t('manage:extension_catalog.unavailable_title')}>
      <div className="flex flex-col gap-3" data-testid="unavailable-catalog-selection">
        <p>
          {t(defaults
            ? 'manage:extension_catalog.unavailable_defaults'
            : 'manage:extension_catalog.unavailable_record')}
        </p>
        <ul className="flex flex-col gap-1">
          {selection.mcpServers.map(({ provider, itemId }) => (
            <li key={`mcp:${provider}:${itemId}`} className="flex flex-wrap items-baseline gap-x-2">
              <span>{t('manage:extension_catalog.kind_mcp')}</span>
              <span className="font-mono break-all">{`${provider}: ${itemId}`}</span>
            </li>
          ))}
          {selection.skills.map((skill) => (
            <li key={`skill:${skill}`} className="flex flex-wrap items-baseline gap-x-2">
              <span>{t('manage:extension_catalog.kind_skill')}</span>
              <span className="font-mono break-all">{skill}</span>
            </li>
          ))}
        </ul>
        <div>
          <ConsoleDangerButton
            disabled={removing}
            confirm={{
              title: t('manage:extension_catalog.remove_confirm_title'),
              description: t('manage:extension_catalog.remove_confirm_description'),
              action: t('manage:extension_catalog.remove'),
              onConfirm: onRemove,
            }}
          >
            {removing ? t('manage:extension_catalog.removing') : t('manage:extension_catalog.remove')}
          </ConsoleDangerButton>
        </div>
      </div>
    </ErrorNote>
  );
}
