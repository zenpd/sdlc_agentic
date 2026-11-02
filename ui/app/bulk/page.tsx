'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import Nav from '@/components/Nav';
import PageHeader from '@/components/PageHeader';
import StatusBadge from '@/components/StatusBadge';
import { getRuns, runTicket, formatDuration, formatTime, type RunRecord, type WorkflowMode } from '@/lib/api';
import { useActiveApp } from '@/components/AppContext';

const STORAGE_KEY = 'bulk-request-ticket-ids';

function personaOf(run: RunRecord): string | null {
  const fetchLine = run.logs.fetch?.[0] ?? '';
  const m = fetchLine.match(/\(persona:\s*([\w-]+)\)\s*$/);
  return m ? m[1] : null;
}

function summaryOf(run: RunRecord): string | null {
  const fetchLine = run.logs.fetch?.[0] ?? '';
  const m = fetchLine.match(/^\S+:\s*(.*?)\s*\(persona:/);
  return m ? m[1] : null;
}

function latestRunFor(runs: RunRecord[], ticketId: string): RunRecord | null {
  return runs.find((r) => r.ticket_key === ticketId || r.ticket_key.replace(/\D/g, '') === ticketId) ?? null;
}

function loadStoredIds(): string[] {
  if (typeof window === 'undefined') return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveStoredIds(ids: string[]) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
  } catch {
    // best-effort — a private window or blocked storage just means it won't persist
  }
}

function TicketCard({
  id, run, onRun, onRemove, running, canRun,
}: { id: string; run: RunRecord | null; onRun: () => void; onRemove: () => void; running: boolean; canRun: boolean }) {
  const persona = run ? personaOf(run) : null;
  const summary = run ? summaryOf(run) : null;

  return (
    <div className="card-main" style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, justifyContent: 'space-between' }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700 }}>#{id}</div>
          {summary && (
            <div style={{ fontSize: 12.5, color: 'var(--color-text-secondary)', marginTop: 2 }}>{summary}</div>
          )}
        </div>
        {run && <StatusBadge status={run.status} />}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))', gap: 10 }}>
        <div>
          <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', marginBottom: 3 }}>
            Persona
          </div>
          <div style={{ fontSize: 12.5, fontFamily: 'var(--font-mono)' }}>{persona ?? '—'}</div>
        </div>
        <div>
          <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', marginBottom: 3 }}>
            Duration
          </div>
          <div style={{ fontSize: 12.5, fontFamily: 'var(--font-mono)' }}>{run ? formatDuration(run.duration_sec) : '—'}</div>
        </div>
        <div>
          <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', marginBottom: 3 }}>
            Last run
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--color-text-secondary)' }}>{run ? formatTime(run.started_at) : '—'}</div>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 12, paddingTop: 4, borderTop: '1px solid var(--color-border)', marginTop: 2 }}>
        <button
          onClick={onRun}
          disabled={running || !canRun}
          className="btn-secondary"
          style={{ padding: '7px 14px', fontSize: 12.5, opacity: running || !canRun ? 0.5 : 1 }}
        >
          {running ? 'Running…' : run ? 'Re-run' : 'Run'}
        </button>
        {run && (
          <Link href={run.status === 'awaiting-human' && run.workflow_mode === 'manual' ? `/run?manual_run=${encodeURIComponent(run.id)}` : `/logs?run=${run.id}`} style={{ fontSize: 12.5, color: 'var(--color-accent-purple)', textDecoration: 'none' }}>
            {run.status === 'awaiting-human' && run.workflow_mode === 'manual' ? 'Continue manual task →' : 'View trace →'}
          </Link>
        )}
        <div style={{ flex: 1 }} />
        {run?.pr_url && (
          <a href={run.pr_url} target="_blank" rel="noopener noreferrer" style={{ fontSize: 12.5, color: 'var(--color-text-secondary)', textDecoration: 'none' }}>
            PR #{run.pr_url.split('/').pop()}
          </a>
        )}
        <button
          onClick={onRemove}
          aria-label="Remove"
          style={{ background: 'none', border: 'none', color: 'var(--color-text-muted)', cursor: 'pointer', fontSize: 16, lineHeight: 1, padding: 0 }}
        >
          ×
        </button>
      </div>
    </div>
  );
}

