import type { ReactNode } from 'react';
import { ArrowUpRight, BookOpenText, PlugZap } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { FieldLegend, FieldSet } from '@/components/ui/field';
import type { AgentExtensionCatalog } from '@/types';
import { ConsoleFieldRow, ConsoleSearchMultiSelect } from './console';

/** Whether two selections hold the same catalog ids, in any order. */
export const sameIds = (left: string[], right: string[]) =>
  left.length === right.length && left.every((value) => right.includes(value));

/**
 * The deployment catalog's MCP servers or Skills, picked for one Agent or
 * Assistant, above the record's own field of the same kind.
 *
 * `manage` opens the catalog's own management console; it is offered where a
 * console session can be scoped to the record (Agents).
 */
export function CatalogExtensionField({
  kind,
  id,
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
  return (
    <FieldSet className="gap-4 rounded-lg border p-4">
      <FieldLegend variant="label" className="px-1">
        {t(isMcp ? 'manage:agents.mcp_configuration' : 'manage:agents.skill_configuration')}
      </FieldLegend>
      <ConsoleFieldRow
        label={t(isMcp ? 'manage:agents.mcp_field_label' : 'manage:agents.skills_field_label')}
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
              placeholder={t(loading ? 'manage:agents.loading_extensions'
                : isMcp ? 'manage:agents.select_mcp_placeholder' : 'manage:agents.select_skills_placeholder')}
              searchPlaceholder={t(isMcp ? 'manage:agents.search_mcp_placeholder' : 'manage:agents.search_skills_placeholder')}
              emptyLabel={t(isMcp ? 'manage:agents.no_mcp_servers' : 'manage:agents.no_skills')}
              disabled={loading || disabled}
            />
          </div>
          {manage && (
            <Button
              type="button"
              variant="outline"
              disabled={manage.busy}
              onClick={manage.onOpen}
              className="shrink-0 gap-2"
            >
              {isMcp ? <PlugZap className="size-4" aria-hidden /> : <BookOpenText className="size-4" aria-hidden />}
              {t(manage.opening ? 'manage:agents.opening_extensions'
                : isMcp ? 'manage:agents.manage_mcp_catalog' : 'manage:agents.manage_skills_catalog')}
              <ArrowUpRight className="size-3.5" aria-hidden />
            </Button>
          )}
        </div>
      </ConsoleFieldRow>
      {children}
    </FieldSet>
  );
}
