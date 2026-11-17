'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useState } from 'react';
import Nav from '@/components/Nav';
import PageHeader from '@/components/PageHeader';
import { useActiveApp } from '@/components/AppContext';
import {
  actOnApprovalRequest,
  getApprovalRequests,
  formatTime,
  type ApprovalInboxItem,
} from '@/lib/api';

export default function ApprovalsPage() {
  const { activeApp, activeAppId, can, me } = useActiveApp();
  const [requests, setRequests] = useState<ApprovalInboxItem[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [personaFilter, setPersonaFilter] = useState('all');
  const [busyAction, setBusyAction] = useState<'approve' | 'reject' | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    try {
      const saved = localStorage.getItem(`sdlc:approval-persona:${activeAppId}`);
      setPersonaFilter(saved || 'all');
    } catch {
      setPersonaFilter('all');
    }
  }, [activeAppId]);

  const refresh = useCallback(async () => {
    try {
      const items = await getApprovalRequests(activeAppId);
      setRequests(items);
      setSelectedId((current) => items.some((item) => item.id === current)
        ? current
        : items[0]?.id ?? '');
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load approval requests.');
    } finally {
      setLoading(false);
    }
  }, [activeAppId]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 10000);
    return () => clearInterval(timer);
  }, [refresh]);

  const visibleRequests = useMemo(
    () => personaFilter === 'all'
      ? requests
      : requests.filter((item) => item.persona === personaFilter),
    [personaFilter, requests],
  );
  const selected = visibleRequests.find((item) => item.id === selectedId) ?? null;
  useEffect(() => {
    if (!visibleRequests.some((item) => item.id === selectedId)) {
      setSelectedId(visibleRequests[0]?.id ?? '');
    }
  }, [selectedId, visibleRequests]);
  const canDecide = selected?.kind === 'pull_request' ? can('maintainer') : can('runner');

  const act = async (action: 'approve' | 'reject') => {
    if (!selected) return;
    setBusyAction(action);
    setError(null);
    try {
      await actOnApprovalRequest(selected.id, action);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : `Could not ${action} this request.`);
    } finally {
      setBusyAction(null);
    }
  };

  const personaOptions = [...new Set(requests.map((item) => item.persona))].sort();
  const task = selected?.run.manual_task;
  const report = task?.report;
  const summary = task?.initial_values?.summary
    || task?.context?.fetch?.summary
    || task?.context?.gate?.summary;
  const description = task?.initial_values?.description
    || task?.context?.fetch?.description
    || task?.context?.gate?.description;

  return (
    <div className="bg-bg min-h-screen">
      <Nav />
      <div className="content">
        <PageHeader
          eyebrow="Human task inbox"
          title="Approvals"
          description="Review requests the workflow has paused for, then approve them to continue or reject them to stop."
        />

        <div className="approval-inbox-toolbar">
          <div>
            <strong>{visibleRequests.length}</strong>
            <span> pending request{visibleRequests.length === 1 ? '' : 's'}</span>
            {activeApp && <span className="chip purple">Application · {activeApp.name}</span>}
          </div>
          <label>
            <span>Persona</span>
            <select
              className="input-main"
              value={personaFilter}
              onChange={(event) => {
                const nextFilter = event.target.value;
                setPersonaFilter(nextFilter);
                try {
                  localStorage.setItem(
                    `sdlc:approval-persona:${activeAppId}`,
                    nextFilter,
                  );
                } catch {
                  setError('Could not save your persona filter preference.');
                }
              }}
            >
              <option value="all">All personas</option>
              {personaOptions.map((persona) => (
                <option key={persona} value={persona}>{persona}</option>
              ))}
            </select>
          </label>
        </div>

        {error && <div className="alert error" role="alert">{error}</div>}

        <div className="approval-inbox-layout">
          <section className="card-main approval-request-list" aria-label="Pending requests">
            {loading ? (
              <div className="empty-state">Loading approval requests…</div>
            ) : visibleRequests.length === 0 ? (
              <div className="approval-inbox-empty">
                <span aria-hidden="true">✓</span>
                <h2>{requests.length ? 'No requests for this persona' : 'You’re all caught up'}</h2>
                <p>
                  {requests.length
                    ? 'Choose another persona filter to see more requests.'
                    : 'Requests that need a human decision will appear here.'}
                </p>
              </div>
            ) : visibleRequests.map((item) => (
              <button
                key={item.id}
                type="button"
                className={`approval-request-row${item.id === selectedId ? ' selected' : ''}`}
                onClick={() => setSelectedId(item.id)}
              >
                <span className="approval-request-icon" aria-hidden="true">
                  {item.kind === 'pull_request' ? 'PR' : '•'}
                </span>
                <span className="approval-request-copy">
                  <strong>{item.run.ticket_key}</strong>
                  <span>{item.stage_label}</span>
                  <small>{item.persona} · {formatTime(item.requested_at)}</small>
                </span>
                <span className="approval-state">
                  {item.kind === 'pull_request' ? 'Approval' : 'Action needed'}
                </span>
              </button>
            ))}
          </section>

          <section className="card-main approval-request-detail" aria-label="Request details">
            {!selected ? (
              <div className="approval-inbox-empty compact">
                <h2>Select a request</h2>
                <p>Request details and available actions will appear here.</p>
              </div>
            ) : (
              <>
                <div className="approval-detail-head">
                  <div>
                    <span className="eyebrow">{selected.stage_label}</span>
                    <h2>{selected.run.ticket_key}</h2>
                    <p>
                      {activeApp?.name ?? 'Application'} · Persona: <b>{selected.persona}</b>
                    </p>
                  </div>
                  <Link
                    className="btn-secondary approval-open-run"
                    href={`/run?run=${encodeURIComponent(selected.run.id)}`}
                  >
                    Open run
                  </Link>
                </div>

                <div className="approval-detail-section">
                  <span className="run-review-label">Why it was raised</span>
                  <p>{selected.reason || 'The workflow paused for a human decision.'}</p>
                  <small>Requested {formatTime(selected.requested_at)}</small>
                </div>

                {(summary || description) && (
                  <div className="approval-detail-section">
                    <span className="run-review-label">Ticket context</span>
                    {summary && <strong>{summary}</strong>}
                    {description && <p className="approval-context-description">{description}</p>}
                  </div>
                )}

                {report && (
                  <div className="approval-detail-section">
                    <span className="run-review-label">Verification findings</span>
                    <p>{report.decision_reason || report.summary}</p>
                    {report.review_report && (
                      <pre className="manual-review-findings">{report.review_report}</pre>
                    )}
                  </div>
                )}

                <div className="approval-action-area">
                  <div className="approval-action-buttons">
                    <button
                      className="btn-primary"
                      type="button"
                      disabled={!canDecide || !!busyAction}
                      onClick={() => void act('approve')}
                    >
                      {busyAction === 'approve' ? 'Approving…' : 'Approve & continue'}
                    </button>
                    <button
                      className="btn-danger"
                      type="button"
                      disabled={!canDecide || !!busyAction}
                      onClick={() => void act('reject')}
                    >
                      {busyAction === 'reject' ? 'Rejecting…' : 'Reject & stop'}
                    </button>
                  </div>
                  {!canDecide && (
                    <p className="locked-note">
                      🔒 {selected.kind === 'pull_request'
                        ? 'A maintainer or admin must decide this pull request approval.'
                        : `A runner or higher must decide this request. Your role is ${me?.role ?? 'unknown'}.`}
                    </p>
                  )}
                </div>
              </>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
