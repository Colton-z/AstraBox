import { useCallback, useEffect, useState } from 'react';
import type { PermissionMode } from '../../types';
import { getNextPermissionMode } from '../../utils/format';
import { setSessionPermissionMode } from '../../api';

// Permission-mode state and the shift-tab cycle action. The mode is the
// session's: it changes through its own route, and no message carries one.
export function usePermissionModeControl({
  sessionId,
  initialPermissionMode,
  availableModes,
  lifecycleState,
}: {
  sessionId: string;
  initialPermissionMode: PermissionMode | null | undefined;
  availableModes: readonly PermissionMode[];
  lifecycleState: string;
}) {
  const initial = initialPermissionMode && availableModes.includes(initialPermissionMode)
    ? initialPermissionMode
    : (availableModes[0] ?? '');
  const [permissionMode, setPermissionMode] = useState<PermissionMode>(initial);
  useEffect(() => {
    const next = initialPermissionMode && availableModes.includes(initialPermissionMode)
      ? initialPermissionMode
      : (availableModes[0] ?? '');
    setPermissionMode(next);
  }, [availableModes, initialPermissionMode]);
  const [modeSwitching, setModeSwitching] = useState(false);
  const canChangePermissionMode = availableModes.length > 1
    && lifecycleState === 'ready'
    && !modeSwitching;
  // Picking from the list and cycling with shift-tab are the same action: one
  // named mode is asked for, and the local state moves only once the backend
  // has taken it. A mode already held is not re-sent — the engines that accept
  // one append a transcript entry for it, and a no-op should not be visible.
  const selectPermissionMode = useCallback(async (next: PermissionMode) => {
    if (!canChangePermissionMode) return;
    if (!next || next === permissionMode || !availableModes.includes(next)) return;
    setModeSwitching(true);
    try { await setSessionPermissionMode(sessionId, next); setPermissionMode(next); } finally { setModeSwitching(false); }
  }, [sessionId, permissionMode, availableModes, canChangePermissionMode]);
  const cyclePermissionMode = useCallback(
    async () => selectPermissionMode(getNextPermissionMode(permissionMode, availableModes)),
    [selectPermissionMode, permissionMode, availableModes],
  );

  return {
    permissionMode,
    modeSwitching,
    canChangePermissionMode,
    selectPermissionMode,
    cyclePermissionMode,
    hasPermissionModes: availableModes.length > 0,
  };
}
