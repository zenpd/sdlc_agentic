'use client';

import { useCallback, useEffect, useState } from 'react';
import Nav from '@/components/Nav';
import PageHeader from '@/components/PageHeader';
import { useActiveApp } from '@/components/AppContext';
import { listUsers, updateUserRole, formatTime, ROLES, ROLE_INFO, type Role, type UserRecord } from '@/lib/api';

export default function UsersPage() {
  const { me, can } = useActiveApp();
  const [users, setUsers] = useState<UserRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setUsers(await listUsers());
      setError(null);
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    if (!me) return;
    if (!can('admin')) { setLoading(false); return; }
    refresh().finally(() => setLoading(false));
  }, [me, can, refresh]);

  const changeRole = async (login: string, role: Role) => {
    setSaving(login);
    setNotice(null);
    try {
      await updateUserRole(login, role);
      await refresh();
      setError(null);
      setNotice(`@${login} is now ${role}.`);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSaving(null);
    }
  };

  return (
    <div className="bg-bg min-h-screen">
      <Nav />

      <div className="content">
        <PageHeader
          eyebrow="Access"
          title="Users"
          description="Everyone who has signed in, and what their role lets them do. New people start as viewers until an admin raises them."
        />

        {error && <div className="alert error">{error}</div>}
        {notice && <div className="tip-box" style={{ marginBottom: 16 }}><b>✓ {notice}</b></div>}

        <div className="app-card-meta" style={{ marginBottom: 18 }}>
          {ROLES.map((r) => (
            <span key={r} className="chip" title={ROLE_INFO[r]}>
              <span className={`role-badge ${r}`} style={{ marginTop: 0 }}>{r}</span>
              {ROLE_INFO[r]}
            </span>
          ))}
        </div>

        {me && !can('admin') ? (
          <div className="locked-note">🔒 Only <b>admins</b> can manage users. Your role is <b>{me.role}</b>.</div>
        ) : loading ? (
          <div style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>Loading...</div>
        ) : (
          <div className="card-main" style={{ padding: 0, overflow: 'hidden' }}>
            {users.map((u) => (
              <div key={u.login} className="row-item" style={{ padding: '14px 20px', borderBottom: '1px solid var(--color-border)', cursor: 'default' }}>
                {u.avatar_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={u.avatar_url} alt="" width={36} height={36} style={{ borderRadius: '50%' }} />
                ) : (
                  <span className="app-avatar lg" style={{ borderRadius: '50%' }}>{u.login.slice(0, 2).toUpperCase()}</span>
                )}
                <div style={{ minWidth: 0 }}>
                  <b>@{u.login}{u.login === me?.login ? ' (you)' : ''}</b>
                  <span className="meta">Joined {formatTime(u.created_at)} · last sign-in {formatTime(u.last_login_at)}</span>
                </div>
                <select
                  className="input-main"
                  style={{ width: 150 }}
                  value={u.role}
                  disabled={saving === u.login}
                  onChange={(e) => changeRole(u.login, e.target.value as Role)}
                >
                  {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
