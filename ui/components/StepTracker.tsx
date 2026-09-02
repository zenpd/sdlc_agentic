export type StepState = 'pending' | 'in-progress' | 'success' | 'failed' | 'error' | 'human';

export type Step = {
  key: string;
  label: string;
};

const DEFAULT_STEPS: Step[] = [
  { key: 'fetch', label: 'Fetch Ticket' },
  { key: 'gate', label: 'Clarity Gate' },
  { key: 'agent', label: 'Agent Run' },
  { key: 'verify', label: 'Verify' },
  { key: 'pr', label: 'Create PR' },
  { key: 'report', label: 'Report' },
];

// props for use in Run / Logs pages (data-driven)
type ListProps = {
  steps: Step[];
  states: StepState[];
  logs?: Record<string, string[]>;
};

// props for use in sidebar / mini tracker (index-based)
type IndexProps = {
  current: number;
};

export default function StepTracker(props: ListProps | IndexProps) {
  if ('current' in props) {
    return <IndexTracker current={props.current} />;
  }
  return <ListTracker steps={props.steps} states={props.states} logs={props.logs} />;
}

function IndexTracker({ current }: { current: number }) {
  return (
    <div className="steps">
      {DEFAULT_STEPS.map(({ label }, i) => {
        const state = i < current ? 'done' : i === current ? 'active' : '';
        return (
          <div key={label}>
            <div className={`step-item-dash ${state}`}>
              <div className="step-dot">
                {i < current ? (
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                ) : null}
              </div>
              <div className="step-label">
                {label}
              </div>
            </div>
            {i < DEFAULT_STEPS.length - 1 && <div className="step-line" />}
          </div>
        );
      })}
    </div>
  );
}

function LogLine({ text, type }: { text: string; type?: string }) {
  const cls = type === 'success' ? 'success' : type === 'error' ? 'error' : type === 'info' ? 'info' : '';
  const style = type === 'human' ? { color: '#d97706' } : undefined;
  return <div className={`log-line-dash ${cls}`} style={style}>{text}</div>;
}

function ListTracker({ steps, states, logs }: ListProps) {
  return (
    <div className="steps">
      {steps.map(({ key, label }, i) => {
        const s = states[i] || 'pending';
        const state = s === 'success' ? 'done' : s === 'failed' || s === 'error' || s === 'human' ? 'done' : s === 'in-progress' ? 'active' : '';
        const isActive = s === 'in-progress';
        const isHuman = s === 'human';
        const isFailed = s === 'failed' || s === 'error';
        const dotBg = state === 'done'
          ? { background: isFailed ? 'var(--color-red)' : isHuman ? '#d97706' : 'var(--color-accent-purple)', borderColor: isFailed ? 'var(--color-red)' : isHuman ? '#d97706' : 'var(--color-accent-purple)' }
          : isActive ? { borderColor: 'var(--color-accent-purple)' } : {};

        return (
          <div key={key}>
            <div className={`step-item-dash ${state} ${isActive ? 'active' : ''}`} style={{ opacity: s === 'pending' ? 0.45 : 1 }}>
              <div className="step-dot" style={dotBg}>
                {s === 'success' ? (
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                ) : isFailed ? (
                  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                  </svg>
                ) : isHuman ? (
                  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="12" y1="7" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
                  </svg>
                ) : null}
              </div>
              <div style={{ flex: 1 }}>
                <div
                  className="step-label"
                  style={isActive ? { color: 'var(--color-accent-purple)' } : isHuman ? { color: '#d97706', fontWeight: 600 } : state === 'done' ? { opacity: 0.7 } : {}}
                >
                  {label}
                  {isHuman && <span style={{ marginLeft: 6, fontSize: 10, fontWeight: 700, letterSpacing: '.04em' }}>· HUMAN REVIEW NEEDED</span>}
                </div>
                {logs?.[key] && (
                  <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 2 }}>
                    {logs[key].map((t, j) => (
                      <LogLine key={j} text={t} type={isHuman ? 'human' : undefined} />
                    ))}
                  </div>
                )}
              </div>
            </div>
            {i < steps.length - 1 && <div className="step-line" />}
          </div>
        );
      })}
    </div>
  );
}
