export default function StatCard({
  label,
  value,
  accent,
  trend,
}: {
  label: string;
  value: string;
  accent?: string;
  trend?: { value: number; direction: 'up' | 'down' };
}) {
  return (
    <div
      style={{
        background: 'var(--color-surface)',
        border: '1px solid var(--color-border)',
        borderRadius: 14,
        padding: '20px 24px',
        boxShadow: '0 1px 3px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.04)',
      }}
    >
      <div
        style={{
          fontSize: 12,
          fontWeight: 600,
          color: 'var(--color-text-muted)',
          textTransform: 'uppercase',
          letterSpacing: '0.6px',
          marginBottom: 6,
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontSize: 30,
          fontWeight: 800,
          color: accent ?? 'var(--color-text)',
          fontFamily: 'var(--font-mono)',
          lineHeight: 1.1,
          marginBottom: trend ? 8 : 0,
        }}
      >
        {value}
      </div>
      {trend && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12, fontWeight: 600 }}>
          <span style={{ color: trend.direction === 'up' ? 'var(--color-green)' : 'var(--color-red)' }}>
            {trend.direction === 'up' ? '↑' : '↓'} {Math.abs(trend.value)}%
          </span>
          <span style={{ color: 'var(--color-text-muted)', fontWeight: 400 }}>vs prev 3d</span>
        </div>
      )}
    </div>
  );
}
