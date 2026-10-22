'use client';

import { useState } from 'react';
import { useActiveApp } from '@/components/AppContext';
import { approvalWaitSec, decideApproval, formatDuration, formatTime, type RunRecord } from '@/lib/api';

// Files the pipeline listed when it paused: "[APPROVAL] Waiting ... — <stat>: a.py, b.py"
function changedFiles(run: RunRecord): { stat: string; files: string[] } {
  const line = [...(run.logs.pr ?? [])].reverse().find((l) => l.startsWith('[APPROVAL]')) ?? '';
  const m = line.match(/—\s*(.*?):\s*(.*)$/);
  if (m) return { stat: m[1], files: m[2].split(',').map((f) => f.trim()).filter(Boolean) };
  const modified = (run.logs.agent ?? []).find((l) => l.startsWith('Modified:'));
  return { stat: '', files: modified ? modified.replace('Modified:', '').split(',').map((f) => f.trim()) : [] };
}

/** Human sign-off before a PR opens, for apps with "Require approval before PR". */
export default function ApprovalPanel({ run, onDecided }: { run: RunRecord; onDecided?: (run: RunRecord) => void }) {
  const { me, can } = useActiveApp();
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const approval = run.approval;
  if (!approval) return null;

  if (approval.status !== 'pending') {
    const label = approval.status === 'approved' ? '✓ Approved' : approval.status === 'rejected' ? '✕ Rejected' : '⏱ Approval expired';
    return (
      <div className={`approval-result ${approval.status}`} style={{ marginBottom: 16 }}>
        <b>{label}</b>
        {approval.by && approval.status !== 'expired' && <> by @{approval.by}</>}
        {approval.at && <> · {formatTime(approval.at)}</>}
        {approval.note && <> — “{approval.note}”</>}
      </div>
    );
  }

  if (run.status !== 'awaiting-approval') return null;
  const { stat, files } = changedFiles(run);
  const canDecide = can('maintainer');

  const decide = async (decision: 'approve' | 'reject') => {
    setBusy(decision);
    setError(null);
    try {
      onDecided?.(await decideApproval(run.id, decision, note));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="approval-panel">
      <h3>🛡️ Approval needed before a pull request is opened</h3>
      <p>
        The agent finished and verified its change. This application requires a maintainer to sign off before anything
        is pushed. {run.triggered_by && <>Started by @{run.triggered_by}.</>}
      </p>
      {approvalWaitSec(run) != null && (
        <p className="approval-wait">
          ⏳ Waiting {formatDuration(approvalWaitSec(run))} for a decision. The pipeline isn&apos;t stuck — nothing runs
          until someone approves or rejects, and other runs aren&apos;t held up meanwhile.
        </p>
      )}
      {files.length > 0 && (
        <div className="approval-files">
          {stat && <span style={{ fontSize: 12, fontWeight: 700, marginRight: 4 }}>{stat}:</span>}
          {files.map((f) => <code key={f}>{f}</code>)}
        </div>
      )}
      {error && <div className="alert error" style={{ marginBottom: 10 }}>{error}</div>}
      {canDecide ? (
        <div className="approval-actions">
          <input className="input-main" placeholder="Note (optional) — shown on the ticket if rejected" value={note}
            onChange={(e) => setNote(e.target.value)} maxLength={500} />
          <button className="btn-danger" disabled={!!busy} onClick={() => decide('reject')}>
            {busy === 'reject' ? 'Rejecting…' : 'Reject'}
          </button>
          <button className="btn-primary" disabled={!!busy} onClick={() => decide('approve')}>
            {busy === 'approve' ? 'Approving…' : 'Approve & open PR'}
          </button>
        </div>
      ) : (
        <div className="locked-note">
          🔒 Waiting for a <b>maintainer or admin</b> to approve.{me && <> Your role is <b>{me.role}</b>.</>}
        </div>
      )}
    </div>
  );
}
