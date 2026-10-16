'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';
import { getStats, logoutGithub, updatePreferences, type Role } from '@/lib/api';
import { useActiveApp } from '@/components/AppContext';

export const appInitials = (name: string) =>
  name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join('').toUpperCase() || 'A';

const NAV_ITEMS: Array<{ label: string; href: string; icon: string; color: string; minRole?: Role }> = [
  { label: 'Dashboard', href: '/', icon: 'home', color: '#7c3aed' },
  { label: 'Applications', href: '/applications', icon: 'github', color: '#0ea5e9' },
  { label: 'Run', href: '/run', icon: 'play', color: '#10b981' },
  { label: 'Bulk Request', href: '/bulk', icon: 'tickets', color: '#f59e0b' },
  { label: 'Logs', href: '/logs', icon: 'file', color: '#3b82f6' },
  { label: 'Skills', href: '/skills', icon: 'award', color: '#ec4899' },
  { label: 'Config', href: '/config', icon: 'settings', color: '#f97316' },
  { label: 'Activity', href: '/activity', icon: 'activity', color: '#14b8a6', minRole: 'maintainer' },
  { label: 'Teams', href: '/teams', icon: 'teams', color: '#0891b2', minRole: 'admin' },
  { label: 'Users', href: '/users', icon: 'users', color: '#6366f1', minRole: 'admin' },
];

const STROKE = {
  viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 2, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
};

function NavIcon({ name }: { name: string }) {
  switch (name) {
    case 'home':
      return <svg {...STROKE}><path d="m3 11 9-8 9 8v10h-6v-6H9v6H3z" /></svg>;
    case 'github':
      return (
        <svg viewBox="0 0 24 24" fill="currentColor">
          <path d="M12 .3a12 12 0 0 0-3.79 23.4c.6.1.82-.26.82-.58v-2.17c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.84 2.81 1.3 3.5 1 .1-.78.42-1.31.76-1.61-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 6 0c2.29-1.55 3.3-1.23 3.3-1.23.66 1.66.24 2.88.12 3.18.77.84 1.24 1.91 1.24 3.22 0 4.61-2.81 5.62-5.49 5.92.43.37.81 1.1.81 2.22v3.29c0 .32.22.69.83.58A12 12 0 0 0 12 .3Z" />
        </svg>
      );
    case 'play':
      return <svg {...STROKE}><polygon points="6 4 20 12 6 20 6 4" /></svg>;
    case 'tickets':
      return <svg {...STROKE}><rect x="3" y="4" width="5" height="16" rx="1" /><rect x="10" y="4" width="5" height="16" rx="1" /><path d="m17 5 4 14" /></svg>;
    case 'file':
      return <svg {...STROKE}><path d="M4 19V9M10 19V5M16 19v-7M22 19V3" /></svg>;
    case 'award':
      return <svg {...STROKE}><circle cx="12" cy="8" r="6" /><polyline points="8.21 13.89 7 23 12 20 17 23 15.79 13.88" /></svg>;
    case 'activity':
      return <svg {...STROKE}><path d="M12 8v4l3 2" /><circle cx="12" cy="12" r="9" /></svg>;
    case 'teams':
      return <svg {...STROKE}><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></svg>;
    case 'users':
      return <svg {...STROKE}><circle cx="9" cy="8" r="4" /><path d="M2 21a7 7 0 0 1 14 0" /><path d="M16 4a4 4 0 0 1 0 8M22 21a7 7 0 0 0-4-6.3" /></svg>;
    case 'settings':
      return (
        <svg {...STROKE}>
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
        </svg>
      );
    default:
      return null;
  }
}

