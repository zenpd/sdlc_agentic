'use client';

import { useEffect, useMemo, useState } from 'react';
import Nav from '@/components/Nav';
import PageHeader from '@/components/PageHeader';
import { useActiveApp } from '@/components/AppContext';
import { getAudit, formatTime, type AuditEntry } from '@/lib/api';

const ACTION_LABELS: Record<string, string> = {
  'app.onboard': 'onboarded application',
  'app.update': 'renamed / edited application',
  'app.remove': 'removed application',
  'app.config': 'changed config of',
  'app.repo': 'pointed repository of',
  'server.config': 'changed',
  'skill.update': 'edited persona skill',
  'skill.create': 'created persona skill',
  'user.role': 'changed the role of',
};

function Diff({ details }: { details: Record<string, any> }) {
  const lines: React.ReactNode[] = [];
  const walk = (obj: Record<string, any>, prefix = '') => {
    for (const [key, val] of Object.entries(obj)) {
      const name = prefix + key;
      if (val && typeof val === 'object' && 'from' in val && 'to' in val) {
        lines.push(
          <div key={name}>
            {name}: {val.from ? <span className="from">{String(val.from)}</span> : <i>(blank)</i>} → {val.to ? <span className="to">{String(val.to)}</span> : <i>(blank — inherits default)</i>}
          </div>,
        );
      } else if (val && typeof val === 'object') {
        walk(val, `${name}.`);
      } else if (val === 'updated') {
        lines.push(<div key={name}>{name}: <span className="to">updated (value hidden)</span></div>);
      } else if (val !== '' && val != null) {
        lines.push(<div key={name}>{name}: {String(val)}</div>);
      }
    }
  };
  walk(details);
  return lines.length ? <div className="audit-diff">{lines}</div> : null;
}

export default function ActivityPage() {
  const { apps, activeApp, activeAppId, me, can } = useActiveApp();
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [scope, setScope] = useState<'app' | 'all'>('all');
  const [actor, setActor] = useState('');

  useEffect(() => {
    if (!me) return;
    if (!can('maintainer')) { setLoading(false); return; }
    let cancelled = false;
    getAudit({ appId: scope === 'app' ? activeAppId : undefined })
      .then((e) => { if (!cancelled) { setEntries(e); setError(null); } })
      .catch((e) => { if (!cancelled) setError(e.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [me, can, scope, activeAppId]);

  const actors = useMemo(() => [...new Set(entries.map((e) => e.actor))].sort(), [entries]);
  const visible = actor ? entries.filter((e) => e.actor === actor) : entries;
  const appName = (id: string | null) => (id ? apps.find((a) => a.id === id)?.name ?? id : null);

  return (
    <div className="bg-bg min-h-screen">
      <Nav />

      <div className="content">
        <PageHeader
          eyebrow="Audit log"
          title="Activity"
          description="Who changed what, and when — onboarding, config, repositories, skills, and roles. Token values are never recorded."
          actions={
            <>
              <select className="input-main" style={{ width: 170 }} value={actor} onChange={(e) => setActor(e.target.value)}>
                <option value="">Everyone</option>
                {actors.map((a) => <option key={a} value={a}>@{a}</option>)}
              </select>
              <div className="segmented">
                <button className={scope === 'app' ? 'on' : ''} onClick={() => setScope('app')}>{activeApp?.name ?? 'This application'}</button>
                <button className={scope === 'all' ? 'on' : ''} onClick={() => setScope('all')}>Everything</button>
              </div>
            </>
          }
        />

        {error && <div className="alert error">{error}</div>}

        {me && !can('maintainer') ? (
          <div className="locked-note">🔒 The audit log is visible to <b>maintainers and admins</b>. Your role is <b>{me.role}</b>.</div>
        ) : loading ? (
          <div style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>Loading...</div>
        ) : visible.length === 0 ? (
          <div className="card-main" style={{ padding: 24, color: 'var(--color-text-muted)', fontSize: 14 }}>
            No changes recorded yet.
          </div>
        ) : (
          <div className="card-main" style={{ padding: 0, overflow: 'hidden' }}>
            {visible.map((e) => (
              <div key={e.id} className="audit-row">
                <span className="audit-when">{formatTime(e.at)}</span>
                <b>@{e.actor}</b>
                <div>
                  {ACTION_LABELS[e.action] ?? e.action}{' '}
                  <b>{e.target}</b>
                  {e.app_id && e.action !== 'app.onboard' && e.target !== appName(e.app_id) && (
                    <span className="chip purple" style={{ marginLeft: 8 }}>{appName(e.app_id)}</span>
                  )}
                  <Diff details={e.details} />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
