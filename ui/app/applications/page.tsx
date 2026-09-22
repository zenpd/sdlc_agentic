'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import Nav from '@/components/Nav';
import {
  getGithubUser, logoutGithub, getGithubRepos, selectGithubRepo,
  getConfig, GITHUB_LOGIN_PATH, type GithubUser, type GithubRepo,
} from '@/lib/api';

function GithubIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 .3a12 12 0 0 0-3.79 23.4c.6.1.82-.26.82-.58v-2.17c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.84 2.81 1.3 3.5 1 .1-.78.42-1.31.76-1.61-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 6 0c2.29-1.55 3.3-1.23 3.3-1.23.66 1.66.24 2.88.12 3.18.77.84 1.24 1.91 1.24 3.22 0 4.61-2.81 5.62-5.49 5.92.43.37.81 1.1.81 2.22v3.29c0 .32.22.69.83.58A12 12 0 0 0 12 .3Z" />
    </svg>
  );
}

function RepoCard({
  repo, active, onSelect, selecting,
}: { repo: GithubRepo; active: boolean; onSelect: () => void; selecting: boolean }) {
  const [hover, setHover] = useState(false);
  const clickable = !active && !selecting;

  return (
    <div
      className="card-main"
      onClick={clickable ? onSelect : undefined}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        padding: 18, display: 'flex', flexDirection: 'column', gap: 10,
        cursor: clickable ? 'pointer' : 'default',
        border: active ? '1px solid var(--color-accent-purple)' : hover ? '1px solid var(--color-accent-purple)' : undefined,
        background: active ? 'rgba(139,92,246,.06)' : undefined,
        boxShadow: active ? '0 0 0 1px var(--color-accent-purple)' : selecting ? 'none' : undefined,
        opacity: selecting ? 0.6 : 1,
        transition: 'border-color .15s, background .15s, box-shadow .15s',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 8 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 700, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {repo.full_name}
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--color-text-muted)', marginTop: 2 }}>
            {repo.private ? 'Private' : 'Public'} · branch {repo.default_branch}
          </div>
        </div>
        {active ? (
          <span style={{
            fontSize: 9.5, fontWeight: 700, letterSpacing: '.04em', padding: '3px 8px', borderRadius: 100,
            color: 'var(--color-accent-purple)', background: 'rgba(139,92,246,.12)', whiteSpace: 'nowrap', flexShrink: 0,
          }}>
            ACTIVE
          </span>
        ) : selecting ? (
          <span style={{ fontSize: 11, color: 'var(--color-text-muted)', whiteSpace: 'nowrap', flexShrink: 0 }}>
            Selecting…
          </span>
        ) : (
          <svg
            width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="var(--color-accent-purple)" strokeWidth="2.5"
            strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, opacity: hover ? 1 : 0, transition: 'opacity .15s' }}
          >
            <line x1="5" y1="12" x2="19" y2="12" /><polyline points="12 5 19 12 12 19" />
          </svg>
        )}
      </div>
      {repo.description && (
        <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {repo.description}
        </div>
      )}
    </div>
  );
}

