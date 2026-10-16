export type RunStatus = 'pending' | 'in-progress' | 'awaiting-approval' | 'success' | 'failed' | 'error' | 'human';

export default function StatusBadge({ status }: { status: string }) {
  const s = status as RunStatus;
  const dot = <span style={{ width: 5, height: 5, borderRadius: '50%', display: 'inline-block', background: 'currentColor' }} />;

  switch (s) {
    case 'success':
      return <span className="badge success">{dot} Success</span>;
    case 'failed':
    case 'error':
      return <span className="badge failed">{dot} {s === 'error' ? 'Error' : 'Failed'}</span>;
    case 'human':
      return <span className="badge human">{dot} Human In Loop</span>;
    case 'awaiting-approval':
      return <span className="badge approval">{dot} Awaiting approval</span>;
    case 'in-progress':
      return <span className="badge in-progress">{dot} Running</span>;
    default:
      return <span className="badge pending">{dot} Pending</span>;
  }
}
