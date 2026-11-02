'use client';

import { Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import Nav from '@/components/Nav';
import PageHeader from '@/components/PageHeader';
import StatusBadge from '@/components/StatusBadge';
import StageProgress from '@/components/StageProgress';
import StepTracker from '@/components/StepTracker';
import { useActiveApp } from '@/components/AppContext';
import ApprovalPanel from '@/components/ApprovalPanel';
import {
  describeDuration, formatCost, formatTime, getRuns, withoutAgentTrace,
  type RunRecord, type StepInfo, type StepState,
} from '@/lib/api';

const STEPS: StepInfo[] = [
  { key: 'fetch', label: 'Fetching ticket', state: 'pending' },
  { key: 'assign', label: 'Assigning to agent', state: 'pending' },
  { key: 'in_progress', label: 'Moving to In Progress', state: 'pending' },
  { key: 'gate', label: 'Clarity gate', state: 'pending' },
  { key: 'agent', label: 'Agent coding & testing', state: 'pending' },
  { key: 'verify', label: 'Verification review', state: 'pending' },
  { key: 'pr', label: 'Opening PR', state: 'pending' },
  { key: 'jira', label: 'Updating ticket', state: 'pending' },
];

const STATUSES = ['in-progress', 'awaiting-approval', 'awaiting-human', 'success', 'failed', 'error', 'human'] as const;

function ticketSummary(run: RunRecord): string | null {
  const line = run.logs.fetch?.[0] ?? '';
  const match = line.match(/^\S+:\s*(.*?)\s*\(persona:\s*[\w-]+\)\s*$/);
  return match?.[1] ?? null;
}

function personaName(run: RunRecord): string | null {
  const line = run.logs.fetch?.[0] ?? '';
  return line.match(/\(persona:\s*([\w-]+)\)\s*$/)?.[1] ?? null;
}

function RunHistoryItem({
  run, selected, onSelect,
}: { run: RunRecord; selected: boolean; onSelect: () => void }) {
  const summary = ticketSummary(run);
  const persona = personaName(run);

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className="card-main"
      style={{
        display: 'block', width: '100%', padding: 16, marginBottom: 10, textAlign: 'left',
        border: selected ? '1px solid var(--color-accent-purple)' : undefined,
        boxShadow: selected ? '0 0 0 2px rgba(109,75,234,.12)' : undefined,
        background: selected ? 'var(--color-accent-light)' : undefined,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
        <strong>{run.ticket_key.match(/^\d+$/) ? `#${run.ticket_key}` : run.ticket_key}</strong>
        <StatusBadge status={run.status} />
      </div>
      {summary && (
        <div style={{
          marginTop: 6, fontSize: 12.5, color: 'var(--color-text-secondary)',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {summary}
        </div>
      )}
      <div style={{ marginTop: 8, fontSize: 11.5, color: 'var(--color-text-muted)' }}>
        {persona ? `${persona} · ` : ''}{run.triggered_by ? `by @${run.triggered_by} · ` : ''}{formatTime(run.started_at)} · {describeDuration(run)}
      </div>
    </button>
  );
}

function LogsContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { activeApp } = useActiveApp();
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState(searchParams.get('q') ?? '');
  const [statusFilter, setStatusFilter] = useState('all');
  const [selectedId, setSelectedId] = useState(searchParams.get('run'));
  const requestSequence = useRef(0);

  const refreshRuns = useCallback(async (showLoading = false) => {
    const sequence = ++requestSequence.current;
    if (showLoading) setLoading(true);
    try {
      const result = await getRuns(activeApp?.id);
      if (sequence !== requestSequence.current) return;
      setRuns(result);
      setError(null);
    } catch (e) {
      if (sequence === requestSequence.current) {
        setError(e instanceof Error ? e.message : 'Could not load run history.');
      }
    } finally {
      if (showLoading && sequence === requestSequence.current) setLoading(false);
    }
  }, [activeApp?.id]);

  useEffect(() => {
    void refreshRuns(true);
    const timer = setInterval(() => { void refreshRuns(); }, 6000);
    return () => {
      requestSequence.current++;
      clearInterval(timer);
    };
  }, [refreshRuns]);

  useEffect(() => {
    setQuery(searchParams.get('q') ?? '');
    setSelectedId(searchParams.get('run'));
  }, [searchParams]);

  const normalizedQuery = query.trim().toLowerCase();
  const filteredRuns = runs.filter((run) => {
    if (statusFilter !== 'all' && run.status !== statusFilter) return false;
    if (!normalizedQuery) return true;
    const searchable = [
      run.ticket_key,
      ticketSummary(run) ?? '',
      personaName(run) ?? '',
      run.triggered_by ?? '',
      run.pr_url ?? '',
      ...Object.values(run.logs).flat(),
    ].join(' ').toLowerCase();
    return searchable.includes(normalizedQuery);
  });

  const selectedRun = (selectedId
    ? runs.find((run) => run.id === selectedId)
    : filteredRuns[0]) ?? null;

  const selectRun = (run: RunRecord) => {
    setSelectedId(run.id);
    const params = new URLSearchParams();
    if (searchParams.get('q')) params.set('q', searchParams.get('q')!);
    params.set('run', run.id);
    router.replace(`/logs?${params.toString()}`, { scroll: false });
  };

  const stepStates: StepState[] = STEPS.map(
    ({ key }) => selectedRun?.steps.find((step) => step.key === key)?.state ?? 'pending',
  );

  return (
    <div className="bg-bg min-h-screen">
      <Nav />
      <main className="content">
        <PageHeader
          eyebrow="Run history"
          title="Logs"
          description={`Review pipeline history and agent traces for ${activeApp?.name ?? 'the active application'}.`}
          actions={
            <button type="button" className="btn-secondary" onClick={() => void refreshRuns()}>
              Refresh
            </button>
          }
        />

        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(280px, 380px) minmax(0, 1fr)', gap: 18, alignItems: 'start' }}>
          <section className="card-main" style={{ padding: 18 }}>
            <div className="panel-head">
              <h3>Run history</h3>
              <span style={{ fontSize: 12, color: 'var(--color-text-muted)' }}>{filteredRuns.length} shown</span>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 130px', gap: 8, marginBottom: 14 }}>
              <input
                className="input-main"
                type="search"
                aria-label="Search run history"
                placeholder="Ticket, summary, persona…"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
              <select
                className="input-main"
                aria-label="Filter by run status"
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value)}
              >
                <option value="all">All statuses</option>
                {STATUSES.map((status) => (
                  <option key={status} value={status}>{status.replace('-', ' ')}</option>
                ))}
              </select>
            </div>

            {error && <div className="alert error">{error}</div>}
            {loading ? (
              <p style={{ color: 'var(--color-text-muted)', fontSize: 13 }}>Loading run history…</p>
            ) : filteredRuns.length === 0 ? (
              <p style={{ color: 'var(--color-text-muted)', fontSize: 13 }}>
                {runs.length ? 'No runs match these filters.' : 'No runs have been recorded for this application yet.'}
              </p>
            ) : (
              <div style={{ maxHeight: 'calc(100vh - 290px)', overflowY: 'auto', padding: '2px 3px 2px 1px' }}>
                {filteredRuns.map((run) => (
                  <RunHistoryItem
                    key={run.id}
                    run={run}
                    selected={run.id === selectedRun?.id}
                    onSelect={() => selectRun(run)}
                  />
                ))}
              </div>
            )}
          </section>

          <section>
            {!selectedRun ? (
              <div className="card-main" style={{ padding: 28, color: 'var(--color-text-muted)', fontSize: 14 }}>
                Select a run to inspect its stages, logs, and agent activity.
              </div>
            ) : (
              <div className="card-main" style={{ padding: 22 }}>
                <div className="panel-head" style={{ flexWrap: 'wrap' }}>
                  <div>
                    <h3>{selectedRun.ticket_key.match(/^\d+$/) ? `#${selectedRun.ticket_key}` : selectedRun.ticket_key}</h3>
                    {ticketSummary(selectedRun) && (
                      <p style={{ marginTop: 4, color: 'var(--color-text-secondary)', fontSize: 13 }}>
                        {ticketSummary(selectedRun)}
                      </p>
                    )}
                  </div>
                  <StatusBadge status={selectedRun.status} />
                </div>

                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 18px', marginBottom: 22, color: 'var(--color-text-muted)', fontSize: 12 }}>
                  <span>Started {formatTime(selectedRun.started_at)}{selectedRun.triggered_by ? ` by @${selectedRun.triggered_by}` : ''}</span>
                  <span>Mode {selectedRun.workflow_mode}</span>
                  <span>Duration {describeDuration(selectedRun)}</span>
                  <span>Persona {personaName(selectedRun) ?? 'not recorded'}</span>
                  <span>Run {selectedRun.id}</span>
                </div>

                <div
                  className="card-main"
                  style={{
                    display: 'flex',
                    flexWrap: 'wrap',
                    gap: '8px 24px',
                    padding: '14px 16px',
                    marginBottom: 20,
                    fontSize: 12.5,
                  }}
                >
                  <span>
                    LLM cost: {selectedRun.cost_status === 'pending'
                      ? 'still collecting'
                      : selectedRun.cost_usd == null
                        ? 'not available'
                      : `${formatCost(selectedRun.cost_usd)} USD`}
                    {selectedRun.cost_status === 'partial' ? ' (partial estimate)' : ''}
                    {selectedRun.cost_status === 'estimated' ? ' (estimated)' : ''}
                  </span>
                  <span>
                    Tokens: {selectedRun.total_tokens.toLocaleString()} total
                    {' '}({selectedRun.prompt_tokens.toLocaleString()} input /{' '}
                    {selectedRun.completion_tokens.toLocaleString()} output)
                  </span>
                </div>

                {selectedRun.pr_url && (
                  <p style={{ marginBottom: 18, fontSize: 13 }}>
                    Pull request:{' '}
                    <a href={selectedRun.pr_url} target="_blank" rel="noopener noreferrer" className="link-btn">
                      {selectedRun.pr_url}
                    </a>
                  </p>
                )}

                <ApprovalPanel run={selectedRun} onDecided={() => void refreshRuns()} />
                {selectedRun.status === 'awaiting-human' && selectedRun.workflow_mode === 'manual' && (
                  <div className="tip-box warn" style={{ marginBottom: 18 }}>
                    <b>Manual stage awaiting completion</b>
                    <p style={{ marginBottom: 10 }}>
                      {selectedRun.manual_task?.label ?? 'A workflow stage'} needs a human result before this run can continue.
                    </p>
                    <button className="btn-secondary" onClick={() => router.push(`/run?manual_run=${encodeURIComponent(selectedRun.id)}`)}>
                      Continue manual task
                    </button>
                  </div>
                )}

                <div style={{ marginBottom: 26 }}>
                  <StageProgress
                    stages={STEPS.map(({ key, label }) => ({ key, label }))}
                    states={stepStates}
                  />
                </div>

                <StepTracker
                  steps={STEPS.map(({ key, label }) => ({ key, label }))}
                  states={stepStates}
                  logs={withoutAgentTrace(selectedRun.logs)}
                />
              </div>
            )}
          </section>
        </div>
      </main>
    </div>
  );
}

export default function LogsPage() {
  return (
    <Suspense fallback={<div className="content">Loading run history…</div>}>
      <LogsContent />
    </Suspense>
  );
}
