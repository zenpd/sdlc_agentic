'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import Nav from '@/components/Nav';
import PageHeader from '@/components/PageHeader';
import StepTracker from '@/components/StepTracker';
import StatusBadge from '@/components/StatusBadge';
import StageProgress from '@/components/StageProgress';
import { runTicket, cancelRun, getRunStatus, getRuns, describeDuration, formatDuration, withoutAgentTrace, manualTaskNeedsHumanAction, type RunRecord, type StepInfo, type WorkflowMode } from '@/lib/api';
import { useActiveApp } from '@/components/AppContext';
import ApprovalPanel from '@/components/ApprovalPanel';
import ManualStagePanel from '@/components/ManualStagePanel';

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

function manualStageLogs(record: RunRecord): Record<string, string[]> {
  const logs: Record<string, string[]> = {};
  const context = record.manual_task?.context ?? {};
  const steps = new Map(record.steps.map((step) => [step.key, step.state]));

  const assignee = context.assign?.actual_assignee;
  if (assignee) logs.assign = [`Target user: ${assignee}`];

  const status = context.in_progress?.status;
  if (status) {
    const operation = context.in_progress?.operation ?? '';
    logs.in_progress = [
      operation.toLowerCase().startsWith('already')
        ? `Already active: ${status}`
        : `Status updated: ${status}`,
    ];
  }

  if (context.gate?.decision === 'pass') {
    logs.gate = ['Clarity check completed'];
  }

  for (const key of ['fetch', 'assign', 'in_progress', 'gate', 'agent', 'verify', 'pr', 'jira']) {
    const state = steps.get(key);
    if ((state === 'failed' || state === 'error') && record.logs[key]?.length) {
      logs[key] = [record.logs[key][record.logs[key].length - 1]];
    }
  }

  return logs;
}

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
  const [workflowMode, setWorkflowMode] = useState<WorkflowMode>('automatic');
  const [runId, setRunId]           = useState<string | null>(null);
  const [status, setStatus]         = useState<RunRecord['status'] | null>(null);
  const [stepStates, setStepStates] = useState<Array<'pending' | 'in-progress' | 'success' | 'failed' | 'error' | 'human' | 'cancelled'>>(
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
  const pollingRunIdRef = useRef<string | null>(null);
  const skipManualResumeRef = useRef(false);

  const stopTimer = useCallback(() => {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
  }, []);

  const clearForNewRun = useCallback(() => {
    skipManualResumeRef.current = true;
    if (intervalRef.current) { clearInterval(intervalRef.current); intervalRef.current = null; }
    pollingRunIdRef.current = null;
    stopTimer();
    startTimeRef.current = null;
    setTicketKey('');
    setWorkflowMode('automatic');
    setRunId(null);
    setStatus(null);
    setStepStates(STEPS.map(() => 'pending'));
    setLogs({});
    setPrUrl(null);
    setDuration(null);
    setError(null);
    setRunning(false);
    setElapsed(0);
    setRecord(null);
  }, [stopTimer]);

  const startPolling = useCallback((id: string) => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    pollingRunIdRef.current = id;
    intervalRef.current = setInterval(async () => {
      try {
        const r = await getRunStatus(id);
        if (pollingRunIdRef.current !== id) return;
        setRecord(r);
        setStatus(r.status);
        setStepStates(r.steps.map((s) => s.state as any));
        setLogs(r.logs);
        setPrUrl(r.pr_url);
        setDuration(r.duration_sec);

        if (['success', 'failed', 'error', 'human', 'cancelled'].includes(r.status)) {
          if (intervalRef.current) { clearInterval(intervalRef.current); intervalRef.current = null; }
          pollingRunIdRef.current = null;
          setRunning(false);
          stopTimer();
        } else if (r.status === 'awaiting-human') {
          const implementationRunning = ['generating', 'validating', 'applying', 'verifying', 'creating', 'updating'].includes(
            r.manual_task?.phase ?? '',
          );
          setRunning(implementationRunning);
          if (!implementationRunning) {
            if (intervalRef.current) {
              clearInterval(intervalRef.current);
              intervalRef.current = null;
            }
            stopTimer();
          }
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
      pollingRunIdRef.current = null;
      stopTimer();
    };
  }, []);

  useEffect(() => {
    skipManualResumeRef.current = false;
  }, [activeAppId]);

  useEffect(() => {
    const handleNewRun = () => {
      window.history.replaceState(null, '', '/run');
      clearForNewRun();
    };
    window.addEventListener('sdlc:new-run', handleNewRun);
    return () => window.removeEventListener('sdlc:new-run', handleNewRun);
  }, [clearForNewRun]);

  useEffect(() => {
    let cancelled = false;
    const searchParams = new URLSearchParams(window.location.search);
    if (searchParams.get('new_run') === '1') {
      window.history.replaceState(null, '', '/run');
      clearForNewRun();
      return () => { cancelled = true; };
    }
    if (skipManualResumeRef.current) return;
    const requestedRunId = searchParams.get('manual_run');
    if (requestedRunId) window.history.replaceState(null, '', '/run');
    const loadingRuns = requestedRunId
      ? getRunStatus(requestedRunId).then((run) => [run])
      : getRuns(activeAppId);
    loadingRuns.then((runs) => {
      if (cancelled || skipManualResumeRef.current || runId) return;
      const pending = runs.find((run) => (
        run.app_id === activeAppId
        && (
          (run.workflow_mode === 'manual' && run.status === 'awaiting-human')
          || (
            run.workflow_mode !== 'manual'
            && ['in-progress', 'cancelling', 'awaiting-approval'].includes(run.status)
          )
        )
      ));
      if (!pending) return;
      setRunId(pending.id);
      setTicketKey(pending.ticket_key);
      setWorkflowMode(pending.workflow_mode === 'manual' ? 'manual' : 'automatic');
      setRecord(pending);
      setStatus(pending.status);
      setStepStates(pending.steps.map((step) => step.state));
      setLogs(pending.logs);
      setPrUrl(pending.pr_url);
      setDuration(pending.duration_sec);
      setRunning(
        pending.workflow_mode !== 'manual'
        || ['generating', 'validating', 'applying', 'verifying', 'creating', 'updating'].includes(
          pending.manual_task?.phase ?? '',
        ),
      );
      startPolling(pending.id);
    }).catch((loadError: unknown) => {
      if (!cancelled) {
        setError(loadError instanceof Error ? loadError.message : 'Could not load manual run details.');
      }
    });
    return () => { cancelled = true; };
  }, [activeAppId, runId, startPolling, clearForNewRun]);

  const handleRun = async () => {
    if (!ticketKey.trim()) return;
    skipManualResumeRef.current = true;
    window.history.replaceState(null, '', '/run');
    if (intervalRef.current) { clearInterval(intervalRef.current); intervalRef.current = null; }
    pollingRunIdRef.current = null;
    setError(null); setRunId(null); setStatus(null); setRecord(null);
    setStepStates(STEPS.map(() => 'pending'));
    setLogs({}); setPrUrl(null); setDuration(null);
    setRunning(true);

    try {
      const { run_id } = await runTicket(ticketKey.trim(), activeAppId, workflowMode);
      setRunId(run_id);
      startPolling(run_id);
    } catch (e: any) {
      setError(e.message);
      setRunning(false);
    }
  };

  const handleStop = async () => {
    if (!runId) return;
    setError(null);
    try {
      const updated = await cancelRun(runId);
      setRecord(updated);
      setStatus(updated.status);
      setStepStates(updated.steps.map((step) => step.state));
      setLogs(updated.logs);
      if (updated.status === 'cancelled') {
        setRunning(false);
        stopTimer();
      } else {
        setRunning(true);
        startPolling(updated.id);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not stop the run.');
    }
  };

  const isTerminal = status && ['success', 'failed', 'error', 'human', 'cancelled'].includes(status);
  const pipelineStepStates = [...stepStates];
  const manualTask = record?.manual_task;
  if (record?.status === 'awaiting-human' && manualTask) {
    const waitingStageIndex = STEPS.findIndex(
      (step) => step.key === manualTask.stage_key,
    );
    if (waitingStageIndex !== -1) {
      const automatedPhase = ['generating', 'validating', 'applying', 'verifying', 'creating', 'updating']
        .includes(manualTask.phase);
      if (automatedPhase) {
        pipelineStepStates[waitingStageIndex] = 'in-progress';
      } else if (manualTaskNeedsHumanAction(manualTask)) {
        pipelineStepStates[waitingStageIndex] = 'human';
      }
    }
  }

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
          <div style={{ marginBottom: 18 }}>
            <label className="label-main" htmlFor="workflow-mode">Execution mode</label>
            <select
              id="workflow-mode"
              className="input-main"
              value={workflowMode}
              disabled={running}
              onChange={(e) => setWorkflowMode(e.target.value as WorkflowMode)}
            >
              <option value="automatic">🤖 Automatic</option>
              <option value="manual">👤 Manual</option>
            </select>
          </div>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 12 }}>
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
            {runId
              && workflowMode !== 'manual'
              && ['in-progress', 'awaiting-approval', 'cancelling'].includes(status ?? '')
              && (
                <button
                  className="btn-secondary"
                  type="button"
                  onClick={handleStop}
                  disabled={status === 'cancelling' || !canRun}
                >
                  {status === 'cancelling' ? 'Stopping…' : 'Stop run'}
                </button>
              )}
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
                {running && status !== 'awaiting-human' && (
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 13, color: 'var(--color-text-muted)' }}>
                    {status === 'awaiting-approval' && record ? `⏸ ${describeDuration(record)}` : formatDuration(elapsed)}
                  </span>
                )}
                {status && <StatusBadge status={status} />}
              </div>
            </div>

            {record && <ManualStagePanel
              run={record}
              onCompleted={(updated) => {
                setRecord(updated);
                setStatus(updated.status);
                setStepStates(updated.steps.map((step) => step.state));
                setLogs(updated.logs);
                setPrUrl(updated.pr_url);
                setRunning(
                  updated.status !== 'awaiting-human'
                    ? !['success', 'failed', 'error', 'human'].includes(updated.status)
                    : ['generating', 'validating', 'applying', 'verifying', 'creating', 'updating'].includes(updated.manual_task?.phase ?? ''),
                );
                if (['generating', 'validating', 'applying', 'verifying', 'creating', 'updating'].includes(updated.manual_task?.phase ?? '')) {
                  startPolling(updated.id);
                }
              }}
            />}
            {status === 'cancelling' && (
              <p style={{ color: 'var(--color-text-secondary)', fontSize: 13, marginTop: 0 }}>
                Stop requested. The current operation will finish before the pipeline stops; later stages will not start.
              </p>
            )}
            {record && <ApprovalPanel run={record} onDecided={(r) => { setRecord(r); setStatus(r.status); }} />}

            <div style={{ marginBottom: 28 }}>
              <StageProgress
                stages={STEPS.map(({ key, label }) => ({ key, label }))}
                states={pipelineStepStates}
              />
            </div>

            <StepTracker
              steps={STEPS.map(({ state, ...rest }) => rest)}
              states={stepStates}
              logs={workflowMode === 'manual' && record
                ? manualStageLogs(record)
                : withoutAgentTrace(logs)}
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
                {status === 'cancelled' && (
                  <p style={{ color: 'var(--color-text-secondary)', fontSize: 14 }}>
                    Pipeline stopped.
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
