'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import Nav from '@/components/Nav';
import PageHeader from '@/components/PageHeader';
import StepTracker from '@/components/StepTracker';
import StatusBadge from '@/components/StatusBadge';
import StageProgress from '@/components/StageProgress';
import { runTicket, getRunStatus, describeDuration, formatDuration, withoutAgentTrace, type RunRecord, type StepInfo } from '@/lib/api';
import { useActiveApp } from '@/components/AppContext';
import ApprovalPanel from '@/components/ApprovalPanel';

const STEPS: StepInfo[] = [
  { key: 'fetch', label: 'Fetching ticket', state: 'pending' },
  { key: 'assign', label: 'Assigning to agent', state: 'pending' },
  { key: 'in_progress', label: 'Moving to In Progress', state: 'pending' },
  { key: 'gate', label: 'Clarity gate', state: 'pending' },
  { key: 'agent', label: 'Agent coding & testing', state: 'pending' },
  { key: 'verify', label: 'Verification review', state: 'pending' },
  { key: 'pr', label: 'Opening PR', state: 'pending' },
  { key: 'jira', label: 'Updating Ticket', state: 'pending' },
];

const CONFETTI_PIECES = [
  { x:  8, color: '#6366f1', delay: 0,    size: 8,  dur: 1.1 },
  { x: 15, color: '#10b981', delay: 0.08, size: 6,  dur: 0.9 },
  { x: 23, color: '#f59e0b', delay: 0.04, size: 9,  dur: 1.2 },
  { x: 33, color: '#8b5cf6', delay: 0.12, size: 7,  dur: 1.0 },
  { x: 42, color: '#6366f1', delay: 0.02, size: 8,  dur: 1.15 },
  { x: 50, color: '#10b981', delay: 0.15, size: 10, dur: 0.95 },
  { x: 60, color: '#f59e0b', delay: 0.06, size: 6,  dur: 1.05 },
  { x: 70, color: '#6366f1', delay: 0.10, size: 9,  dur: 1.2 },
  { x: 80, color: '#8b5cf6', delay: 0.03, size: 7,  dur: 0.85 },
  { x: 88, color: '#10b981', delay: 0.14, size: 8,  dur: 1.0 },
  { x: 18, color: '#dc2626', delay: 0.09, size: 6,  dur: 0.95 },
  { x: 55, color: '#dc2626', delay: 0.11, size: 7,  dur: 1.1 },
  { x: 75, color: '#f59e0b', delay: 0.07, size: 5,  dur: 0.9 },
  { x: 38, color: '#a78bfa', delay: 0.13, size: 8,  dur: 1.05 },
  { x: 64, color: '#a78bfa', delay: 0.01, size: 6,  dur: 1.15 },
];

function Confetti() {
  return (
    <div
      aria-hidden
      style={{ position: 'absolute', top: 0, left: 0, right: 0, height: 160, pointerEvents: 'none', overflow: 'hidden' }}
    >
      {CONFETTI_PIECES.map((p, i) => (
        <div
          key={i}
          style={{
            position: 'absolute',
            top: 8,
            left: `${p.x}%`,
            width: p.size,
            height: p.size,
            borderRadius: p.size % 3 === 0 ? '50%' : 2,
            background: p.color,
            animation: `confettiFall ${p.dur}s ease-out ${p.delay}s forwards`,
          }}
        />
      ))}
    </div>
  );
}

