import { useCallback, useEffect, useRef, useState } from 'react';
import type { PermissionMode } from '../../types';
import { getNextPermissionMode } from '../../utils/format';
import { setSessionPermissionMode } from '../../api';

// Permission-mode state and the shift-tab cycle action. `permissionModeRef`
// mirrors the state because the chat hook reads the current mode from stream
// callbacks that would otherwise close over stale state.
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
  const permissionModeRef = useRef<string | null>(initial || null);
  useEffect(() => {
    const next = initialPermissionMode && availableModes.includes(initialPermissionMode)
      ? initialPermissionMode
      : (availableModes[0] ?? '');
    setPermissionMode(next);
    permissionModeRef.current = next || null;
  }, [availableModes, initialPermissionMode]);
  useEffect(() => { permissionModeRef.current = permissionMode || null; }, [permissionMode]);
  const [modeSwitching, setModeSwitching] = useState(false);
  const canChangePermissionMode = availableModes.length > 1
    && (lifecycleState === 'ready' || lifecycleState === 'background')
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
    permissionModeRef,
    modeSwitching,
    canChangePermissionMode,
    selectPermissionMode,
    cyclePermissionMode,
    hasPermissionModes: availableModes.length > 0,
  };
}
