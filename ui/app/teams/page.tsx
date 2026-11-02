'use client';

import { useCallback, useEffect, useState } from 'react';
import Nav from '@/components/Nav';
import PageHeader from '@/components/PageHeader';
import { useActiveApp } from '@/components/AppContext';
import {
  addTeamMember, createTeam, deleteTeam, listUsers, removeTeamMember,
  type Team, type UserRecord,
} from '@/lib/api';

export default function TeamsPage() {
  const { me, can, apps, teams, refreshTeams, refreshApps } = useActiveApp();
  const isAdmin = can('admin');
  const [users, setUsers] = useState<UserRecord[]>([]);
  const [newTeam, setNewTeam] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (isAdmin) listUsers().then(setUsers).catch(() => {});
  }, [isAdmin]);

  const act = useCallback(async (fn: () => Promise<unknown>, done: string) => {
    setError(null);
    try {
      await fn();
      await Promise.all([refreshTeams(), refreshApps()]);
      setNotice(done);
    } catch (e: any) {
      setError(e.message);
    }
  }, [refreshTeams, refreshApps]);

  const add = (team: Team, login: string) => login && act(() => addTeamMember(team.id, login), `@${login} added to ${team.name}.`);
  const remove = (team: Team, login: string) => act(() => removeTeamMember(team.id, login), `@${login} removed from ${team.name}.`);
  const removeTeam = (team: Team) => {
    if (window.confirm(`Delete the ${team.name} team?`)) act(() => deleteTeam(team.id), `${team.name} was deleted.`);
  };
  const create = () => {
    const name = newTeam.trim();
    if (!name) return;
    act(() => createTeam(name), `${name} team created — add people, then assign applications to it.`).then(() => setNewTeam(''));
  };

  const shared = apps.filter((a) => !a.team_id);

  return (
    <div className="bg-bg min-h-screen">
      <Nav />

      <div className="content page-stack">
        <PageHeader
          eyebrow="Access"
          title="Teams"
          description="An application belongs to one team — only its members (and admins) can see and run it. Shared applications are visible to everyone. People can be in several teams."
          actions={isAdmin ? (
            <div style={{ display: 'flex', gap: 8 }}>
              <input className="input-main" style={{ width: 220 }} placeholder="New team name, e.g. Payments" value={newTeam}
                onChange={(e) => setNewTeam(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') create(); }} />
              <button className="btn-primary" onClick={create} disabled={!newTeam.trim()}>＋ Create team</button>
            </div>
          ) : undefined}
        />

        {error && <div className="alert error">{error}</div>}
        {notice && <div className="tip-box"><b>✓ {notice}</b></div>}
        {me && !isAdmin && (
          <div className="locked-note">🔒 Only <b>admins</b> manage teams. Below are the teams you&apos;re in.</div>
        )}

        <div className="app-grid">
          {teams.map((team) => {
            const teamApps = apps.filter((a) => a.team_id === team.id);
            const candidates = users.filter((u) => !team.members.includes(u.login));
            return (
              <div key={team.id} className="card-main team-card">
                <div className="app-card-head">
                  <span className="app-avatar lg">{team.name.slice(0, 2).toUpperCase()}</span>
                  <div style={{ minWidth: 0 }}>
                    <h3>{team.name}</h3>
                    <p style={{ fontSize: 12, color: 'var(--color-text-muted)' }}>
                      {team.members.length} member{team.members.length === 1 ? '' : 's'} · {team.app_count} application{team.app_count === 1 ? '' : 's'}
                    </p>
                  </div>
                  {isAdmin && (
                    <button className="link-btn" style={{ marginLeft: 'auto', border: 0, background: 'none', color: 'var(--color-red)' }}
                      onClick={() => removeTeam(team)}>Delete</button>
                  )}
                </div>

                <div>
                  <div className="label-main">Members</div>
                  <div className="app-card-meta">
                    {team.members.length === 0 && <span style={{ fontSize: 12, color: 'var(--color-text-muted)' }}>No one yet</span>}
                    {team.members.map((login) => (
                      <span key={login} className="member-chip">
                        @{login}
                        {isAdmin && <button aria-label={`Remove ${login}`} onClick={() => remove(team, login)}>×</button>}
                      </span>
                    ))}
                  </div>
                  {isAdmin && candidates.length > 0 && (
                    <select className="input-main" style={{ marginTop: 10 }} value="" onChange={(e) => add(team, e.target.value)}>
                      <option value="">＋ Add a member…</option>
                      {candidates.map((u) => <option key={u.login} value={u.login}>@{u.login} ({u.role})</option>)}
                    </select>
                  )}
                </div>

                <div>
                  <div className="label-main">Applications</div>
                  <div className="app-card-meta">
                    {teamApps.length === 0
                      ? <span style={{ fontSize: 12, color: 'var(--color-text-muted)' }}>None — assign one from its Config page</span>
                      : teamApps.map((a) => <span key={a.id} className="chip purple">{a.name}</span>)}
                  </div>
                </div>
              </div>
            );
          })}
          {teams.length === 0 && (
            <div className="card-main" style={{ padding: 24, color: 'var(--color-text-muted)', fontSize: 14 }}>
              {isAdmin ? 'No teams yet — create one above.' : "You aren't in any team yet. You can still use shared applications."}
            </div>
          )}
        </div>

        <div className="card-main" style={{ padding: 20 }}>
          <div className="label-main">Shared with everyone</div>
          <div className="app-card-meta">
            {shared.map((a) => <span key={a.id} className="chip">{a.name}</span>)}
          </div>
        </div>
      </div>
    </div>
  );
}
