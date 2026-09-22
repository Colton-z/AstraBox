import React from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown } from 'lucide-react';
import type { PermissionMode } from '../../types';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from '../../components/ui/dropdown-menu';
import { PERMISSION_MODE_LABELS } from '../../utils/format';

// Permission-mode tones in the design language: default reads neutral, plan =
// astra (the "think first" mode), acceptEdits = mint (it lets edits through),
// bypass = plasma (the warm "danger / human-bypass" accent). Tone classes live
// in styles.css (.permmode-*) to keep the color-mix borders out of arbitrary TW.
const PERMISSION_TONE: Record<string, string> = {
  default: 'permmode-default',
  plan: 'permmode-plan',
  acceptEdits: 'permmode-accept',
  bypassPermissions: 'permmode-bypass',
  dontAsk: 'permmode-deny',
  auto: 'permmode-auto',
};

export function PermissionModeStripInline({
  permissionMode,
  permissionModes,
  canChange,
  modeSwitching,
  onSelect,
}: {
  permissionMode: PermissionMode;
  permissionModes: readonly PermissionMode[];
  canChange: boolean;
  modeSwitching: boolean;
  onSelect: (mode: PermissionMode) => Promise<void>;
}) {
  const { t } = useTranslation();
  const label = (mode: PermissionMode) => {
    const known = PERMISSION_MODE_LABELS[mode];
    return known ? t(known) : mode;
  };
  // The whole roster, not the one step ⇧Tab would take: a mode a reader
  // cannot see is a mode they do not know they have, and engines declare as
  // many as five. ⇧Tab still cycles for anyone who knows it.
  const trigger = (
    <span
      data-testid="permission-mode-badge"
      data-permission-mode={permissionMode}
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-medium ${PERMISSION_TONE[permissionMode] ?? 'permmode-engine'}`}
    >
      {modeSwitching ? t('chat:permission_mode.switching') : label(permissionMode)}
      {canChange ? <ChevronDown className="size-3 opacity-60" aria-hidden /> : null}
    </span>
  );
  if (!canChange || modeSwitching) {
    return <div className="flex items-center gap-1.5 text-11">{trigger}</div>;
  }
  return (
    <div className="flex items-center gap-1.5 text-11">
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <button
              type="button"
              aria-label={t('chat:permission_mode.choose')}
              className="rounded-full transition-opacity duration-150 hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          }
        >
          {trigger}
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="min-w-44">
          <DropdownMenuRadioGroup
            value={permissionMode}
            onValueChange={(next) => void onSelect(String(next))}
          >
            {permissionModes.map((mode) => (
              <DropdownMenuRadioItem key={mode} value={mode} data-permission-option={mode}>
                {label(mode)}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
