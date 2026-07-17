'use client';

import { useEffect, useState, useCallback } from 'react';
import Link from 'next/link';
import Nav from '@/components/Nav';
import StatCard from '@/components/StatCard';
import StatusBadge from '@/components/StatusBadge';
import { getStats, getRuns, formatDuration, formatTime, type RunStats, type RunRecord } from '@/lib/api';

function Sparkline({ data }: { data: Array<{ date: string; success: number; failed: number }> }) {
  const W = 220, H = 48, PAD = 4;
  const maxVal = Math.max(...data.map(d => d.success + d.failed), 1);
  const n = data.length;
  const pts = data.map((d, i) => ({
    x: PAD + (i / (n - 1)) * (W - PAD * 2),
    y: H - PAD - (d.success / maxVal) * (H - PAD * 2),
  }));
  const failPts = data.map((d, i) => ({
    x: PAD + (i / (n - 1)) * (W - PAD * 2),
    y: H - PAD - ((d.success + d.failed) / maxVal) * (H - PAD * 2),
  }));
  const successPath = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
  const failPath    = failPts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
  return (
    <svg width={W} height={H} style={{ display: 'block', overflow: 'visible' }}>
      <path d={failPath}    fill="none" stroke="#dc2626"               strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" opacity="0.5" />
      <path d={successPath} fill="none" stroke="var(--color-accent-purple)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      {pts.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r="2.5" fill="var(--color-accent-purple)" />
      ))}
    </svg>
  );
}

function computeTrend(daily: RunStats['daily'], key: 'success' | 'failed'): { value: number; direction: 'up' | 'down' } {
  const first = daily.slice(0, 3).reduce((a, d) => a + d[key], 0);
  const last  = daily.slice(-3).reduce((a, d) => a + d[key], 0);
  const dir = last >= first ? 'up' : 'down';
  const val = first > 0 ? Math.abs(Math.round(((last - first) / first) * 100)) : 0;
  return { value: val, direction: dir };
}