export default function ApplicationsPage() {
  const router = useRouter();
  const [user, setUser] = useState<GithubUser | null | undefined>(undefined);
  const [repos, setRepos] = useState<GithubRepo[]>([]);
  const [activeRepo, setActiveRepo] = useState<string>('');
  const [loadingRepos, setLoadingRepos] = useState(false);
  const [selecting, setSelecting] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [u, cfg] = await Promise.all([getGithubUser(), getConfig().catch(() => null)]);
    setUser(u);
    setActiveRepo(cfg?.TARGET_REPO || '');
    if (u) {
      setLoadingRepos(true);
      try {
        setRepos(await getGithubRepos());
        setError(null);
      } catch (e: any) {
        setError(e.message);
      } finally {
        setLoadingRepos(false);
      }
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const handleSelect = async (repo: GithubRepo) => {
    setSelecting(repo.full_name);
    try {
      await selectGithubRepo(repo.full_name, repo.default_branch);
      setActiveRepo(repo.full_name);
      setError(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSelecting(null);
    }
  };

  const handleDisconnect = async () => {
    await logoutGithub();
    setUser(null);
    setRepos([]);
    router.push('/login');
  };

  const filteredRepos = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return repos;
    return repos.filter((r) => r.full_name.toLowerCase().includes(q) || r.description?.toLowerCase().includes(q));
  }, [repos, search]);

  return (
    <div className="bg-bg min-h-screen">
      <div className="bg-animation">
        <div className="bg-grid" />
        <div className="bg-orb bg-orb-1" />
        <div className="bg-orb bg-orb-2" />
        <div className="bg-orb bg-orb-3" />
      </div>

      <Nav />

      <div className="content" style={{ padding: '40px 32px 48px' }}>
        <div className="card-main" style={{ padding: '20px 24px', marginBottom: 24 }}>
          <h1 style={{ fontSize: 24, fontWeight: 800, letterSpacing: '-0.01em' }}>Applications</h1>
          <p style={{ fontSize: 13, color: 'var(--color-text-secondary)', marginTop: 4 }}>
            Connect your GitHub account and pick which repository the agent should work on — no manual token or repo name needed.
          </p>
        </div>

        {error && (
          <div style={{ marginBottom: 20, padding: '12px 16px', borderRadius: 10, background: 'rgba(220,38,38,.08)', color: 'var(--color-red)', fontSize: 13 }}>
            {error}
          </div>
        )}

        {user === undefined ? (
          <div style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>Loading...</div>
        ) : !user ? (
          <div className="card-main" style={{ padding: 40, textAlign: 'center' }}>
            <div style={{
              width: 52, height: 52, borderRadius: 14, background: 'var(--color-surface)',
              display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 18px',
              color: 'var(--color-text-primary)',
            }}>
              <GithubIcon size={26} />
            </div>
            <h2 style={{ fontSize: 17, fontWeight: 700, marginBottom: 6 }}>Connect your GitHub account</h2>
            <p style={{ fontSize: 13, color: 'var(--color-text-secondary)', maxWidth: 420, margin: '0 auto 20px' }}>
              Sign in with GitHub to see every repository you have access to, then pick one for the agent to work against.
            </p>
            <a
              href={GITHUB_LOGIN_PATH}
              className="btn-primary"
              style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '10px 20px', fontSize: 13.5, textDecoration: 'none' }}
            >
              <GithubIcon size={15} /> Login with GitHub
            </a>
          </div>
        ) : (
          <>
            <div className="card-main" style={{ padding: '16px 20px', marginBottom: 24, display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
              {user.avatar_url && (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={user.avatar_url} alt={user.login} width={32} height={32} style={{ borderRadius: '50%' }} />
              )}
              <div style={{ fontSize: 13.5 }}>
                Connected as <strong>@{user.login}</strong>
              </div>
              <div style={{ flex: 1 }} />
              <input
                className="input-main"
                placeholder="Search repositories…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                style={{ maxWidth: 260 }}
              />
              <button onClick={handleDisconnect} className="btn-secondary" style={{ padding: '8px 14px', fontSize: 12.5 }}>
                Disconnect
              </button>
            </div>

            {loadingRepos ? (
              <div style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>Loading repositories...</div>
            ) : filteredRepos.length === 0 ? (
              <div className="card-main" style={{ padding: 24, color: 'var(--color-text-muted)', fontSize: 14 }}>
                No repositories match that search.
              </div>
            ) : (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 14 }}>
                {filteredRepos.map((repo) => (
                  <RepoCard
                    key={repo.full_name}
                    repo={repo}
                    active={repo.full_name === activeRepo}
                    selecting={selecting === repo.full_name}
                    onSelect={() => handleSelect(repo)}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
