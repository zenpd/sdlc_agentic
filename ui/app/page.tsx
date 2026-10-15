'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import Nav from '@/components/Nav';
import StatCard from '@/components/StatCard';
import StatusBadge from '@/components/StatusBadge';
import { useActiveApp } from '@/components/AppContext';
import {
  getStats, getRuns, getAppConfig, formatDuration, formatTime,
  type RunStats, type RunRecord, type AppConfig,
} from '@/lib/api';

const ticketLabel = (key: string) => (/^\d+$/.test(key) ? `#${key}` : key);

function parseFetch(run: RunRecord): { summary: string | null; persona: string | null } {
  const line = run.logs.fetch?.[0] ?? '';
  const m = line.match(/^\S+:\s*(.*?)\s*\(persona:\s*([\w-]+)\)\s*$/);
  return m ? { summary: m[1], persona: m[2] } : { summary: null, persona: null };
}

function previewLines(run: RunRecord | undefined): Array<{ cls: string; text: string }> {
  if (!run) {
    return [
      { cls: 'muted', text: '$ agent run --ticket 674' },
      { cls: 'ok', text: '✓ clarity gate passed' },
      { cls: 'ok', text: '✓ persona · backend-engineer' },
      { cls: 'ok', text: '✓ src/app.py  +20 −0' },
      { cls: 'run', text: '● opening pull request…' },
    ];
  }
  const state = (key: string) => run.steps.find((s) => s.key === key)?.state;
  const { persona } = parseFetch(run);
  const lines: Array<{ cls: string; text: string }> = [{ cls: 'muted', text: `$ agent run --ticket ${run.ticket_key}` }];
  const gate = state('gate');
  if (gate === 'success') lines.push({ cls: 'ok', text: '✓ clarity gate passed' });
  else if (gate === 'human') lines.push({ cls: 'run', text: '⚠ ticket needs more detail — handed to a human' });
  else if (gate === 'in-progress') lines.push({ cls: 'run', text: '● checking ticket clarity…' });
  if (persona) lines.push({ cls: 'ok', text: `✓ persona · ${persona}` });
  const agent = state('agent');
  if (agent === 'success') lines.push({ cls: 'ok', text: '✓ code change verified' });
  else if (agent === 'in-progress') lines.push({ cls: 'run', text: '● agent writing code…' });
  if (run.pr_url) lines.push({ cls: 'ok', text: `✓ PR #${run.pr_url.split('/').pop()} opened` });
  else if (state('pr') === 'in-progress') lines.push({ cls: 'run', text: '● opening pull request…' });
  return lines;
}

function LivePreview({ lines }: { lines: Array<{ cls: string; text: string }> }) {
  const total = lines.length;
  const signature = lines.map((l) => l.text).join('|');
  const [shown, setShown] = useState(1);

  useEffect(() => {
    let n = 1;
    setShown(1);
    // Reveal one line per tick, hold the finished log for a few ticks, then replay.
    const t = setInterval(() => {
      n = n >= total + 4 ? 1 : n + 1;
      setShown(n);
    }, 750);
    return () => clearInterval(t);
  }, [signature, total]);

  const visible = Math.min(shown, total);
  return (
    <div className="hero-preview">
      <div className="preview-top">
        <span /><span /><span />
        <div className="preview-live"><i />LIVE</div>
      </div>
      <div className="preview-screen">
        {lines.slice(0, visible).map((l, i) => (
          <div key={`${i}-${l.text}`} className={`preview-line ${l.cls}`}>
            {l.text}
            {i === visible - 1 && <span className="preview-cursor" />}
          </div>
        ))}
      </div>
      <div className="preview-timeline"><i style={{ width: `${(visible / total) * 100}%` }} /></div>
    </div>
  );
}