export default function Nav() {
  const pathname = usePathname();
  const router = useRouter();
  const [dark, setDark] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const { apps, teamApps, teams, activeTeamId, setActiveTeamId, activeApp, activeAppId, setActiveAppId, me: githubUser, can } = useActiveApp();
  const [backendUp, setBackendUp] = useState<boolean | null>(null);
  const [query, setQuery] = useState('');
  const searchRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // A theme saved on the Account page follows the user to any browser.
  const savedTheme = githubUser?.preferences?.theme;
  useEffect(() => {
    if (!savedTheme) return;
    const next = savedTheme === 'system' ? window.matchMedia('(prefers-color-scheme: dark)').matches : savedTheme === 'dark';
    document.documentElement.classList.toggle('dark', next);
    setDark(next);
    try {
      if (savedTheme === 'system') localStorage.removeItem('theme');
      else localStorage.setItem('theme', savedTheme);
    } catch { /* ignore */ }
  }, [savedTheme]);

  useEffect(() => {
    setDark(document.documentElement.classList.contains('dark'));
  }, []);

  useEffect(() => {
    let cancelled = false;
    const load = () => getStats()
      .then(() => { if (!cancelled) setBackendUp(true); })
      .catch(() => { if (!cancelled) setBackendUp(false); });
    load();
    const t = setInterval(load, 15000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        searchRef.current?.focus();
      }
      if (e.key === 'Escape') {
        searchRef.current?.blur();
        setMenuOpen(false);
        setSwitcherOpen(false);
        setMobileOpen(false);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => {
    if (!menuOpen && !switcherOpen) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
        setSwitcherOpen(false);
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [menuOpen, switcherOpen]);

  const switchApp = (id: string) => {
    setActiveAppId(id);
    setSwitcherOpen(false);
  };

  const toggleDark = () => {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle('dark', next);
    localStorage.setItem('theme', next ? 'dark' : 'light');
    updatePreferences({ theme: next ? 'dark' : 'light' }).catch(() => {});
  };

  const handleLogout = async () => {
    setMenuOpen(false);
    await logoutGithub();
    router.push('/login');
  };

  const submitSearch = () => {
    const q = query.trim();
    if (!q) return;
    router.push(`/logs?q=${encodeURIComponent(q)}`);
    searchRef.current?.blur();
  };

  const initials = githubUser?.login?.slice(0, 2).toUpperCase() ?? '··';

  return (
    <>
      <header className="topbar">
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
          <button className="icon-btn mobile-menu" aria-label="Toggle navigation" onClick={() => setMobileOpen((o) => !o)}>
            <svg {...STROKE}><path d="M4 7h16M4 12h16M4 17h16" /></svg>
          </button>
          <Link href="/" className="topbar-brand">
            <span className="app-switcher" aria-hidden="true">
              {Array.from({ length: 9 }).map((_, i) => <i key={i} />)}
            </span>
            <span className="brand-mark">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
              </svg>
            </span>
            <span className="brand-text">Agent SDLC</span>
          </Link>
        </div>

        <label className="topbar-search">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></svg>
          <input
            ref={searchRef}
            type="search"
            placeholder="Search runs by ticket ID…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') submitSearch(); }}
          />
          <kbd>Ctrl K</kbd>
        </label>

        <div className="top-actions" ref={menuRef}>
          <div className="app-switch">
            <button
              className={`app-pill${switcherOpen ? ' open' : ''}`}
              aria-label="Switch application"
              onClick={() => { setSwitcherOpen((o) => !o); setMenuOpen(false); }}
            >
              <span className="app-avatar">{appInitials(activeApp?.name ?? 'Default')}</span>
              <span className="app-pill-text">
                <small>Application</small>
                <b>{activeApp?.name ?? 'Default application'}</b>
              </span>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6" /></svg>
            </button>
            {switcherOpen && (
              <div className="account-menu app-menu">
                <div className="account-menu-head">
                  <small>Switch application</small>
                  <b>{teamApps.length} of {apps.length} you can access</b>
                </div>
                {teams.length > 0 && (
                  <div className="team-filter">
                    <button className={!activeTeamId ? 'on' : ''} onClick={() => setActiveTeamId('')}>All teams</button>
                    {teams.map((t) => (
                      <button key={t.id} className={activeTeamId === t.id ? 'on' : ''} onClick={() => setActiveTeamId(t.id)}>{t.name}</button>
                    ))}
                  </div>
                )}
                {teamApps.map((a) => (
                  <button key={a.id} className={`app-option${a.id === activeAppId ? ' active' : ''}`} onClick={() => switchApp(a.id)}>
                    <span className="app-avatar">{appInitials(a.name)}</span>
                    <span className="app-option-text">
                      {a.name}
                      <span>{a.team || 'Shared'} · {a.ticket_provider === 'ado' ? 'Azure DevOps' : 'Jira'} · {a.target_repo || 'no repository yet'}</span>
                    </span>
                    {a.id === activeAppId && <span className="app-option-check">✓</span>}
                  </button>
                ))}
                <div className="app-menu-divider" />
                {can('admin') && <Link href="/applications?onboard=1" onClick={() => setSwitcherOpen(false)}>＋ Onboard application</Link>}
                <Link href="/applications" onClick={() => setSwitcherOpen(false)}>Manage applications</Link>
              </div>
            )}
          </div>
          <button className="icon-btn" aria-label="Toggle theme" onClick={toggleDark} title={dark ? 'Light theme' : 'Dark theme'}>
            {dark ? (
              <svg {...STROKE}><circle cx="12" cy="12" r="5" /><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" /></svg>
            ) : (
              <svg {...STROKE}><path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5 8.5 8.5 0 1 0 20.5 14.5Z" /></svg>
            )}
          </button>
          <button
            className="avatar-btn"
            aria-label="Account"
            onClick={() => { setMenuOpen((o) => !o); setSwitcherOpen(false); }}
            style={{ backgroundImage: githubUser?.avatar_url ? `url(${githubUser.avatar_url})` : undefined }}
          >
            {!githubUser?.avatar_url && initials}
          </button>
          {menuOpen && (
            <div className="account-menu">
              <div className="account-menu-head">
                <small>Signed in with GitHub</small>
                <b>{githubUser ? `@${githubUser.login}` : 'Unknown user'}</b>
                {githubUser && <span className={`role-badge ${githubUser.role}`}>{githubUser.role}</span>}
              </div>
              <Link href="/account" onClick={() => setMenuOpen(false)}>Account settings</Link>
              <Link href="/applications" onClick={() => setMenuOpen(false)}>Manage applications</Link>
              <button onClick={handleLogout}>Log out</button>
            </div>
          )}
        </div>
      </header>

      {mobileOpen && <div className="sidebar-backdrop" onClick={() => setMobileOpen(false)} />}

      <aside className={`sidebar${mobileOpen ? ' open' : ''}`}>
        <Link href="/run" className="sidebar-cta" onClick={() => setMobileOpen(false)}>
          <svg {...STROKE}><path d="M12 5v14M5 12h14" /></svg>
          New run
        </Link>

        <nav className="sidebar-nav">
          {NAV_ITEMS.filter((item) => !item.minRole || can(item.minRole)).map(({ label, href, icon, color }) => (
            <Link
              key={href}
              href={href}
              onClick={() => setMobileOpen(false)}
              className={`nav-link${pathname === href ? ' active' : ''}`}
            >
              <span className="nav-icon" style={{ '--nav-color': color } as React.CSSProperties}>
                <NavIcon name={icon} />
              </span>
              {label}
            </Link>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className={`server-pill${backendUp === false ? ' down' : ''}`}>
            <span className={backendUp === false ? 'dot red' : 'dot'} />
            {backendUp === false ? 'Backend unreachable' : 'Pipeline server connected'}
          </div>
        </div>
      </aside>
    </>
  );
}
