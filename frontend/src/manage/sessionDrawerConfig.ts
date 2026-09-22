// Build the read-only session drawer's view sections from a session detail (+ an
// optional loaded trace). Sessions are operational records — you inspect a run, you
// don't edit it — so this only produces DrawerSectionSpec[] (no edit config). The
// section rhythm mirrors the environment drawer: eyebrow-labelled groups of mono
// label→value rows, hairline-divided. Trace facts fold in once the trace resolves.
import i18n from '@/i18n';
import type {
  AdminSessionDetail,
  AdminSessionSummary,
  AdminSessionTrace,
} from '@/types';

import { formatDateTimeSeconds } from './console/format';
import { formatDuration, sessionUserDisplay } from './sessionConfig';

function joinList(items?: string[]): string {
  const xs = (items || []).filter(Boolean);
  return xs.length ? xs.join(' · ') : '';
}

/**
 * Drawer sections for a session. `detail` is the full record; `trace` (when
 * loaded) adds the turn/transcript summary. `summary` is the cheap list row
 * already on hand, used as a fallback so the drawer renders identity facts the
 * instant it opens, before `detail` resolves.
 */
export function buildSessionSections(
  summary: AdminSessionSummary,
  detail: AdminSessionDetail | null,
  trace: AdminSessionTrace | null,
): import('./console').DrawerSectionSpec[] {
  const d = detail ?? summary;
  const skills = detail ? joinList(detail.template_skills) : '';
  const mcp = detail ? joinList(detail.template_mcp_servers) : '';

  const sections: import('./console').DrawerSectionSpec[] = [
    {
      label: i18n.t('manage:sessions.section_overview'),
      fields: [
        { label: i18n.t('manage:common_fields.user'), value: sessionUserDisplay(d), mono: false },
        { label: i18n.t('manage:common_fields.agent'), value: d.agent_id || '—' },
        { label: i18n.t('common:sandbox'), value: d.sandbox_id || '—' },
        { label: i18n.t('manage:sessions.field_session_id'), value: d.session_id },
      ],
    },
    {
      label: i18n.t('manage:sessions.section_timeline'),
      fields: [
        { label: i18n.t('manage:sessions.field_created'), value: formatDateTimeSeconds(d.created_at) },
        { label: i18n.t('manage:sessions.field_updated'), value: formatDateTimeSeconds(d.updated_at) },
        { label: i18n.t('manage:sessions.field_expires'), value: formatDateTimeSeconds(d.expires_at) },
        { label: i18n.t('manage:common_fields.duration'), value: formatDuration(d.duration_seconds) },
      ],
    },
  ];

  // Runtime facts only exist on the full detail (local-runtime flag).
  if (detail) {
    sections.push({
      label: i18n.t('manage:sessions.section_runtime'),
      fields: [
        { label: i18n.t('manage:common_fields.local_runtime'), value: detail.has_local_runtime ? i18n.t('common:yes') : i18n.t('common:no') },
        ...(skills ? [{ label: i18n.t('manage:common_fields.skills'), value: skills, mono: false }] : []),
        ...(mcp ? [{ label: i18n.t('manage:common_fields.mcp_servers'), value: mcp, mono: false }] : []),
      ],
    });
  }

  // Trace summary — a compact digest of the loaded trace (counts + busy/current
  // turn), not the full two-pane transcript. Inspecting individual frames stays a
  // deeper drill the operator opens deliberately.
  if (trace) {
    const turns = trace.turns?.length ?? 0;
    const messages = trace.messages?.length ?? 0;
    const frames = trace.frames?.length ?? 0;
    sections.push({
      label: i18n.t('manage:sessions.trace_section'),
      fields: [
        { label: i18n.t('manage:sessions.field_current_turn'), value: trace.current_turn_id || '—' },
        { label: i18n.t('manage:sessions.field_turn_count'), value: String(turns) },
        { label: i18n.t('manage:sessions.field_events'), value: i18n.t('manage:sessions.events_value', { messages, frames }) },
      ],
    });
  }

  return sections;
}
