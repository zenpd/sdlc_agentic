export type StageState = 'pending' | 'in-progress' | 'success' | 'failed' | 'error' | 'human';

type Stage = { key: string; label: string };

const DOT = 24;

const STATE_COLOR: Record<StageState, string> = {
  pending: 'var(--color-border)',
  'in-progress': 'var(--color-accent-purple)',
  success: 'var(--color-green)',
  failed: 'var(--color-red)',
  error: 'var(--color-red)',
  human: '#d97706',
};

function StageIcon({ state }: { state: StageState }) {
  if (state === 'success') {
    return (
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="20 6 9 17 4 12" pathLength={1} style={{ strokeDasharray: 1, strokeDashoffset: 0, animation: 'stageCheckDraw .4s ease-out' }} />
      </svg>
    );
  }
  if (state === 'failed' || state === 'error') {
    return (
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round">
        <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
      </svg>
    );
  }
  if (state === 'human') {
    return (
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
        <line x1="12" y1="7" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
    );
  }
  return null;
}

export default function StageProgress({ stages, states }: { stages: Stage[]; states: StageState[] }) {
  const n = stages.length;
  const colWidth = 100 / n;

  return (
    <div style={{ overflowX: 'auto', paddingBottom: 2 }}>
      <div style={{ position: 'relative', minWidth: 560, padding: `${DOT / 2}px 0 0` }}>
        {/* base track, spans dot-center to dot-center */}
        <div
          style={{
            position: 'absolute',
            top: DOT / 2 - 1,
            left: `${colWidth / 2}%`,
            right: `${colWidth / 2}%`,
            height: 2,
            borderRadius: 1,
            background: 'var(--color-border)',
          }}
        />

        {/* filled + animated segments, one per gap between stages */}
        {stages.slice(0, -1).map((stage, i) => {
          const s = states[i] || 'pending';
          const isPending = s === 'pending';
          const isActive = s === 'in-progress';
          const color = STATE_COLOR[s];
          const fill = isPending ? 0 : isActive ? 0.5 : 1;

          return (
            <div
              key={`seg-${stage.key}`}
              style={{
                position: 'absolute',
                top: DOT / 2 - 1,
                left: `${(i + 0.5) * colWidth}%`,
                width: `${fill * colWidth}%`,
                height: 2,
                borderRadius: 1,
                backgroundColor: isActive ? undefined : color,
                backgroundImage: isActive ? `linear-gradient(90deg, ${color}, #a78bfa, ${color})` : 'none',
                backgroundSize: isActive ? '200% 100%' : '100% 100%',
                animation: isActive ? 'stageFlow 1.1s linear infinite' : 'none',
                transition: 'width .55s cubic-bezier(.4,0,.2,1), background-color .3s',
              }}
            />
          );
        })}

        {/* stage dots + labels */}
        <div style={{ display: 'flex', position: 'relative' }}>
          {stages.map((stage, i) => {
            const state: StageState = states[i] || 'pending';
            const color = STATE_COLOR[state];
            const isFilled = state !== 'pending' && state !== 'in-progress';
            const isActive = state === 'in-progress';

            return (
              <div
                key={stage.key}
                style={{
                  flex: 1,
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  gap: 10,
                  minWidth: 0,
                }}
              >
                <div
                  style={{
                    width: DOT,
                    height: DOT,
                    borderRadius: '50%',
                    border: `2px solid ${color}`,
                    background: isFilled ? color : 'var(--color-surface)',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    flexShrink: 0,
                    boxShadow: isActive ? `0 0 0 5px ${color}26` : isFilled ? `0 0 0 3px ${color}14` : 'none',
                    animation: isActive ? 'stagePulseRing 1.4s ease-in-out infinite, stageBreathe 1.4s ease-in-out infinite' : 'none',
                    transition: 'background .3s ease, border-color .3s ease, box-shadow .3s ease',
                  }}
                >
                  <StageIcon state={state} />
                </div>
                <div
                  style={{
                    fontSize: 10.5,
                    fontWeight: isActive ? 700 : 500,
                    color: isActive ? color : state === 'pending' ? 'var(--color-text-muted)' : 'var(--color-text-secondary)',
                    textAlign: 'center',
                    lineHeight: 1.3,
                    maxWidth: Math.max(64, colWidth * 1.05) + '%',
                    padding: '0 2px',
                    transition: 'color .3s ease',
                  }}
                >
                  {stage.label}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <style>{`
        @keyframes stagePulseRing {
          0%, 100% { box-shadow: 0 0 0 5px rgba(139,92,246,.22); }
          50% { box-shadow: 0 0 0 9px rgba(139,92,246,.06); }
        }
        @keyframes stageBreathe {
          0%, 100% { transform: scale(1); }
          50% { transform: scale(1.08); }
        }
        @keyframes stageFlow {
          0% { background-position: 0% 0%; }
          100% { background-position: 200% 0%; }
        }
        @keyframes stageCheckDraw {
          from { stroke-dashoffset: 1; }
          to { stroke-dashoffset: 0; }
        }
      `}</style>
    </div>
  );
}
