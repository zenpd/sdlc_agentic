'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { getGithubUser, GITHUB_LOGIN_PATH } from '@/lib/api';

function GithubIcon({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 .3a12 12 0 0 0-3.79 23.4c.6.1.82-.26.82-.58v-2.17c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.84 2.81 1.3 3.5 1 .1-.78.42-1.31.76-1.61-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 6 0c2.29-1.55 3.3-1.23 3.3-1.23.66 1.66.24 2.88.12 3.18.77.84 1.24 1.91 1.24 3.22 0 4.61-2.81 5.62-5.49 5.92.43.37.81 1.1.81 2.22v3.29c0 .32.22.69.83.58A12 12 0 0 0 12 .3Z" />
    </svg>
  );
}

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
    <div className="auth-page bg-bg" style={{ position: 'relative', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div className="bg-animation">
        <div className="bg-grid" />
        <div className="bg-orb bg-orb-1" />
        <div className="bg-orb bg-orb-2" />
        <div className="bg-orb bg-orb-3" />
      </div>

      <div style={{ width: '92%', maxWidth: 400, position: 'relative', zIndex: 1 }}>
        <div
          className="card-main"
          style={{
            padding: '44px 40px 36px',
            textAlign: 'center',
            opacity: checking ? 0 : 1,
            transform: checking ? 'translateY(6px)' : 'translateY(0)',
            transition: 'opacity .4s ease, transform .4s ease',
          }}
        >
          <div
            style={{
              width: 48, height: 48, borderRadius: 12,
              background: 'linear-gradient(135deg, var(--color-accent-purple), var(--color-accent-violet))',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              margin: '0 auto 20px',
              boxShadow: '0 4px 12px rgba(99,102,241,.3)',
            }}
          >
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
            </svg>
          </div>

          <h1 style={{ fontSize: 24, fontWeight: 800, letterSpacing: '-0.01em', marginBottom: 6, color: 'var(--color-text)' }}>
            Welcome to SDLC Automation
          </h1>
          <p style={{ fontSize: 13, color: 'var(--color-text-secondary)', marginBottom: 32 }}>
            Sign in with GitHub to get started — that&apos;s the only step.
          </p>

          <a
            href={GITHUB_LOGIN_PATH}
            className="btn-primary"
            style={{
              display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10,
              width: '100%', padding: '13px 20px', fontSize: 14, textDecoration: 'none',
              background: '#171b21',
            }}
          >
            <GithubIcon size={18} /> Login with GitHub
          </a>

          <p style={{ fontSize: 11.5, color: 'var(--color-text-muted)', marginTop: 22, lineHeight: 1.5 }}>
            We only ask for repository access so the agent can work on the projects you choose.
          </p>
        </div>
      </div>
    </div>
  );
}