export default function DashboardPage() {
  const [stats, setStats] = useState<RunStats | null>(null);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [appConfig, setAppConfig] = useState<AppConfig | null>(null);
  const [backendUp, setBackendUp] = useState<boolean | null>(null);
  const { activeApp, activeAppId } = useActiveApp();

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [s, r] = await Promise.all([getStats(activeAppId), getRuns(activeAppId)]);
        if (cancelled) return;
        setStats(s); setRuns(r); setBackendUp(true);
      } catch {
        if (!cancelled) setBackendUp(false);
      }
    };
    load();
    getAppConfig(activeAppId).then((c) => { if (!cancelled) setAppConfig(c); }).catch(() => {});
    const t = setInterval(load, 15000);
    return () => { cancelled = true; clearInterval(t); };
  }, [activeAppId]);

  const maxDaily = Math.max(1, ...(stats?.daily ?? []).map((d) => d.success + d.failed));
  const weekTotal = (stats?.daily ?? []).reduce((a, d) => a + d.success + d.failed, 0);
  const successRate = stats?.success_rate ?? 0;
  const latest = runs[0];
  const config = appConfig?.effective;
  const secrets = appConfig?.secrets;
  const providerKey = config ? config.TICKET_PROVIDER || 'jira' : '';
  const provider = providerKey === 'ado' ? 'Azure DevOps' : providerKey === 'jira' ? 'Jira' : '—';
  const ticketSecretSet = providerKey === 'ado' ? secrets?.ADO_PAT.set : secrets?.JIRA_API_TOKEN.set;
  const fullyConfigured = !!(config && config.TARGET_REPO && secrets?.GITHUB_TOKEN.set && secrets?.AZURE_OPENAI_API_KEY.set && ticketSecretSet);

  return (
    <div className="bg-bg min-h-screen">
      <Nav />

      <div className="content page-stack">
        <section className="hero hero-compact">
          <div className="hero-copy">
            <div className="hero-pill">⚡ LIVE &nbsp; Autonomous SDLC agent is ready</div>
            <h2 className="tagline">From issue to <span className="gradient-word">merge-ready code.</span></h2>
            <p>
              Point it at a Jira or Azure DevOps ticket. The agent checks the ticket is clear enough, picks the right
              persona, writes the change in your repository, and opens a pull request — with every step visible live.
            </p>
            <div className="hero-actions">
              <Link href="/run" className="hero-btn-primary">▷ Launch pipeline</Link>
              <Link href="/logs" className="hero-btn-secondary">View logs</Link>
            </div>
          </div>
          <LivePreview lines={previewLines(latest)} />
        </section>

        <section className="metrics">
          <StatCard
            label="Total runs" value={stats ? String(stats.total) : '—'} icon="runs"
            hint={stats && weekTotal === 0 ? 'No runs in the last 7 days' : `${weekTotal} in the last 7 days`}
          >
            {weekTotal > 0 && (
              <div className="mini-bars">
                {(stats?.daily ?? []).map((d, i) => (
                  <i key={i} title={`${d.date}: ${d.success + d.failed}`} style={{ height: `${Math.max(12, ((d.success + d.failed) / maxDaily) * 100)}%` }} />
                ))}
              </div>
            )}
          </StatCard>
          <StatCard
            label="Success rate" value={stats ? `${successRate}%` : '—'} tone="teal" icon="gauge"
            hint={stats ? `${stats.success} of ${stats.total} runs shipped a PR` : 'Runs that opened a PR'}
          >
            <div className="gauge"><i style={{ width: `${successRate}%` }} /></div>
          </StatCard>
          <StatCard label="Needs human" value={stats ? String(stats.human) : '—'} tone="amber" icon="human" hint="Tickets handed back for clarification" />
          <StatCard label="Avg. delivery" value={stats ? formatDuration(stats.avg_duration_sec) : '—'} tone="pink" icon="clock" hint="Ticket → pull request" />
          <StatCard
            label="Estimated LLM cost this month"
            value={stats?.monthly_cost_usd == null ? '—' : `$${stats.monthly_cost_usd.toFixed(5)}`}
            tone="green"
            icon="dollar"
            hint={stats
              ? `${stats.monthly_prompt_tokens.toLocaleString()} input · ${stats.monthly_completion_tokens.toLocaleString()} output tokens${stats.monthly_incomplete_cost_runs ? ` · ${stats.monthly_incomplete_cost_runs} run(s) with incomplete pricing` : ''}`
              : 'Estimated from LiteLLM model pricing'}
          />
        </section>

        <section className="feature-strip">
          <Link href="/run" className="feature-card featured">
            <div className="spark" />
            <div className="feature-icon">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polygon points="6 4 20 12 6 20 6 4" /></svg>
            </div>
            <h3>Run a ticket</h3>
            <p>Trigger the full pipeline for one ticket and watch the agent work live, down to the line.</p>
            <span className="feature-arrow">→</span>
          </Link>
          <Link href="/bulk" className="feature-card">
            <div className="feature-icon">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="5" height="16" rx="1" /><rect x="10" y="4" width="5" height="16" rx="1" /><path d="m17 5 4 14" /></svg>
            </div>
            <h3>Bulk request</h3>
            <p>Queue many tickets and run them all in parallel, each in its own isolated workspace.</p>
            <span className="feature-arrow">→</span>
          </Link>
          <Link href="/skills" className="feature-card">
            <div className="feature-icon">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="8" r="6" /><polyline points="8.21 13.89 7 23 12 20 17 23 15.79 13.88" /></svg>
            </div>
            <h3>Persona skills</h3>
            <p>Tune how the backend, frontend, QA, security, and architect personas approach a ticket.</p>
            <span className="feature-arrow">→</span>
          </Link>
        </section>

        <section className="lower-grid">
          <article className="card-main panel">
            <div className="panel-head">
              <h3>Recent runs</h3>
              <Link href="/logs" className="link-btn">View all →</Link>
            </div>
            {runs.length === 0 ? (
              <div style={{ color: 'var(--color-text-muted)', fontSize: 13 }}>
                {backendUp === false ? 'Pipeline server is unreachable.' : 'No runs yet — launch your first pipeline.'}
              </div>
            ) : (
              runs.slice(0, 5).map((run) => {
                const { summary, persona } = parseFetch(run);
                return (
                  <Link key={run.id} href={`/logs?run=${run.id}`} className="row-item">
                    <div className="row-thumb">
                      <svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z" /></svg>
                    </div>
                    <div style={{ minWidth: 0 }}>
                      <b style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {ticketLabel(run.ticket_key)}{summary ? ` · ${summary}` : ''}
                      </b>
                      <span className="meta">
                        {persona ? `${persona} · ` : ''}{formatTime(run.started_at)} · {formatDuration(run.duration_sec)}
                      </span>
                    </div>
                    <StatusBadge status={run.status} />
                  </Link>
                );
              })
            )}
          </article>

          <aside className="card-main panel health-card">
            <div className="panel-head">
              <h3>Pipeline health</h3>
              <Link href="/config" className="link-btn">Configure</Link>
            </div>
            <div className={`tip-box${backendUp === false || (config && !fullyConfigured) ? ' warn' : ''}`}>
              <b>
                {backendUp === false
                  ? 'Pipeline server is unreachable'
                  : config && !fullyConfigured
                    ? 'Some integrations need attention'
                    : 'Everything is running smoothly'}
              </b>
              <p>
                {backendUp === false
                  ? 'Start the backend service to run tickets.'
                  : config && !fullyConfigured
                    ? 'Finish connecting your ticket provider, repository, or model in Config.'
                    : 'Ticket provider, repository, and model are connected and ready.'}
              </p>
            </div>
            <div style={{ marginTop: 14 }}>
              <div className="kv-row"><span>Application</span><b title={activeApp?.name}>{activeApp?.name ?? '—'}</b></div>
              <div className="kv-row"><span>Ticket provider</span><b>{provider}</b></div>
              <div className="kv-row"><span>Repository</span><b title={config?.TARGET_REPO}>{config?.TARGET_REPO || '—'}</b></div>
              <div className="kv-row"><span>Branch</span><b>{config?.TARGET_BRANCH || '—'}</b></div>
              <div className="kv-row"><span>Model</span><b>{config?.AZURE_OPENAI_DEPLOYMENT || '—'}</b></div>
            </div>
            <div className="progress-label"><span>Success rate</span><b>{stats ? `${successRate}%` : '—'}</b></div>
            <div className="progress-track"><i style={{ width: `${successRate}%` }} /></div>
          </aside>
        </section>
      </div>
    </div>
  );
}
