'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import Nav from '@/components/Nav';
import PageHeader from '@/components/PageHeader';
import StatusBadge from '@/components/StatusBadge';
import { useActiveApp } from '@/components/AppContext';
import {
  getRuns, listMySessions, revokeMySession, signOutOtherSessions, updatePreferences, formatTime,
  type RunRecord, type SessionInfo, type Theme,
} from '@/lib/api';

// SQLite gives "YYYY-MM-DD HH:MM:SS" in UTC with no zone marker.
const sqliteTime = (t: string) => formatTime(t.includes('T') ? t : `${t.replace(' ', 'T')}Z`);

function describeDevice(ua: string): { name: string; mobile: boolean } {
  if (!ua) return { name: 'Unknown device', mobile: false };
  const browser = /Edg\//.test(ua) ? 'Edge' : /Chrome\//.test(ua) ? 'Chrome' : /Firefox\//.test(ua) ? 'Firefox'
    : /Safari\//.test(ua) ? 'Safari' : ua.split(/[ /]/)[0];
  const os = /Windows/.test(ua) ? 'Windows' : /iPhone|iPad/.test(ua) ? 'iOS' : /Android/.test(ua) ? 'Android'
    : /Mac OS X/.test(ua) ? 'macOS' : /Linux/.test(ua) ? 'Linux' : '';
  return { name: os ? `${browser} on ${os}` : browser, mobile: /iPhone|iPad|Android|Mobile/.test(ua) };
}

export default function AccountPage() {
  const { me, apps, teams, refreshMe } = useActiveApp();
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [myRuns, setMyRuns] = useState<RunRecord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const loadSessions = useCallback(async () => {
    try { setSessions(await listMySessions()); } catch (e: any) { setError(e.message); }
  }, []);

  useEffect(() => {
    loadSessions();
    getRuns(undefined, { mine: true }).then(setMyRuns).catch(() => {});
  }, [loadSessions]);

  const savePref = async (update: { default_app_id?: string; theme?: Theme | '' }) => {
    setError(null);
    try {
      await updatePreferences(update);
      await refreshMe();
      setNotice('Preferences saved — they follow you to any browser you sign in on.');
    } catch (e: any) {
      setError(e.message);
    }
  };

  const revoke = async (s: SessionInfo) => {
    try {
      await revokeMySession(s.id);
      setNotice(`Signed out ${describeDevice(s.user_agent).name}.`);
      await loadSessions();
    } catch (e: any) { setError(e.message); }
  };

  const signOutOthers = async () => {
    if (!window.confirm('Sign out every other browser and device? This one stays signed in.')) return;
    try {
      const { signed_out } = await signOutOtherSessions();
      setNotice(`Signed out ${signed_out} other session${signed_out === 1 ? '' : 's'}.`);
      await loadSessions();
    } catch (e: any) { setError(e.message); }
  };

  const prefs = me?.preferences ?? {};
  const appName = (id: string) => apps.find((a) => a.id === id)?.name ?? id;
  const others = sessions.filter((s) => !s.current).length;

  return (
    <div className="bg-bg min-h-screen">
      <Nav />

      <div className="content page-stack">
        <PageHeader
          eyebrow="Your account"
          title="Account settings"
          description={me ? `Signed in as @${me.login} · role: ${me.role}. These settings are saved on the server and follow you to any device.` : 'Loading…'}
        />

        {error && <div className="alert error">{error}</div>}
        {notice && <div className="tip-box"><b>✓ {notice}</b></div>}

        <div className="card-main" style={{ padding: 22 }}>
          <div className="panel-head"><h3>Preferences</h3></div>
          <div className="form-grid">
            <div>
              <label className="label-main">Default application</label>
              <select className="input-main" value={prefs.default_app_id ?? ''} onChange={(e) => savePref({ default_app_id: e.target.value })}>
                <option value="">None — remember the last one per browser</option>
                {apps.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
              <div className="field-hint">Where you land when you sign in on a new browser</div>
            </div>
            <div>
              <label className="label-main">Theme</label>
              <select className="input-main" value={prefs.theme ?? ''} onChange={(e) => savePref({ theme: e.target.value as Theme | '' })}>
                <option value="">Not set (this browser decides)</option>
                <option value="light">Light</option>
                <option value="dark">Dark</option>
                <option value="system">Match system</option>
              </select>
            </div>
          </div>
        </div>

        <div className="card-main" style={{ padding: 22 }}>
          <div className="panel-head"><h3>Your teams</h3></div>
          <div className="app-card-meta">
            {teams.filter((t) => me && t.members.includes(me.login)).map((t) => (
              <span key={t.id} className="chip purple">{t.name} · {t.app_count} app{t.app_count === 1 ? '' : 's'}</span>
            ))}
            {me && !teams.some((t) => t.members.includes(me.login)) && (
              <span style={{ fontSize: 13, color: 'var(--color-text-muted)' }}>
                You&apos;re not in any team yet — you can use shared applications{me.role === 'admin' ? ', and as an admin you see every application' : ''}.
              </span>
            )}
          </div>
        </div>

        <div className="card-main" style={{ padding: 22 }}>
          <div className="panel-head">
            <h3>Your recent runs</h3>
            <Link href="/logs" className="link-btn">All logs →</Link>
          </div>
          {myRuns.length === 0 ? (
            <div style={{ color: 'var(--color-text-muted)', fontSize: 13 }}>You haven&apos;t started any runs yet.</div>
          ) : (
            myRuns.slice(0, 8).map((run) => (
              <Link key={run.id} href={`/logs?run=${run.id}`} className="row-item">
                <div className="row-thumb"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z" /></svg></div>
                <div style={{ minWidth: 0 }}>
                  <b>{/^\d+$/.test(run.ticket_key) ? `#${run.ticket_key}` : run.ticket_key}</b>
                  <span className="meta">{appName(run.app_id)} · {formatTime(run.started_at)}</span>
                </div>
                <StatusBadge status={run.status} />
              </Link>
            ))
          )}
        </div>

        <div className="card-main" style={{ padding: 0, overflow: 'hidden' }}>
          <div className="panel-head" style={{ padding: '18px 20px 6px' }}>
            <h3>Where you&apos;re signed in</h3>
            {others > 0 && <button className="btn-danger" onClick={signOutOthers}>Sign out everywhere else ({others})</button>}
          </div>
          {sessions.map((s) => {
            const device = describeDevice(s.user_agent);
            return (
              <div key={s.id} className="session-row">
                <span className="session-icon">
                  {device.mobile ? (
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="6" y="2" width="12" height="20" rx="2" /><path d="M11 18h2" /></svg>
                  ) : (
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="2" y="4" width="20" height="13" rx="2" /><path d="M8 21h8M12 17v4" /></svg>
                  )}
                </span>
                <div style={{ minWidth: 0 }}>
                  <b style={{ fontSize: 13.5 }}>{device.name}</b>
                  {s.current && <span className="this-device">This browser</span>}
                  <div style={{ fontSize: 11.5, color: 'var(--color-text-muted)', marginTop: 2 }}>
                    Signed in {sqliteTime(s.created_at)} · last active {sqliteTime(s.last_seen_at)}
                  </div>
                </div>
                {!s.current && <button className="btn-secondary" style={{ padding: '7px 14px', fontSize: 12.5 }} onClick={() => revoke(s)}>Sign out</button>}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