export default function RunPage() {
  const [ticketKey, setTicketKey]   = useState('');
  const [runId, setRunId]           = useState<string | null>(null);
  const [status, setStatus]         = useState<RunRecord['status'] | null>(null);
  const [stepStates, setStepStates] = useState<Array<'pending' | 'in-progress' | 'success' | 'failed' | 'error' | 'human'>>(
    STEPS.map(() => 'pending')
  );
  const [logs, setLogs]         = useState<Record<string, string[]>>({});
  const [prUrl, setPrUrl]       = useState<string | null>(null);
  const [duration, setDuration] = useState<number | null>(null);
  const [error, setError]       = useState<string | null>(null);
  const [running, setRunning]   = useState(false);
  const [elapsed, setElapsed]   = useState(0);
  const [record, setRecord]     = useState<RunRecord | null>(null);
  const { activeApp, activeAppId, me, can } = useActiveApp();
  const canRun = can('runner');

  const intervalRef   = useRef<ReturnType<typeof setInterval> | null>(null);
  const timerRef      = useRef<ReturnType<typeof setInterval> | null>(null);
  const startTimeRef  = useRef<number | null>(null);

  const stopTimer = () => {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
  };

  const startPolling = useCallback((id: string) => {
    intervalRef.current = setInterval(async () => {
      try {
        const r = await getRunStatus(id);
        setRecord(r);
        setStatus(r.status);
        setStepStates(r.steps.map((s) => s.state as any));
        setLogs(r.logs);
        setPrUrl(r.pr_url);
        setDuration(r.duration_sec);

        if (['success', 'failed', 'error', 'human'].includes(r.status)) {
          if (intervalRef.current) { clearInterval(intervalRef.current); intervalRef.current = null; }
          setRunning(false);
          stopTimer();
        }
      } catch { /* keep polling */ }
    }, 2000);
  }, []);

  useEffect(() => {
    if (!running) return;
    startTimeRef.current = Date.now();
    setElapsed(0);
    timerRef.current = setInterval(() => {
      setElapsed(Math.floor((Date.now() - (startTimeRef.current ?? Date.now())) / 1000));
    }, 1000);
    return stopTimer;
  }, [running]);

  useEffect(() => {
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
      stopTimer();
    };
  }, []);

  const handleRun = async () => {
    if (!ticketKey.trim()) return;
    setError(null); setRunId(null); setStatus(null); setRecord(null);
    setStepStates(STEPS.map(() => 'pending'));
    setLogs({}); setPrUrl(null); setDuration(null);
    setRunning(true);

    try {
      const { run_id } = await runTicket(ticketKey.trim(), activeAppId);
      setRunId(run_id);
      startPolling(run_id);
    } catch (e: any) {
      setError(e.message);
      setRunning(false);
    }
  };

  const isTerminal = status && ['success', 'failed', 'error', 'human'].includes(status);

  return (
    <div className="bg-bg min-h-screen">
      <Nav />

      <div className="content">
        <PageHeader
          eyebrow="Pipeline"
          title="Run Pipeline"
          description="Trigger a pipeline run for a specific ticket or work item, and watch the agent work live."
        />

        {/* Input */}
        <div className="card-main" style={{ padding: 24, marginBottom: 20 }}>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 16 }}>
            <div style={{ flex: 1 }}>
              <label className="label-main">Ticket ID</label>
              <input
                className="input-main"
                placeholder="e.g. KAN-1 or 668"
                value={ticketKey}
                onChange={(e) => setTicketKey(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') handleRun(); }}
                disabled={running}
              />
            </div>
            <button
              className="btn-primary"
              onClick={handleRun}
              disabled={running || !ticketKey.trim() || !canRun}
              style={running || !canRun ? { opacity: 0.5, pointerEvents: 'none', cursor: 'not-allowed' } : {}}
            >
              {running ? (
                <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" style={{ animation: 'spin 1s linear infinite' }}>
                    <path d="M21 12a9 9 0 1 1-6.219-8.56" strokeLinecap="round" />
                  </svg>
                  Running…
                </span>
              ) : 'Run Pipeline'}
            </button>
          </div>
          {activeApp && (
            <div className="app-card-meta" style={{ marginTop: 14 }}>
              <span className="chip purple">Application · {activeApp.name}</span>
              <span className="chip">{activeApp.ticket_provider === 'ado' ? 'Azure DevOps' : 'Jira'}</span>
              <span className="chip">{activeApp.target_repo ? `${activeApp.target_repo} · ${activeApp.target_branch}` : 'No repository yet'}</span>
              <span className="chip">{activeApp.default_persona ? `persona · ${activeApp.default_persona}` : 'persona · auto'}</span>
              {activeApp.require_pr_approval && <span className="chip purple">🛡️ approval before PR</span>}
              {activeApp.protected_paths && <span className="chip" title={activeApp.protected_paths}>🔒 protected paths</span>}
              {activeApp.verify_mode === 'command' && <span className="chip" title="Tests run before any PR opens">🧪 {activeApp.test_command}</span>}
        {activeApp.verify_mode === 'github_actions' && <span className="chip">⚙️ GitHub Actions{activeApp.ci_workflow ? ` · ${activeApp.ci_workflow}` : ''}</span>}
            </div>
          )}
          {me && !canRun && <div className="locked-note" style={{ marginTop: 14 }}>🔒 Your role is <b>{me.role}</b> — starting runs needs <b>runner</b> or higher. Ask an admin.</div>}
          {error && <p style={{ marginTop: 12, color: 'var(--color-red)', fontSize: 13 }}>{error}</p>}
        </div>

        {/* Status */}
        {runId && (
          <div className="card-main" style={{ padding: 28, position: 'relative', overflow: 'hidden' }}>
            {status === 'success' && <Confetti />}

            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 24, flexWrap: 'wrap', gap: 12 }}>
              <h2 style={{ fontSize: 18, fontWeight: 700 }}>{ticketKey}</h2>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                {running && (
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 13, color: 'var(--color-text-muted)' }}>
                    {status === 'awaiting-approval' && record ? `⏸ ${describeDuration(record)}` : formatDuration(elapsed)}
                  </span>
                )}
                {status && <StatusBadge status={status} />}
              </div>
            </div>

            {record && <ApprovalPanel run={record} onDecided={(r) => { setRecord(r); setStatus(r.status); }} />}

            <div style={{ marginBottom: 28 }}>
              <StageProgress
                stages={STEPS.map(({ key, label }) => ({ key, label }))}
                states={stepStates}
              />
            </div>

            <StepTracker
              steps={STEPS.map(({ state, ...rest }) => rest)}
              states={stepStates}
              logs={withoutAgentTrace(logs)}
            />

            {isTerminal && (
              <div style={{ marginTop: 28, paddingTop: 20, borderTop: '1px solid var(--color-border)' }}>
                {status === 'success' && prUrl && (
                  <div style={{ fontSize: 14, display: 'flex', flexDirection: 'column', gap: 8 }}>
                    <p style={{ color: 'var(--color-green)', fontWeight: 600 }}>
                      ✓ Pipeline complete!
                    </p>
                    <p>
                      PR:{' '}
                      <a href={prUrl} target="_blank" rel="noopener noreferrer"
                        style={{ color: 'var(--color-accent-purple)', textDecoration: 'underline' }}>
                        {prUrl}
                      </a>
                    </p>
                    <p style={{ color: 'var(--color-text-secondary)' }}>
                      Duration: {record ? describeDuration(record) : formatDuration(duration)}
                    </p>
                  </div>
                )}
                {(status === 'failed' || status === 'error') && (
                  <p style={{ color: 'var(--color-red)', fontSize: 14 }}>
                    Pipeline failed. Check logs for details.
                  </p>
                )}
                {status === 'human' && (
                  <p style={{ color: '#d97706', fontSize: 14 }}>
                    Ticket needs human review.
                  </p>
                )}
              </div>
            )}
          </div>
        )}

        {!runId && !running && (
          <div style={{ textAlign: 'center', paddingTop: 72 }}>
            <p style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>
              Enter a ticket ID and click Run Pipeline to start.
            </p>
          </div>
        )}
      </div>

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