export default function DashboardPage() {
  const [stats, setStats] = useState<RunStats | null>(null);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([getStats(), getRuns()]);
      setStats(s); setRuns(r);
      setLastRefreshed(Date.now());
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    refresh().finally(() => setLoading(false));
    const tid = setInterval(refresh, 30_000);
    return () => clearInterval(tid);
  }, [refresh]);

  const successTrend = stats ? computeTrend(stats.daily, 'success') : undefined;
  const failedTrend  = stats ? computeTrend(stats.daily, 'failed')  : undefined;

  return (
    <div className="bg-bg min-h-screen">
      <div className="bg-animation">
        <div className="bg-grid" />
        <div className="bg-orb bg-orb-1" />
        <div className="bg-orb bg-orb-2" />
        <div className="bg-orb bg-orb-3" />
      </div>

      <Nav />

      <div className="content" style={{ maxWidth: 1200, margin: '0 auto', padding: '84px 32px 48px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 32, flexWrap: 'wrap' }}>
          <h1 style={{ fontSize: 28, fontWeight: 800, letterSpacing: '-0.01em' }}>Dashboard</h1>
          <div style={{ flex: 1 }} />
          {lastRefreshed && (
            <span style={{ fontSize: 12, color: 'var(--color-text-muted)' }}>
              Updated {new Date(lastRefreshed).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            </span>
          )}
          <Link href="/run" className="btn-primary" style={{ padding: '10px 20px', fontSize: 13 }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <polygon points="5 3 19 12 5 21 5 3" />
            </svg>
            Run Pipeline
          </Link>
        </div>

        {loading ? (
          <div style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>Loading...</div>
        ) : error ? (
          <div style={{ color: 'var(--color-red)', fontSize: 14 }}>Error: {error}</div>
        ) : (
          <>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16, marginBottom: 32 }}>
              <StatCard label="Total Runs"   value={String(stats?.total ?? 0)} />
              <StatCard label="Success"      value={String(stats?.success ?? 0)} accent="var(--color-green)" trend={successTrend} />
              <StatCard label="Failed"       value={String(stats?.failed ?? 0)}  accent="var(--color-red)"   trend={failedTrend ? { ...failedTrend, direction: failedTrend.direction === 'up' ? 'down' : 'up' } : undefined} />
              <StatCard label="Needs Human"  value={String(stats?.human ?? 0)}   accent="#d97706" />
            </div>

            {stats?.daily && (
              <div className="card-main" style={{ padding: '20px 24px', marginBottom: 32 }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16, flexWrap: 'wrap', gap: 12 }}>
                  <div>
                    <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 2 }}>7-day trend</div>
                    <div style={{ fontSize: 12, color: 'var(--color-text-muted)' }}>
                      {stats.success_rate}% success rate ·{' '}
                      <span style={{ color: 'var(--color-accent-purple)', fontFamily: 'var(--font-mono)' }}>
                        {formatDuration(stats.avg_duration_sec)}
                      </span>
                      {' '}avg
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: 20, fontSize: 12 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ width: 10, height: 3, background: 'var(--color-accent-purple)', borderRadius: 2, display: 'inline-block' }} />
                      <span style={{ color: 'var(--color-text-muted)' }}>Success</span>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ width: 10, height: 3, background: '#dc2626', opacity: .5, borderRadius: 2, display: 'inline-block' }} />
                      <span style={{ color: 'var(--color-text-muted)' }}>Failed</span>
                    </div>
                  </div>
                </div>
                <div>
                  <Sparkline data={stats.daily} />
                  <div style={{ display: 'flex', justifyContent: 'space-between', width: 220, marginTop: 4 }}>
                    {stats.daily.map((d, i) => (
                      <div key={i} style={{ fontSize: 10, color: 'var(--color-text-muted)', textAlign: 'center' }}>
                        {d.date}
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

            <div className="card-main" style={{ overflow: 'hidden' }}>
              <div style={{ padding: '20px 24px 12px' }}>
                <h2 style={{ fontSize: 18, fontWeight: 700 }}>Recent Runs</h2>
              </div>

              {runs.length === 0 ? (
                <div style={{ padding: '0 24px 24px', color: 'var(--color-text-muted)', fontSize: 14 }}>
                  No runs yet.{' '}
                  <Link href="/run" style={{ color: 'var(--color-accent-purple)', textDecoration: 'underline' }}>
                    Launch your first pipeline
                  </Link>
                </div>
              ) : (
                <div style={{ overflowX: 'auto' }}>
                  <table className="table-main">
                    <thead>
                      <tr>
                        <th>Ticket</th>
                        <th>Status</th>
                        <th>PR</th>
                        <th>Started</th>
                        <th>Duration</th>
                      </tr>
                    </thead>
                    <tbody>
                      {runs.map((run) => (
                        <tr key={run.id}>
                          <td style={{ fontWeight: 600 }}>
                            <Link href={`/logs?run=${run.id}`} className="nav-link" style={{ padding: 0 }}>
                              {run.ticket_key}
                            </Link>
                          </td>
                          <td><StatusBadge status={run.status} /></td>
                          <td>
                            {run.pr_url ? (
                              <a href={run.pr_url} target="_blank" rel="noopener noreferrer"
                                style={{ color: 'var(--color-accent-purple)', fontSize: 13, textDecoration: 'none' }}>
                                PR #{run.pr_url.split('/').pop()}
                              </a>
                            ) : <span style={{ color: 'var(--color-text-muted)', fontSize: 13 }}>—</span>}
                          </td>
                          <td style={{ color: 'var(--color-text-secondary)', fontSize: 13 }}>{formatTime(run.started_at)}</td>
                          <td style={{ color: 'var(--color-text-secondary)', fontSize: 13 }}>{formatDuration(run.duration_sec)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
