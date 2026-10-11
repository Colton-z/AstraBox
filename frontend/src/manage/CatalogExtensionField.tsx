import type { ReactNode } from 'react';
import { ArrowUpRight, BookOpenText, PlugZap } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { FieldLegend, FieldSet } from '@/components/ui/field';
import type { AgentExtensionCatalog, ExtensionProviderView } from '@/types';
import { ConsoleFieldRow, ConsoleSearchMultiSelect } from './console';

/** Whether two selections hold the same catalog ids, in any order. */
export const sameIds = (left: string[], right: string[]) =>
  left.length === right.length && left.every((value) => right.includes(value));

/**
 * The deployment catalog's MCP servers or Skills, picked for one Agent or
 * Assistant, above the record's own field of the same kind.
 *
 * Everything that names the catalog comes from `provider`, the declaration the
 * selected extension provider makes: its display name, and whether it has a
 * management console. `manage` opens that console; the button renders only
 * when the provider declares one and the page can scope a console session to
 * the record (Agents). A deployment that runs no catalog renders the record's
 * own field without this wrapper.
 */
export function CatalogExtensionField({
  kind,
  id,
  provider,
  catalog,
  value,
  onChange,
  loading,
  disabled,
  manage,
  children,
}: {
  kind: 'mcp' | 'skills';
  id: string;
  provider: ExtensionProviderView;
  catalog: Pick<AgentExtensionCatalog, 'mcp_servers' | 'skills'> | null;
  value: string[];
  onChange: (ids: string[]) => void;
  loading: boolean;
  disabled: boolean;
  manage?: { opening: boolean; busy: boolean; onOpen: () => void };
  children: ReactNode;
}) {
  const { t } = useTranslation();
  const isMcp = kind === 'mcp';
  const named = { catalog: provider.display_name };
  return (
    <FieldSet className="gap-4 rounded-lg border p-4">
      <FieldLegend variant="label" className="px-1">
        {t(isMcp ? 'manage:agents.mcp_configuration' : 'manage:agents.skill_configuration')}
      </FieldLegend>
      <ConsoleFieldRow
        label={t(isMcp ? 'manage:agents.mcp_field_label' : 'manage:agents.skills_field_label', named)}
        htmlFor={id}
        help={t(isMcp ? 'manage:agents.mcp_field_help' : 'manage:agents.skills_field_help')}
      >
        <div className="flex flex-col gap-2 sm:flex-row">
          <div className="min-w-0 flex-1">
            <ConsoleSearchMultiSelect
              id={id}
              value={value}
              onChange={onChange}
              options={(isMcp ? catalog?.mcp_servers ?? [] : catalog?.skills ?? [])
                .map((item) => ({ value: item.id, label: item.name }))}
              placeholder={loading
                ? t(isMcp ? 'manage:agents.loading_mcp_catalog' : 'manage:agents.loading_skills_catalog', named)
                : t(isMcp ? 'manage:agents.select_mcp_placeholder' : 'manage:agents.select_skills_placeholder')}
              searchPlaceholder={t(isMcp ? 'manage:agents.search_mcp_placeholder' : 'manage:agents.search_skills_placeholder')}
              emptyLabel={t(isMcp ? 'manage:agents.no_mcp_servers' : 'manage:agents.no_skills')}
              disabled={loading || disabled}
            />
          </div>
          {manage && provider.management_console && (
            <Button
              type="button"
              variant="outline"
              disabled={manage.busy}
              onClick={manage.onOpen}
              className="shrink-0 gap-2"
            >
              {isMcp ? <PlugZap className="size-4" aria-hidden /> : <BookOpenText className="size-4" aria-hidden />}
              {manage.opening
                ? t('manage:agents.opening_extensions')
                : t(isMcp ? 'manage:agents.manage_mcp_catalog' : 'manage:agents.manage_skills_catalog', named)}
              <ArrowUpRight className="size-3.5" aria-hidden />
            </Button>
          )}
        </div>
      </ConsoleFieldRow>
      {children}
    </FieldSet>
  );
}
