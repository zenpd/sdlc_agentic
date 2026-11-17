'use client';

import Link from 'next/link';
import { approvalWaitSec, formatDuration, formatTime, type RunRecord } from '@/lib/api';

function changedFiles(run: RunRecord): { stat: string; files: string[] } {
  const line = [...(run.logs.pr ?? [])].reverse().find((entry) => entry.startsWith('[APPROVAL]')) ?? '';
  const match = line.match(/—\s*(.*?):\s*(.*)$/);
  if (match) return {
    stat: match[1],
    files: match[2].split(',').map((file) => file.trim()).filter(Boolean),
  };
  const modified = (run.logs.agent ?? []).find((entry) => entry.startsWith('Modified:'));
  return {
    stat: '',
    files: modified
      ? modified.replace('Modified:', '').split(',').map((file) => file.trim())
      : [],
  };
}

/** Shows the pending PR decision without duplicating actions outside the inbox. */
export default function ApprovalPanel({ run }: { run: RunRecord }) {
  const approval = run.approval;
  if (!approval) return null;

  if (approval.status !== 'pending') {
    const label = approval.status === 'approved'
      ? '✓ Approved'
      : approval.status === 'rejected' ? '✕ Rejected' : '⏱ Approval expired';
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
  return (
    <div className="approval-panel">
      <h3>🛡️ Approval needed before a pull request is opened</h3>
      <p>
        {run.workflow_mode === 'manual'
          ? 'Independent verification passed. A maintainer must approve before the Manual PR stage continues.'
          : 'The agent finished and verified its change. A maintainer must approve before anything is pushed.'}
        {' '}{run.triggered_by && <>Started by @{run.triggered_by}.</>}
      </p>
      {approvalWaitSec(run) != null && (
        <p className="approval-wait">
          ⏳ Waiting {formatDuration(approvalWaitSec(run))}. The pipeline is paused until a reviewer decides.
        </p>
      )}
      {files.length > 0 && (
        <div className="approval-files">
          {stat && <span style={{ fontSize: 12, fontWeight: 700, marginRight: 4 }}>{stat}:</span>}
          {files.map((file) => <code key={file}>{file}</code>)}
        </div>
      )}
      <Link className="btn-primary approval-inbox-link" href="/approvals">
        Review in Approvals inbox
      </Link>
    </div>
  );
}