export default function BulkRequestPage() {
  const [ticketIds, setTicketIds] = useState<string[]>([]);
  const [inputValue, setInputValue] = useState('');
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [runningIds, setRunningIds] = useState<Set<string>>(new Set());
  const [bulkRunning, setBulkRunning] = useState(false);
  const [workflowMode, setWorkflowMode] = useState<WorkflowMode>('automatic');
  const { activeApp, activeAppId, me, can } = useActiveApp();
  const canRun = can('runner');

  useEffect(() => {
    setTicketIds(loadStoredIds());
  }, []);

  useEffect(() => {
    saveStoredIds(ticketIds);
  }, [ticketIds]);

  const refresh = useCallback(async () => {
    try {
      setRuns(await getRuns(activeAppId));
      setError(null);
    } catch (e: any) {
      setError(e.message);
    }
  }, [activeAppId]);

  useEffect(() => {
    refresh().finally(() => setLoading(false));
    const t = setInterval(refresh, 6000);
    return () => clearInterval(t);
  }, [refresh]);

  const latestByTicket = useMemo(() => {
    const map = new Map<string, RunRecord | null>();
    for (const id of ticketIds) map.set(id, latestRunFor(runs, id));
    return map;
  }, [runs, ticketIds]);

  const addIds = () => {
    const parts = inputValue.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
    if (parts.length === 0) return;
    setTicketIds((prev) => {
      const next = [...prev];
      for (const p of parts) if (!next.includes(p)) next.push(p);
      return next;
    });
    setInputValue('');
  };

  const removeId = (id: string) => setTicketIds((prev) => prev.filter((x) => x !== id));

  const runOne = async (id: string) => {
    setRunningIds((s) => new Set(s).add(id));
    try {
      await runTicket(id, activeAppId, workflowMode);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setRunningIds((s) => { const next = new Set(s); next.delete(id); return next; });
    }
  };

  const runAll = async () => {
    if (ticketIds.length === 0) return;
    setBulkRunning(true);
    setRunningIds(new Set(ticketIds));
    try {
      // Fire every ticket in parallel — none of these await each other, so
      // the backend receives and starts all of them at once, same as running
      // them by hand from separate tabs simultaneously.
      await Promise.allSettled(ticketIds.map((id) => runTicket(id, activeAppId, workflowMode)));
      await refresh();
    } finally {
      setRunningIds(new Set());
      setBulkRunning(false);
    }
  };

  const counts = useMemo(() => {
    let success = 0, failed = 0, running = 0, notRun = 0;
    for (const id of ticketIds) {
      const run = latestByTicket.get(id);
      if (!run) notRun++;
      else if (run.status === 'success') success++;
      else if (run.status === 'in-progress' || run.status === 'cancelling' || run.status === 'awaiting-approval' || run.status === 'awaiting-human') running++;
      else failed++;
    }
    return { success, failed, running, notRun };
  }, [ticketIds, latestByTicket]);

  return (
    <div className="bg-bg min-h-screen">
      <Nav />

      <div className="content">
        <PageHeader
          eyebrow="Parallel execution"
          title="Bulk Request"
          description={`Add any number of ticket IDs and run them all at once, in parallel — not one at a time. Runs use ${activeApp?.name ?? 'the active application'}'s settings.`}
        />

        <div className="card-main" style={{ padding: '18px 20px', marginBottom: 20 }}>
          <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
            <select
              className="input-main"
              aria-label="Execution mode"
              value={workflowMode}
              disabled={bulkRunning}
              onChange={(e) => setWorkflowMode(e.target.value as WorkflowMode)}
              style={{ maxWidth: 300 }}
            >
              <option value="automatic">Automatic</option>
              <option value="manual">Manual</option>
            </select>
            <input
              className="input-main"
              placeholder="Ticket ID(s) — e.g. 674 or 674, 675, 676"
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') addIds(); }}
              style={{ maxWidth: 320 }}
            />
            <button onClick={addIds} className="btn-secondary" style={{ padding: '10px 18px', fontSize: 13 }}>
              Add
            </button>
            <div style={{ flex: 1 }} />
            <button
              onClick={runAll}
              disabled={bulkRunning || ticketIds.length === 0 || !canRun}
              className="btn-primary"
              style={{ padding: '10px 20px', fontSize: 13, opacity: bulkRunning || ticketIds.length === 0 || !canRun ? 0.5 : 1 }}
            >
              {bulkRunning ? 'Running all…' : `Run All (${ticketIds.length})`}
            </button>
          </div>

          {me && !canRun && <div className="locked-note" style={{ marginTop: 14 }}>🔒 Your role is <b>{me.role}</b> — starting runs needs <b>runner</b> or higher. Ask an admin.</div>}
          {ticketIds.length > 0 && (
            <div style={{ display: 'flex', gap: 16, marginTop: 14, paddingTop: 14, borderTop: '1px solid var(--color-border)', fontSize: 12.5, color: 'var(--color-text-secondary)', flexWrap: 'wrap' }}>
              <span>{ticketIds.length} tracked</span>
              {counts.success > 0 && <span style={{ color: 'var(--color-green)' }}>{counts.success} succeeded</span>}
              {counts.running > 0 && <span style={{ color: 'var(--color-accent-purple)' }}>{counts.running} running</span>}
              {counts.failed > 0 && <span style={{ color: 'var(--color-red)' }}>{counts.failed} failed</span>}
              {counts.notRun > 0 && <span>{counts.notRun} not run yet</span>}
            </div>
          )}
        </div>

        {error && <div className="alert error">{error}</div>}

        {loading ? (
          <div style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>Loading...</div>
        ) : ticketIds.length === 0 ? (
          <div className="card-main" style={{ padding: 24, color: 'var(--color-text-muted)', fontSize: 14 }}>
            No tickets added yet — enter one or more ticket IDs above to get started.
          </div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: 14 }}>
            {ticketIds.map((id) => (
              <TicketCard
                key={id}
                id={id}
                run={latestByTicket.get(id) ?? null}
                onRun={() => runOne(id)}
                onRemove={() => removeId(id)}
                running={runningIds.has(id)}
                canRun={canRun}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
