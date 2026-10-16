'use client';

import { Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import Nav, { appInitials } from '@/components/Nav';
import PageHeader from '@/components/PageHeader';
import { useActiveApp } from '@/components/AppContext';
import {
  getGithubUser, logoutGithub, getGithubRepos, selectGithubRepo, createApp, deleteApp,
  getAppConfig, listSkills, GITHUB_LOGIN_PATH,
  type GithubUser, type GithubRepo, type Application, type AppConfigUpdate, type SkillSummary,
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

function AppCard({
  app, active, onSelect, onDelete, canConfigure, canRemove,
}: { app: Application; active: boolean; onSelect: () => void; onDelete: () => void; canConfigure: boolean; canRemove: boolean }) {
  return (
    <div
      role="button"
      tabIndex={0}
      className={`card-main app-card${active ? ' active' : ''}`}
      onClick={onSelect}
      onKeyDown={(e) => { if (e.key === 'Enter') onSelect(); }}
    >
      <div className="app-card-head">
        <span className="app-avatar lg">{appInitials(app.name)}</span>
        <div style={{ minWidth: 0 }}>
          <h3>{app.name}</h3>
          <p>{app.team ? `Team · ${app.team}` : 'Shared with everyone'}</p>
        </div>
        {active && <span className="active-tag">ACTIVE</span>}
      </div>
      <div className="app-card-meta">
        <span className="chip purple">{app.ticket_provider === 'ado' ? 'Azure DevOps' : 'Jira'}</span>
        <span className="chip" title={app.target_repo}>{app.target_repo ? `${app.target_repo} · ${app.target_branch}` : 'No repository yet'}</span>
        <span className="chip">{app.default_persona ? `persona · ${app.default_persona}` : 'persona · auto'}</span>
        {app.require_pr_approval && <span className="chip purple">🛡️ approval before PR</span>}
        {app.protected_paths && <span className="chip" title={app.protected_paths}>🔒 protected paths</span>}
        {app.verify_mode === 'command' && <span className="chip" title="Tests run before any PR opens">🧪 {app.test_command}</span>}
        {app.verify_mode === 'github_actions' && <span className="chip">⚙️ GitHub Actions{app.ci_workflow ? ` · ${app.ci_workflow}` : ''}</span>}
      </div>
      <div style={{ display: 'flex', gap: 14, fontSize: 12, fontWeight: 700 }}>
        {canConfigure && <Link href="/config" className="link-btn" onClick={(e) => { e.stopPropagation(); onSelect(); }}>Configure →</Link>}
        {!app.is_default && canRemove && (
          <button
            className="link-btn"
            style={{ border: 0, background: 'none', padding: 0, color: 'var(--color-red)', marginLeft: 'auto' }}
            onClick={(e) => { e.stopPropagation(); onDelete(); }}
          >
            Remove
          </button>
        )}
      </div>
    </div>
  );
}

const EMPTY_FORM = {
  name: '', team_id: '', description: '',
  TICKET_PROVIDER: 'ado' as 'jira' | 'ado',
  JIRA_BASE_URL: '', JIRA_USER_EMAIL: '', JIRA_API_TOKEN: '',
  ADO_ORG_URL: '', ADO_PROJECT: '', ADO_PAT: '',
  DEFAULT_PERSONA: '', BRANCH_PREFIX: '', GATE_MIN_WORDS: '',
  REQUIRE_PR_APPROVAL: '', PROTECTED_PATHS: '',
  VERIFY_MODE: '', TEST_COMMAND: '',
};

function OnboardModal({ onClose, onCreated }: { onClose: () => void; onCreated: (app: Application) => void }) {
  const { teams, activeTeamId } = useActiveApp();
  const [form, setForm] = useState({ ...EMPTY_FORM, team_id: activeTeamId });
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [serverDefaults, setServerDefaults] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listSkills().then((s) => setSkills(s.filter((k) => !k.error))).catch(() => {});
    getAppConfig('default').then((c) => setServerDefaults(c.inherited)).catch(() => {});
  }, []);

  const set = (key: keyof typeof EMPTY_FORM) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  const hint = (key: string, fallback?: string) => {
    const v = serverDefaults[key] || fallback;
    return v ? `Blank = server default: ${v}` : 'Blank = server default';
  };

  const submit = async () => {
    if (!form.name.trim()) { setError('Give the application a name.'); return; }
    setSaving(true);
    setError(null);
    const providerKeys = form.TICKET_PROVIDER === 'ado'
      ? ['ADO_ORG_URL', 'ADO_PROJECT', 'ADO_PAT'] as const
      : ['JIRA_BASE_URL', 'JIRA_USER_EMAIL', 'JIRA_API_TOKEN'] as const;
    const config: AppConfigUpdate = { TICKET_PROVIDER: form.TICKET_PROVIDER };
    for (const k of [...providerKeys, 'DEFAULT_PERSONA', 'BRANCH_PREFIX', 'GATE_MIN_WORDS', 'REQUIRE_PR_APPROVAL', 'PROTECTED_PATHS', 'VERIFY_MODE', 'TEST_COMMAND'] as const) {
      if (form[k].trim()) config[k] = form[k].trim();
    }
    try {
      const app = await createApp({ name: form.name, team_id: form.team_id || null, description: form.description, config });
      onCreated(app);
    } catch (e: any) {
      setError(e.message);
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="card-main modal-card" role="dialog" aria-modal="true" aria-label="Onboard application">
        <div className="eyebrow">✦ New application</div>
        <h2>Onboard an application</h2>
        <p style={{ fontSize: 13, color: 'var(--color-text-secondary)', margin: '6px 0 20px', lineHeight: 1.55 }}>
          Each application keeps its own ticket board, repository, and defaults. Anything you leave blank uses the
          server-wide setting, so you only fill in what&apos;s different.
        </p>

        {error && <div className="alert error" style={{ marginBottom: 14 }}>{error}</div>}

        <div className="form-grid">
          <div className="form-section-label">About</div>
          <div>
            <label className="label-main">Application name *</label>
            <input className="input-main" value={form.name} onChange={set('name')} placeholder="Checkout Service" autoFocus />
          </div>
          <div>
            <label className="label-main">Team</label>
            <select className="input-main" value={form.team_id} onChange={set('team_id')}>
              <option value="">Shared — everyone can use it</option>
              {teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
            <div className="field-hint">Only this team&apos;s members (and admins) will see it</div>
          </div>
          <div style={{ gridColumn: '1 / -1' }}>
            <label className="label-main">Description</label>
            <input className="input-main" value={form.description} onChange={set('description')} placeholder="What this application does" />
          </div>

          <div className="form-section-label">Configuration</div>
          <div style={{ gridColumn: '1 / -1' }}>
            <div className="segmented">
              {(['ado', 'jira'] as const).map((p) => (
                <button key={p} type="button" className={form.TICKET_PROVIDER === p ? 'on' : ''}
                  onClick={() => setForm((f) => ({ ...f, TICKET_PROVIDER: p }))}>
                  {p === 'ado' ? 'Azure DevOps' : 'Jira'}
                </button>
              ))}
            </div>
          </div>
          {form.TICKET_PROVIDER === 'ado' ? (
            <>
              <div>
                <label className="label-main">Org URL</label>
                <input className="input-main" value={form.ADO_ORG_URL} onChange={set('ADO_ORG_URL')} placeholder="https://dev.azure.com/your-org" />
                <div className="field-hint">{hint('ADO_ORG_URL')}</div>
              </div>
              <div>
                <label className="label-main">Project</label>
                <input className="input-main" value={form.ADO_PROJECT} onChange={set('ADO_PROJECT')} placeholder="Your-Project" />
                <div className="field-hint">{hint('ADO_PROJECT')}</div>
              </div>
              <div>
                <label className="label-main">Personal access token</label>
                <input className="input-main" type="password" autoComplete="new-password" value={form.ADO_PAT} onChange={set('ADO_PAT')} placeholder="••••••••" />
                <div className="field-hint">Blank = server default token</div>
              </div>
            </>
          ) : (
            <>
              <div>
                <label className="label-main">Jira base URL</label>
                <input className="input-main" value={form.JIRA_BASE_URL} onChange={set('JIRA_BASE_URL')} placeholder="https://your-domain.atlassian.net" />
                <div className="field-hint">{hint('JIRA_BASE_URL')}</div>
              </div>
              <div>
                <label className="label-main">Jira user email</label>
                <input className="input-main" value={form.JIRA_USER_EMAIL} onChange={set('JIRA_USER_EMAIL')} placeholder="bot@company.com" />
                <div className="field-hint">{hint('JIRA_USER_EMAIL')}</div>
              </div>
              <div>
                <label className="label-main">Jira API token</label>
                <input className="input-main" type="password" autoComplete="new-password" value={form.JIRA_API_TOKEN} onChange={set('JIRA_API_TOKEN')} placeholder="••••••••" />
                <div className="field-hint">Blank = server default token</div>
              </div>
            </>
          )}

          <div className="form-section-label">Defaults for this application</div>
          <div>
            <label className="label-main">Default persona</label>
            <select className="input-main" value={form.DEFAULT_PERSONA} onChange={set('DEFAULT_PERSONA')}>
              <option value="">Auto — pick from the ticket</option>
              {skills.map((s) => <option key={s.slug} value={s.slug}>{s.name || s.slug}</option>)}
            </select>
          </div>
          <div>
            <label className="label-main">Branch prefix</label>
            <input className="input-main" value={form.BRANCH_PREFIX} onChange={set('BRANCH_PREFIX')} placeholder="feat/" />
          </div>
          <div>
            <label className="label-main">Clarity gate — min. words</label>
            <input className="input-main" inputMode="numeric" value={form.GATE_MIN_WORDS} onChange={set('GATE_MIN_WORDS')} placeholder="5" />
          </div>

          <div className="form-section-label">Verification</div>
          <div>
            <label className="label-main">How to verify the agent&apos;s change</label>
            <select className="input-main" value={form.VERIFY_MODE} onChange={set('VERIFY_MODE')}>
              <option value="">Review the diff only</option>
              <option value="command">Run a test command</option>
              <option value="github_actions">Wait for GitHub Actions</option>
            </select>
          </div>
          {form.VERIFY_MODE === 'command' && (
            <div>
              <label className="label-main">Test command</label>
              <input className="input-main" value={form.TEST_COMMAND} onChange={set('TEST_COMMAND')} placeholder="pytest -q" />
              <div className="field-hint">Setup command & timeout: on the Config page</div>
            </div>
          )}

          <div className="form-section-label">Governance</div>
          <div>
            <label className="label-main">Approval before pull request</label>
            <select className="input-main" value={form.REQUIRE_PR_APPROVAL} onChange={set('REQUIRE_PR_APPROVAL')}>
              <option value="">Not required</option>
              <option value="true">Required — a maintainer approves</option>
            </select>
          </div>
          <div>
            <label className="label-main">Protected paths</label>
            <input className="input-main" value={form.PROTECTED_PATHS} onChange={set('PROTECTED_PATHS')} placeholder=".github/workflows/*, *.tf" />
            <div className="field-hint">The agent may never change these</div>
          </div>
        </div>

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 24 }}>
          <button className="btn-secondary" onClick={onClose} disabled={saving}>Cancel</button>
          <button className="btn-primary" onClick={submit} disabled={saving} style={{ opacity: saving ? 0.6 : 1 }}>
            {saving ? 'Onboarding…' : 'Onboard application'}
          </button>
        </div>
      </div>
    </div>
  );
}

function ApplicationsContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { apps, teamApps, teams, activeTeamId, setActiveTeamId, activeApp, activeAppId, setActiveAppId, refreshApps, me, can } = useActiveApp();
  const isAdmin = can('admin');
  const canPickRepo = can('maintainer');
  const [user, setUser] = useState<GithubUser | null | undefined>(undefined);
  const [repos, setRepos] = useState<GithubRepo[]>([]);
  const [loadingRepos, setLoadingRepos] = useState(false);
  const [selecting, setSelecting] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [onboarding, setOnboarding] = useState(false);

  useEffect(() => {
    if (searchParams.get('onboard') === '1' && isAdmin) setOnboarding(true);
  }, [searchParams, isAdmin]);

  const closeOnboarding = () => {
    setOnboarding(false);
    if (searchParams.get('onboard')) router.replace('/applications');
  };

  const refresh = useCallback(async () => {
    const u = await getGithubUser();
    setUser(u);
    if (u) {
      setLoadingRepos(true);
      try {
        setRepos(await getGithubRepos());
        setError(null);
      } catch (e: any) {
        // The backend drops the session when GitHub rejects its token, so a
        // 401 here means "log in again", not an error to show under "Connected as".
        if (String(e.message).startsWith('401')) {
          router.replace('/login');
          return;
        }
        setError(e.message);
      } finally {
        setLoadingRepos(false);
      }
    }
  }, [router]);

  useEffect(() => { refresh(); }, [refresh]);

  const handleSelect = async (repo: GithubRepo) => {
    setSelecting(repo.full_name);
    try {
      await selectGithubRepo(repo.full_name, repo.default_branch, activeAppId);
      await refreshApps();
      setError(null);
      setNotice(`${activeApp?.name ?? 'Application'} now works on ${repo.full_name}.`);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSelecting(null);
    }
  };

  const handleCreated = async (app: Application) => {
    await refreshApps();
    setActiveAppId(app.id);
    closeOnboarding();
    setNotice(`${app.name} is onboarded and active. Pick its repository below.`);
    document.getElementById('repositories')?.scrollIntoView({ behavior: 'smooth' });
  };

  const handleDelete = async (app: Application) => {
    if (!window.confirm(`Remove ${app.name}? Its run history is kept.`)) return;
    try {
      await deleteApp(app.id);
      if (app.id === activeAppId) setActiveAppId('default');
      await refreshApps();
      setNotice(`${app.name} was removed.`);
    } catch (e: any) {
      setError(e.message);
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
      <Nav />

      <div className="content">
        <PageHeader
          eyebrow="Onboarding"
          title="Applications"
          description="Every team or project gets its own application — its own ticket board, repository, and defaults. Runs always use the active application's settings."
          actions={isAdmin ? <button className="btn-primary" onClick={() => setOnboarding(true)}>＋ Onboard application</button> : undefined}
        />

        {error && <div className="alert error">{error}</div>}
        {notice && (
          <div className="tip-box" style={{ marginBottom: 16 }}>
            <b>✓ {notice}</b>
          </div>
        )}

        {teams.length > 0 && (
          <div className="segmented" style={{ marginBottom: 16 }}>
            <button className={!activeTeamId ? 'on' : ''} onClick={() => setActiveTeamId('')}>All teams ({apps.length})</button>
            {teams.map((t) => (
              <button key={t.id} className={activeTeamId === t.id ? 'on' : ''} onClick={() => setActiveTeamId(t.id)}>{t.name}</button>
            ))}
          </div>
        )}

        <div className="app-grid">
          {teamApps.map((app) => (
            <AppCard
              key={app.id}
              app={app}
              active={app.id === activeAppId}
              onSelect={() => setActiveAppId(app.id)}
              onDelete={() => handleDelete(app)}
              canConfigure={canPickRepo}
              canRemove={isAdmin}
            />
          ))}
          {isAdmin && (
            <button className="card-main app-card add" onClick={() => setOnboarding(true)}>
              ＋ Onboard application
            </button>
          )}
        </div>

        <div className="section-title" id="repositories">
          <div>
            <h2>Repository for {activeApp?.name ?? 'this application'}</h2>
            <p>Click a repository to point the active application at it. Runs clone, push, and open pull requests there.</p>
          </div>
        </div>

        {me && !canPickRepo && (
          <div className="locked-note" style={{ marginBottom: 16 }}>🔒 Changing an application&apos;s repository needs <b>maintainer</b> or higher. Your role is <b>{me.role}</b>.</div>
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
            <div className="card-main" style={{ padding: '14px 18px', marginBottom: 20, display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
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
                    active={repo.full_name === activeApp?.target_repo}
                    selecting={selecting === repo.full_name}
                    onSelect={() => { if (canPickRepo) handleSelect(repo); }}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {onboarding && <OnboardModal onClose={closeOnboarding} onCreated={handleCreated} />}
    </div>
  );
}

export default function ApplicationsPage() {
  return (
    <Suspense fallback={null}>
      <ApplicationsContent />
    </Suspense>
  );
}
