import React, { useState, useRef, useCallback, useEffect, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import Ansi from 'ansi-to-react';
import { runTerminalCommandStream, interruptSession } from '../api';
import type { TerminalEvent } from '../api';
import {
  Terminal,
  TerminalActions,
  TerminalContent,
  TerminalCopyButton,
  TerminalHeader,
} from '@/components/ai-elements/terminal';
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from '@/components/ui/input-group';

interface TerminalLine {
  type: 'cmd' | 'stdout' | 'stderr' | 'exit' | 'info';
  text: string;
}

const lineClasses: Record<TerminalLine['type'], string> = {
  stdout: 'text-foreground',
  stderr: 'text-destructive',
  cmd: 'text-primary',
  exit: 'text-muted-foreground italic',
  info: 'text-muted-foreground italic',
};

function normalizeCwd(value?: string | null): string {
  const cwd = String(value ?? '').trim();
  return cwd.startsWith('/') ? cwd : '';
}

export default function TerminalPanel({
  sessionId,
  enabled,
  initialCwd,
  onCwdChange,
}: {
  sessionId: string;
  enabled: boolean;
  initialCwd?: string | null;
  onCwdChange?: (cwd: string) => void;
}) {
  const { t } = useTranslation();
  const [lines, setLines] = useState<TerminalLine[]>([]);
  const [input, setInput] = useState('');
  const [cwd, setCwd] = useState(() => normalizeCwd(initialCwd));
  const [status, setStatus] = useState<'ready' | 'running' | 'stopping' | 'error'>('ready');
  const activeControllerRef = useRef<AbortController | null>(null);
  const cwdRef = useRef(cwd);

  const replaceCurrentCwd = useCallback((value?: string | null) => {
    const next = normalizeCwd(value);
    cwdRef.current = next;
    setCwd(next);
    if (next) onCwdChange?.(next);
  }, [onCwdChange]);

  const setCurrentCwd = useCallback((value?: string | null) => {
    const next = normalizeCwd(value);
    if (!next) return;
    replaceCurrentCwd(next);
  }, [replaceCurrentCwd]);

  const handleTerminalEvent = useCallback((event: TerminalEvent) => {
    if (event.type === 'ack') {
      setCurrentCwd(event.working_directory);
    } else if (event.type === 'cwd') {
      setCurrentCwd(event.path);
    } else if (event.type === 'stdout') {
      setLines((prev) => [...prev, { type: 'stdout', text: event.text ?? '' }]);
    } else if (event.type === 'stderr') {
      setLines((prev) => [...prev, { type: 'stderr', text: event.text ?? '' }]);
    } else if (event.type === 'exit') {
      setLines((prev) => [...prev, { type: 'exit', text: t('misc:terminal.exit_code', { code: event.exit_code ?? '?' }) }]);
    }
  }, [setCurrentCwd, t]);

  const runCommand = useCallback(
    async (command: string) => {
      const cmd = command.trim();
      if (!cmd) return;
      if (!enabled) return;
      if (status === 'running' || status === 'stopping') return;

      setLines((prev) => [...prev, { type: 'cmd', text: `$ ${cmd}` }]);
      setStatus('running');

      const controller = new AbortController();
      activeControllerRef.current?.abort();
      activeControllerRef.current = controller;

      let failed = false;
      try {
        await runTerminalCommandStream(
          sessionId,
          cmd,
          handleTerminalEvent,
          controller.signal,
          cwdRef.current || undefined,
        );
      } catch (err) {
        const e = err as Error & { name?: string };
        if (e?.name !== 'AbortError') {
          failed = true;
          setLines((prev) => [...prev, { type: 'info', text: e.message }]);
        }
      } finally {
        if (activeControllerRef.current === controller) {
          activeControllerRef.current = null;
        }
        setStatus(failed ? 'error' : 'ready');
      }
    },
    [enabled, handleTerminalEvent, sessionId, status]
  );

  const stopCommand = useCallback(async () => {
    if (status !== 'running') return;
    const active = activeControllerRef.current;
    if (!active) return;

    setStatus('stopping');
    active.abort();

    try {
      await interruptSession(sessionId);
      setLines((prev) => [...prev, { type: 'info', text: t('misc:terminal.interrupted') }]);
    } catch (err) {
      setLines((prev) => [...prev, { type: 'info', text: (err as Error).message }]);
      setStatus('error');
      return;
    }

    setStatus('ready');
  }, [sessionId, status, t]);

  useEffect(() => {
    if (!enabled) {
      activeControllerRef.current?.abort();
      activeControllerRef.current = null;
      replaceCurrentCwd(initialCwd);
      return;
    }

    replaceCurrentCwd(initialCwd);

    return () => {
      activeControllerRef.current?.abort();
      activeControllerRef.current = null;
    };
  }, [enabled, initialCwd, sessionId, replaceCurrentCwd]);

  // What the copy button hands over, and what `TerminalContent` watches to
  // keep the view pinned to the newest line. The session as a reader sees it:
  // the command lines included, so a copied transcript still says what
  // produced each block of output.
  const transcript = useMemo(() => lines.map((line) => line.text).join('\n'), [lines]);

  const sendCommand = async () => {
    if (!enabled) return;
    const cmd = input.trim();
    if (!cmd) return;
    setInput('');
    await runCommand(cmd);
  };

  const statusColorClass = !enabled
    ? 'text-muted-foreground'
    : status === 'ready'
      ? 'text-mint-fg'
      : status === 'running'
        ? 'text-teal-fg'
        : status === 'stopping'
          ? 'text-destructive'
          : 'text-muted-foreground';

  const statusLabel = !enabled
    ? t('misc:terminal.status_session_not_ready')
    : status === 'ready'
      ? t('misc:terminal.status_idle')
      : status === 'running'
        ? t('misc:terminal.status_running')
        : status === 'stopping'
          ? t('misc:terminal.status_stopping')
          : t('misc:terminal.status_error');

  const showStatus = !enabled || status !== 'ready';

  /*
    `Terminal` supplies the frame, copy action and newest-output scrolling.
    `TerminalPanel` passes the joined transcript as `output` so copying and
    scroll tracking include command lines, while custom children preserve the
    distinction between commands, stdout and stderr.

    The composition also overrides the vendored terminal's fixed neutral
    palette with the console's theme tokens; `TerminalPanel.test.tsx` checks
    that property on the rendered elements. The icon-only copy action receives
    its accessible name here (docs/frontend-design.md §10).

    `TerminalStatus` renders only while `isStreaming`, but this panel must also
    name the state in which the session cannot run commands.
  */
  return (
    <Terminal
      output={transcript}
      isStreaming={status === 'running'}
      className="h-full border-border bg-muted font-mono text-13 leading-relaxed text-foreground"
    >
      <TerminalHeader className="shrink-0 items-start gap-3 border-border">
        <div className="min-w-0 flex-1">
          {/* The same header the files panel wears, and the same rule: a path
              or nothing. Why there is no path belongs to the body below, which
              already says it; repeating it here would state it twice, a size
              larger (docs/frontend-design.md §1). */}
          <div className="text-11 text-muted-foreground">{t('misc:terminal.current_dir')}</div>
          <div className="break-all font-mono text-11 leading-4 text-muted-foreground">
            {cwd || '—'}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {showStatus ? (
            <span className={`text-xs font-medium ${statusColorClass}`}>
              {statusLabel}
            </span>
          ) : null}
          <TerminalActions>
            <TerminalCopyButton
              aria-label={t('misc:terminal.copy_output')}
              className="text-muted-foreground hover:bg-accent hover:text-foreground"
              disabled={lines.length === 0}
            />
          </TerminalActions>
        </div>
      </TerminalHeader>
      <TerminalContent className="max-h-none min-h-0 flex-1 text-13">
        {lines.length === 0 ? (
          <span className="text-muted-foreground text-sm">
            {enabled ? t('misc:terminal.ready_hint') : t('misc:terminal.not_ready_hint')}
          </span>
        ) : (
          lines.map((line, i) => (
            <div key={i} className={`whitespace-pre-wrap break-all ${lineClasses[line.type]}`}>
              {/* A command's output arrives as the process wrote it. Rendering
                  it through the same reader upstream's default body uses keeps
                  an escape sequence from landing in the panel as literal text
                  on the day a tool decides it is talking to a terminal. */}
              <Ansi>{line.text}</Ansi>
            </div>
          ))
        )}
      </TerminalContent>
      <div className="shrink-0 border-t border-border bg-background p-2">
        {/* `InputGroup` owns the focus-within ring for the prompt and its
            adjacent actions, giving the compound control one focus affordance
            as required by §11. */}
        <InputGroup>
          <InputGroupAddon>
            <span className="font-mono font-bold text-primary select-none">$</span>
          </InputGroupAddon>
          <InputGroupInput
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={t('misc:terminal.input_placeholder')}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                void sendCommand();
              }
            }}
            disabled={!enabled || status === 'running' || status === 'stopping'}
            className="font-mono text-13"
          />
          <InputGroupAddon align="inline-end">
            <InputGroupButton
              variant="secondary"
              onClick={() => void stopCommand()}
              disabled={!enabled || status !== 'running'}
            >
              {status === 'stopping' ? t('misc:terminal.stopping') : t('misc:terminal.stop')}
            </InputGroupButton>
          </InputGroupAddon>
        </InputGroup>
      </div>
    </Terminal>
  );
}
