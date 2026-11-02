import type { ReactNode } from 'react';

type Tone = 'blue' | 'teal' | 'pink' | 'green' | 'amber' | 'red';

const ICONS: Record<string, ReactNode> = {
  runs: <path d="M4 19V9M10 19V5M16 19v-7M22 19V3" />,
  check: <polyline points="20 6 9 17 4 12" />,
  x: <><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></>,
  human: <><circle cx="12" cy="7" r="4" /><path d="M5.5 21a6.5 6.5 0 0 1 13 0" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><polyline points="12 7 12 12 15 14" /></>,
  gauge: <><path d="M12 14l4-4" /><path d="M3.3 17a9 9 0 1 1 17.4 0" /></>,
  dollar: <><path d="M12 2v20" /><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" /></>,
};

export default function StatCard({
  label, value, tone = 'blue', icon = 'runs', hint, trend, children,
}: {
  label: string;
  value: string;
  tone?: Tone;
  icon?: keyof typeof ICONS | string;
  hint?: ReactNode;
  accent?: string;
  trend?: { value: number; direction: 'up' | 'down'; good?: boolean };
  children?: ReactNode;
}) {
  const good = trend ? (trend.good ?? trend.direction === 'up') : true;
  return (
    <article className="metric">
      <div className="metric-top">
        <span className={`metric-icon ${tone === 'blue' ? '' : tone}`}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            {ICONS[icon] ?? ICONS.runs}
          </svg>
        </span>
        {label}
      </div>
      <strong>{value}</strong>
      {trend ? (
        <small>
          <em className={good ? 'trend-up' : 'trend-down'}>
            {trend.direction === 'up' ? '↗' : '↘'} {Math.abs(trend.value)}%
          </em>{' '}
          vs previous 3 days
        </small>
      ) : hint ? (
        <small>{hint}</small>
      ) : null}
      {children}
    </article>
  );
}
