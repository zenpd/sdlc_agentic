'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { getGithubUser, GITHUB_LOGIN_PATH } from '@/lib/api';

function GithubIcon({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 .3a12 12 0 0 0-3.79 23.4c.6.1.82-.26.82-.58v-2.17c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.84 2.81 1.3 3.5 1 .1-.78.42-1.31.76-1.61-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 6 0c2.29-1.55 3.3-1.23 3.3-1.23.66 1.66.24 2.88.12 3.18.77.84 1.24 1.91 1.24 3.22 0 4.61-2.81 5.62-5.49 5.92.43.37.81 1.1.81 2.22v3.29c0 .32.22.69.83.58A12 12 0 0 0 12 .3Z" />
    </svg>
  );
}

const STAGES = ['Ticket', 'Clarity gate', 'Agent', 'Pull request'];

export default function LoginPage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    let cancelled = false;
    getGithubUser().then((user) => {
      if (!cancelled && user) router.replace('/');
      if (!cancelled) setChecking(false);
    });
    return () => { cancelled = true; };
  }, [router]);

  return (
    <div className="auth-page">
      <div className="auth-shell">
        <section className="auth-hero">
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, fontWeight: 750, fontSize: 18 }}>
            <span className="brand-mark">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
              </svg>
            </span>
            Agent SDLC
          </div>

          <div>
            <div className="hero-pill">⚡ AUTONOMOUS ENGINEERING</div>
            <h2 style={{ margin: '16px 0 12px', fontSize: 'clamp(32px, 3.4vw, 48px)', lineHeight: 1.06, letterSpacing: '-.045em', fontWeight: 780, maxWidth: 560 }}>
              From issue to <span className="gradient-word">merge-ready code.</span>
            </h2>
            <p style={{ maxWidth: 520, color: 'rgba(255,255,255,.87)', lineHeight: 1.65, fontSize: 14.5 }}>
              The agent reads your Jira or Azure DevOps ticket, checks it&apos;s clear enough to act on, writes the change
              in your repository, and opens a pull request — fully visible, step by step.
            </p>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 26 }}>
              {STAGES.map((s, i) => (
                <span key={s} style={{
                  display: 'inline-flex', alignItems: 'center', gap: 8, padding: '8px 12px', borderRadius: 999,
                  background: 'rgba(255,255,255,.13)', border: '1px solid rgba(255,255,255,.28)', fontSize: 12, fontWeight: 700,
                }}>
                  <span style={{
                    width: 18, height: 18, borderRadius: '50%', display: 'grid', placeItems: 'center',
                    background: '#fff', color: '#3d4fd8', fontSize: 10, fontWeight: 800,
                  }}>{i + 1}</span>
                  {s}
                </span>
              ))}
            </div>
          </div>

          <div style={{ fontSize: 12, color: 'rgba(255,255,255,.65)' }}>Jira · Azure DevOps · GitHub · Azure OpenAI</div>
        </section>

        <section className="auth-panel">
          <div
            className="card-main"
            style={{
              width: '100%', maxWidth: 420, padding: '40px 36px 32px', boxShadow: 'var(--shadow-md)',
              opacity: checking ? 0 : 1, transform: checking ? 'translateY(6px)' : 'none',
              transition: 'opacity .4s ease, transform .4s ease',
            }}
          >
            <div className="eyebrow">✦ Welcome</div>
            <h1 style={{ fontSize: 28, fontWeight: 780, letterSpacing: '-.035em', marginBottom: 8 }}>Sign in to continue</h1>
            <p style={{ fontSize: 14, color: 'var(--color-text-secondary)', lineHeight: 1.55, marginBottom: 28 }}>
              Sign in with GitHub — that&apos;s the only step. No tokens to create or paste.
            </p>

            <a
              href={GITHUB_LOGIN_PATH}
              className="btn-primary"
              style={{ width: '100%', minHeight: 48, fontSize: 15, background: '#171b21', boxShadow: '0 8px 20px rgba(23,27,33,.22)' }}
            >
              <GithubIcon /> Continue with GitHub
            </a>

            <div style={{
              marginTop: 22, padding: '12px 14px', borderRadius: 12, background: 'var(--color-surface-2)',
              fontSize: 12, color: 'var(--color-text-secondary)', lineHeight: 1.55,
            }}>
              We only request repository access so the agent can open pull requests on the projects you choose.
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
