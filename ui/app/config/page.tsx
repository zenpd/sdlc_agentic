'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import Nav from '@/components/Nav';
import PageHeader from '@/components/PageHeader';
import { useActiveApp } from '@/components/AppContext';
import {
  getConfig, updateConfig, getAppConfig, updateAppConfig, listSkills, updateApp, getAppSuggestions,
  type ConfigUpdate, type AppConfigUpdate, type SkillSummary, type AppSuggestions,
} from '@/lib/api';

type FieldProps = {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  inherited?: string;
  type?: string;
};

function Field({ label, value, onChange, placeholder, inherited, type = 'text' }: FieldProps) {
  return (
    <div>
      <label className="label-main">{label}</label>
      <input
        className="input-main"
        type={type}
        value={value}
        placeholder={inherited || placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
      {inherited !== undefined && !value && (
        <div className="field-hint">{inherited ? 'Inherited from server defaults' : 'Not set anywhere'}</div>
      )}
    </div>
  );
}

function BooleanField({
  label, value, inherited, defaultValue = false, onChange,
}: {
  label: string;
  value: string;
  inherited?: string;
  defaultValue?: boolean;
  onChange: (value: string) => void;
}) {
  const resolved = value || inherited || String(defaultValue);
  return (
    <label style={{ display: 'flex', alignItems: 'center', gap: 9, fontSize: 13 }}>
      <input
        type="checkbox"
        checked={resolved.toLowerCase() === 'true'}
        onChange={(e) => onChange(String(e.target.checked))}
      />
      <span>
        {label}
        {inherited !== undefined && !value && (
          <span className="field-hint" style={{ display: 'block' }}>
            {inherited ? 'Inherited from server defaults' : 'Using the built-in default'}
          </span>
        )}
      </span>
    </label>
  );
}

type SecretState = { set: boolean; inherited?: boolean };

function SecretField({
  label, state, value, onChange,
}: { label: string; state?: SecretState; value: string; onChange: (v: string) => void }) {
  const configuredState = state ?? { set: false };
  const note = !configuredState.set
    ? null
    : configuredState.inherited
      ? '· inherited from server'
      : '· configured';
  return (
    <div>
      <label className="label-main">
        {label} {note && <span style={{ color: configuredState.inherited ? 'var(--color-text-muted)' : 'var(--color-green)', fontWeight: 400 }}>{note}</span>}
      </label>
      <input
        className="input-main"
        type="password"
        value={value}
        placeholder={configuredState.set ? '•••••••• (leave blank to keep current value)' : 'Not set'}
        onChange={(e) => onChange(e.target.value)}
        autoComplete="new-password"
      />
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card-main" style={{ padding: 22, marginBottom: 18 }}>
      <div className="panel-head"><h3>{title}</h3></div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 16 }}>
        {children}
      </div>
    </div>
  );
}

const SETTING_KEYS = [
  'JIRA_BASE_URL', 'JIRA_USER_EMAIL', 'ADO_ORG_URL', 'ADO_PROJECT',
  'TARGET_REPO', 'TARGET_BRANCH', 'AZURE_OPENAI_ENDPOINT', 'AZURE_OPENAI_DEPLOYMENT',
  'STATUS_DONE', 'STATUS_IN_PROGRESS', 'STATUS_FAILED', 'NOTIFY_ON_SUCCESS', 'NOTIFY_ON_FAILURE',
  'NOTIFY_EMAIL_TO', 'SMTP_HOST', 'SMTP_PORT', 'SMTP_FROM', 'SMTP_USERNAME', 'SMTP_STARTTLS',
] as const;
const APP_DEFAULT_KEYS = [
  'DEFAULT_PERSONA', 'BRANCH_PREFIX', 'GATE_MIN_WORDS', 'REQUIRE_PR_APPROVAL', 'PROTECTED_PATHS',
  'VERIFY_MODE', 'TEST_SETUP_COMMAND', 'TEST_COMMAND', 'TEST_TIMEOUT_MIN', 'CI_WORKFLOW',
] as const;
const SECRET_KEYS = [
  'JIRA_API_TOKEN', 'ADO_PAT', 'GITHUB_TOKEN', 'AZURE_OPENAI_API_KEY',
  'SLACK_WEBHOOK_URL', 'TEAMS_WEBHOOK_URL', 'SMTP_PASSWORD', 'JIRA_WEBHOOK_TOKEN',
  'ADO_WEBHOOK_TOKEN',
] as const;

const EMPTY_SECRETS: Record<string, string> = Object.fromEntries(SECRET_KEYS.map((k) => [k, '']));
const NO_SECRETS: Record<string, SecretState> = Object.fromEntries(SECRET_KEYS.map((k) => [k, { set: false }]));

type Scope = 'app' | 'server';

export default function ConfigPage() {
  const { activeApp, activeAppId, refreshApps, refreshTeams, teams, me, can } = useActiveApp();
  const canEdit = can('maintainer');
  const [scope, setScope] = useState<Scope>('app');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [skills, setSkills] = useState<SkillSummary[]>([]);

  const [provider, setProvider] = useState<'jira' | 'ado'>('jira');
  const [fields, setFields] = useState<Record<string, string>>({});
  const [inherited, setInherited] = useState<Record<string, string> | null>(null);
  const [secretState, setSecretState] = useState<Record<string, SecretState>>(NO_SECRETS);
  const [secrets, setSecrets] = useState<Record<string, string>>(EMPTY_SECRETS);

  // Suggestions detected from this app's repository, fetched the first time
  // someone picks a verification mode or turns on approval.
  const [suggestions, setSuggestions] = useState<AppSuggestions | null>(null);
  const suggestionsFor = useRef<{ appId: string; promise: Promise<AppSuggestions | null> } | null>(null);
  useEffect(() => { setSuggestions(null); suggestionsFor.current = null; }, [activeAppId]);

  const loadSuggestions = () => {
    if (suggestionsFor.current?.appId !== activeAppId) {
      const appId = activeAppId;
      suggestionsFor.current = {
        appId,
        promise: getAppSuggestions(appId)
          .then((sg) => { if (suggestionsFor.current?.appId === appId) setSuggestions(sg); return sg; })
          .catch(() => null),
      };
    }
    return suggestionsFor.current.promise;
  };

  // Pre-fill only fields that are still empty — never overwrite what someone typed.
  const fillEmpty = (values: Record<string, string>) =>
    setFields((f) => {
      const next = { ...f };
      for (const [k, v] of Object.entries(values)) if (v && !(f[k] || '').trim()) next[k] = v;
      return next;
    });

  const onVerifyMode = (mode: string) => {
    setField('VERIFY_MODE')(mode);
    if (mode === 'command') {
      fillEmpty({ TEST_TIMEOUT_MIN: '10' });
      loadSuggestions().then((sg) => sg && fillEmpty({ TEST_COMMAND: sg.test_command }));
    } else if (mode === 'github_actions') {
      fillEmpty({ TEST_TIMEOUT_MIN: '20' });
      loadSuggestions();
    }
  };

  const onApproval = (value: string) => {
    setField('REQUIRE_PR_APPROVAL')(value);
    if (value === 'true') {
      loadSuggestions().then((sg) => sg && fillEmpty({ PROTECTED_PATHS: sg.protected_paths.join('\n') }));
    }
  };

  const detectedNote = suggestions && (suggestions.stack
    ? `Detected ${suggestions.stack} · ${suggestions.repo.split('/').pop()}@${suggestions.branch}`
    : suggestions.reason || `Couldn't recognise the stack of ${suggestions.repo || 'this repository'} — enter the command yourself`);

  useEffect(() => {
    listSkills(undefined, activeAppId)
      .then((s) => setSkills(s.filter((k) => !k.error)))
      .catch(() => {});
  }, [activeAppId]);

  // Drops responses for a scope/app the user has already switched away from.
  const requestSeq = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++requestSeq.current;
    try {
      if (scope === 'app') {
        const c = await getAppConfig(activeAppId);
        if (seq !== requestSeq.current) return;
        setProvider(((c.values.TICKET_PROVIDER || c.inherited.TICKET_PROVIDER || 'jira') as 'jira' | 'ado'));
        setFields(Object.fromEntries([...SETTING_KEYS, ...APP_DEFAULT_KEYS].map((k) => [k, c.values[k] ?? ''])));
        setInherited(c.inherited);
        setSecretState(c.secrets ?? NO_SECRETS);
      } else {
        const c = await getConfig();
        if (seq !== requestSeq.current) return;
        setProvider((c.TICKET_PROVIDER as 'jira' | 'ado') || 'jira');
        setFields(Object.fromEntries(SETTING_KEYS.map((k) => [k, c[k] ?? ''])));
        setInherited(null);
        setSecretState(Object.fromEntries(SECRET_KEYS.map((k) => [k, c[k]])));
      }
      setSecrets(EMPTY_SECRETS);
      setError(null);
    } catch (e: any) {
      setError(e.message);
    }
  }, [scope, activeAppId]);

  useEffect(() => {
    if (!me) return;
    if (!canEdit) { setLoading(false); return; }
    setLoading(true);
    setSaved(false);
    refresh().finally(() => setLoading(false));
  }, [refresh, me, canEdit]);

  // Server defaults affect every application, so only admins may save them.
  const canSave = scope === 'server' ? can('admin') : canEdit;

  const setField = (key: string) => (v: string) => setFields((f) => ({ ...f, [key]: v }));
  const setSecret = (key: string) => (v: string) => setSecrets((s) => ({ ...s, [key]: v }));
  const inh = (key: string) => (inherited ? inherited[key] ?? '' : undefined);

  const handleSave = async () => {
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      if (scope === 'app') {
        // Only pin the provider on this app when it actually differs from (or
        // already overrode) the server default — otherwise keep inheriting it.
        const inheritedProvider = inherited?.TICKET_PROVIDER || 'jira';
        const updates: AppConfigUpdate = {
          ...fields,
          ...secrets,
          TICKET_PROVIDER: provider !== inheritedProvider || fields.TICKET_PROVIDER ? provider : '',
        };
        await updateAppConfig(activeAppId, updates);
        await refreshApps();
      } else {
        const updates: ConfigUpdate = { TICKET_PROVIDER: provider, ...fields, ...secrets };
        await updateConfig(updates);
      }
      await refresh();
      setSaved(true);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  const appName = activeApp?.name ?? 'Application';

  return (
    <div className="bg-bg min-h-screen">
      <Nav />

      <div className="content">
        <PageHeader
          eyebrow="Integrations"
          title="Integration Config"
          description={scope === 'app'
            ? `Settings for ${appName}. Leave a field blank to inherit the server default shown in grey. Token/key fields never round-trip back to the browser. Changes apply to this application's next run.`
            : "Server defaults, written to the backend's .env file. Every application inherits these for any field it doesn't set itself."}
          actions={
            <>
              {saved && <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--color-green)' }}>✓ Saved</span>}
              <button
                onClick={handleSave}
                disabled={saving || loading || !canSave}
                className="btn-primary"
                style={{ opacity: saving || loading || !canSave ? 0.5 : 1 }}
              >
                {saving ? 'Saving…' : 'Save changes'}
              </button>
            </>
          }
        />

        <div className="segmented" style={{ marginBottom: 18 }}>
          <button className={scope === 'app' ? 'on' : ''} onClick={() => setScope('app')}>{appName}</button>
          <button className={scope === 'server' ? 'on' : ''} onClick={() => setScope('server')}>Server defaults (.env)</button>
        </div>

        {error && <div className="alert error">{error}</div>}
        {canEdit && !canSave && (
          <div className="locked-note" style={{ marginBottom: 16 }}>🔒 Server defaults apply to every application — only <b>admins</b> can save them. You can still view them.</div>
        )}

        {me && !canEdit ? (
          <div className="locked-note">🔒 Configuration is managed by <b>maintainers and admins</b>. Your role is <b>{me.role}</b>.</div>
        ) : loading ? (
          <div style={{ color: 'var(--color-text-muted)', fontSize: 14 }}>Loading...</div>
        ) : (
          <>
            <Section title="Ticket provider">
              <div style={{ display: 'flex', gap: 20, gridColumn: '1 / -1' }}>
                {(['jira', 'ado'] as const).map((p) => (
                  <label key={p} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 14, cursor: 'pointer' }}>
                    <input type="radio" name="provider" checked={provider === p} onChange={() => setProvider(p)} />
                    {p === 'jira' ? 'Jira' : 'Azure DevOps'}
                  </label>
                ))}
              </div>

              {provider === 'jira' ? (
                <>
                  <Field label="Jira Base URL" value={fields.JIRA_BASE_URL || ''} onChange={setField('JIRA_BASE_URL')} inherited={inh('JIRA_BASE_URL')} placeholder="https://your-domain.atlassian.net" />
                  <Field label="Jira User Email" value={fields.JIRA_USER_EMAIL || ''} onChange={setField('JIRA_USER_EMAIL')} inherited={inh('JIRA_USER_EMAIL')} placeholder="you@company.com" />
                  <SecretField label="Jira API Token" state={secretState.JIRA_API_TOKEN} value={secrets.JIRA_API_TOKEN} onChange={setSecret('JIRA_API_TOKEN')} />
                </>
              ) : (
                <>
                  <Field label="ADO Org URL" value={fields.ADO_ORG_URL || ''} onChange={setField('ADO_ORG_URL')} inherited={inh('ADO_ORG_URL')} placeholder="https://dev.azure.com/your-org" />
                  <Field label="ADO Project" value={fields.ADO_PROJECT || ''} onChange={setField('ADO_PROJECT')} inherited={inh('ADO_PROJECT')} placeholder="Your-Project" />
                  <SecretField label="ADO Personal Access Token" state={secretState.ADO_PAT} value={secrets.ADO_PAT} onChange={setSecret('ADO_PAT')} />
                </>
              )}
            </Section>

            <Section title="Outbound notifications">
              <BooleanField
                label="Notify on successful runs"
                value={fields.NOTIFY_ON_SUCCESS || ''}
                inherited={inh('NOTIFY_ON_SUCCESS')}
                onChange={setField('NOTIFY_ON_SUCCESS')}
              />
              <BooleanField
                label="Notify on failed or paused runs"
                value={fields.NOTIFY_ON_FAILURE || ''}
                inherited={inh('NOTIFY_ON_FAILURE')}
                onChange={setField('NOTIFY_ON_FAILURE')}
              />
              <SecretField label="Slack incoming webhook URL" state={secretState.SLACK_WEBHOOK_URL} value={secrets.SLACK_WEBHOOK_URL || ''} onChange={setSecret('SLACK_WEBHOOK_URL')} />
              <SecretField label="Teams incoming webhook URL" state={secretState.TEAMS_WEBHOOK_URL} value={secrets.TEAMS_WEBHOOK_URL || ''} onChange={setSecret('TEAMS_WEBHOOK_URL')} />
              <Field label="Email recipients (comma-separated)" value={fields.NOTIFY_EMAIL_TO || ''} onChange={setField('NOTIFY_EMAIL_TO')} inherited={inh('NOTIFY_EMAIL_TO')} placeholder="team@example.com" />
              <Field label="SMTP host" value={fields.SMTP_HOST || ''} onChange={setField('SMTP_HOST')} inherited={inh('SMTP_HOST')} placeholder="smtp.example.com" />
              <Field label="SMTP port" value={fields.SMTP_PORT || ''} onChange={setField('SMTP_PORT')} inherited={inh('SMTP_PORT')} placeholder="587" />
              <Field label="Email sender address" value={fields.SMTP_FROM || ''} onChange={setField('SMTP_FROM')} inherited={inh('SMTP_FROM')} placeholder="sdlc-bot@example.com" />
              <Field label="SMTP username (optional)" value={fields.SMTP_USERNAME || ''} onChange={setField('SMTP_USERNAME')} inherited={inh('SMTP_USERNAME')} />
              <SecretField label="SMTP password (optional)" state={secretState.SMTP_PASSWORD} value={secrets.SMTP_PASSWORD || ''} onChange={setSecret('SMTP_PASSWORD')} />
              <BooleanField
                label="Use SMTP STARTTLS"
                value={fields.SMTP_STARTTLS || ''}
                inherited={inh('SMTP_STARTTLS')}
                defaultValue
                onChange={setField('SMTP_STARTTLS')}
              />
            </Section>

            {scope === 'app' && provider === 'jira' && (
              <Section title="Inbound Jira webhook">
                <SecretField
                  label="Webhook bearer token"
                  state={secretState.JIRA_WEBHOOK_TOKEN}
                  value={secrets.JIRA_WEBHOOK_TOKEN || ''}
                  onChange={setSecret('JIRA_WEBHOOK_TOKEN')}
                />
                <div style={{ gridColumn: '1 / -1', fontSize: 12, color: 'var(--color-text-muted)' }}>
                  Configure Jira Automation to POST the issue key to{' '}
                  <code>/api/webhooks/jira/{activeAppId}</code> with the header{' '}
                  <code>Authorization: Bearer &lt;token&gt;</code>. The token is stored
                  as a secret and is never shown again. Jira must be able to reach
                  this backend over HTTPS; localhost alone is not reachable from Jira.
                </div>
              </Section>
            )}

            {scope === 'app' && provider === 'ado' && (
              <Section title="Inbound Azure DevOps webhook">
                <SecretField
                  label="Service Hook password"
                  state={secretState.ADO_WEBHOOK_TOKEN}
                  value={secrets.ADO_WEBHOOK_TOKEN || ''}
                  onChange={setSecret('ADO_WEBHOOK_TOKEN')}
                />
                <div style={{ gridColumn: '1 / -1', fontSize: 12, color: 'var(--color-text-muted)' }}>
                  Configure an Azure DevOps Web Hooks service-hook subscription for work-item
                  created/updated events. Use URL <code>/api/webhooks/ado/{activeAppId}</code> and
                  Basic authentication with username <code>sdlc-agentic</code> and the configured
                  password. The app&apos;s WIQL is checked before a run starts. Azure DevOps must
                  reach this backend over HTTPS; localhost alone is not reachable from Azure DevOps.
                </div>
              </Section>
            )}

            <Section title="GitHub">
              <Field label="Default target repo" value={fields.TARGET_REPO || ''} onChange={setField('TARGET_REPO')} inherited={inh('TARGET_REPO')} placeholder="owner/repo" />
              <Field label="Default target branch" value={fields.TARGET_BRANCH || ''} onChange={setField('TARGET_BRANCH')} inherited={inh('TARGET_BRANCH')} placeholder="dev" />
              <SecretField label="GitHub Token" state={secretState.GITHUB_TOKEN} value={secrets.GITHUB_TOKEN} onChange={setSecret('GITHUB_TOKEN')} />
            </Section>

            <Section title="Azure OpenAI">
              <Field label="Endpoint" value={fields.AZURE_OPENAI_ENDPOINT || ''} onChange={setField('AZURE_OPENAI_ENDPOINT')} inherited={inh('AZURE_OPENAI_ENDPOINT')} placeholder="https://your-resource.cognitiveservices.azure.com/" />
              <Field label="Deployment" value={fields.AZURE_OPENAI_DEPLOYMENT || ''} onChange={setField('AZURE_OPENAI_DEPLOYMENT')} inherited={inh('AZURE_OPENAI_DEPLOYMENT')} placeholder="gpt-4.1-mini" />
              <SecretField label="API Key" state={secretState.AZURE_OPENAI_API_KEY} value={secrets.AZURE_OPENAI_API_KEY} onChange={setSecret('AZURE_OPENAI_API_KEY')} />
            </Section>

            <Section title="Status / state names">
              <Field label="Done" value={fields.STATUS_DONE || ''} onChange={setField('STATUS_DONE')} inherited={inh('STATUS_DONE')} placeholder={provider === 'ado' ? 'Closed' : 'Done'} />
              <Field label="In progress" value={fields.STATUS_IN_PROGRESS || ''} onChange={setField('STATUS_IN_PROGRESS')} inherited={inh('STATUS_IN_PROGRESS')} placeholder={provider === 'ado' ? 'Active' : 'In Progress'} />
              <Field label="Needs human" value={fields.STATUS_FAILED || ''} onChange={setField('STATUS_FAILED')} inherited={inh('STATUS_FAILED')} placeholder={provider === 'ado' ? 'New' : 'Human In Loop'} />
            </Section>

            {scope === 'app' && (
              <Section title="Application defaults">
                <div>
                  <label className="label-main">Default persona</label>
                  <select className="input-main" value={fields.DEFAULT_PERSONA || ''} onChange={(e) => setField('DEFAULT_PERSONA')(e.target.value)}>
                    <option value="">Auto — pick from the ticket</option>
                    {skills.map((s) => <option key={s.slug} value={s.slug}>{s.name || s.slug}</option>)}
                  </select>
                  <div className="field-hint">A ticket that names a persona still wins</div>
                </div>
                <Field label="Branch prefix" value={fields.BRANCH_PREFIX || ''} onChange={setField('BRANCH_PREFIX')} placeholder="feat/" />
                <Field label="Clarity gate — min. words" value={fields.GATE_MIN_WORDS || ''} onChange={setField('GATE_MIN_WORDS')} placeholder="5" />
              </Section>
            )}

            {scope === 'app' && (
              <Section title="Verification — tests & CI">
                <div>
                  <label className="label-main">How to verify the agent&apos;s change</label>
                  <select className="input-main" value={fields.VERIFY_MODE || ''} onChange={(e) => onVerifyMode(e.target.value)}>
                    <option value="">Review the diff only (no automated tests)</option>
                    <option value="command">Run a test command before the PR</option>
                    <option value="github_actions">Wait for GitHub Actions on the PR</option>
                  </select>
                  <div className="field-hint">
                    {fields.VERIFY_MODE === 'command' ? 'Failing tests stop the run — nothing is pushed'
                      : fields.VERIFY_MODE === 'github_actions' ? 'A failing check hands the ticket back, with the PR link'
                      : 'The PR says honestly that nothing was tested'}
                  </div>
                  {fields.VERIFY_MODE && detectedNote && <div className="field-hint">🔎 {detectedNote}</div>}
                </div>
                {fields.VERIFY_MODE === 'command' && (
                  <>
                    <div>
                      <Field label="Setup command (optional)" value={fields.TEST_SETUP_COMMAND || ''} onChange={setField('TEST_SETUP_COMMAND')} placeholder={suggestions?.setup_command || 'pip install -r requirements.txt'} />
                      {suggestions?.setup_command && !fields.TEST_SETUP_COMMAND && (
                        <div className="field-hint" title="Not filled in automatically — it installs packages on the pipeline server">
                          <button type="button" className="link-btn" onClick={() => setField('TEST_SETUP_COMMAND')(suggestions.setup_command)}>Use</button>{' '}
                          <code>{suggestions.setup_command}</code>
                        </div>
                      )}
                    </div>
                    <Field label="Test command" value={fields.TEST_COMMAND || ''} onChange={setField('TEST_COMMAND')} placeholder="pytest -q   ·   npm test" />
                  </>
                )}
                {fields.VERIFY_MODE === 'github_actions' && (
                  <div>
                    <Field label="Workflow to trigger (optional)" value={fields.CI_WORKFLOW || ''} onChange={setField('CI_WORKFLOW')} placeholder="ci.yml — blank = wait for push/PR checks" />
                    {suggestions && (
                      <div className="field-hint">
                        {suggestions.ci_workflows.length
                          ? <>Workflows in the repo: {suggestions.ci_workflows.map((w, i) => (
                              <span key={w}>{i > 0 && ', '}<button type="button" className="link-btn" onClick={() => setField('CI_WORKFLOW')(w)}>{w}</button></span>
                            ))} — only pick one that has a workflow_dispatch trigger</>
                          : 'No workflows found in .github/workflows — this repo has no GitHub Actions to wait for'}
                      </div>
                    )}
                  </div>
                )}
                {fields.VERIFY_MODE && (
                  <Field label="Timeout (minutes)" value={fields.TEST_TIMEOUT_MIN || ''} onChange={setField('TEST_TIMEOUT_MIN')} placeholder="10" />
                )}
              </Section>
            )}

            {scope === 'app' && activeApp && (
              <Section title="Ownership">
                <div>
                  <label className="label-main">Team</label>
                  <select
                    className="input-main"
                    value={activeApp.team_id ?? ''}
                    disabled={!can('admin') || activeApp.is_default}
                    onChange={async (e) => {
                      try {
                        await updateApp(activeAppId, { team_id: e.target.value || null });
                        await Promise.all([refreshApps(), refreshTeams()]);
                        setSaved(true);
                      } catch (err: any) { setError(err.message); }
                    }}
                  >
                    <option value="">Shared — everyone can use it</option>
                    {teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                  </select>
                  <div className="field-hint">
                    {activeApp.is_default ? 'The default application is always shared'
                      : can('admin') ? 'Only this team (and admins) can see and run it — saved immediately'
                      : 'Only admins can move an application to another team'}
                  </div>
                </div>
              </Section>
            )}

            {scope === 'app' && (
              <Section title="Governance">
                <div>
                  <label className="label-main">Approval before pull request</label>
                  <select className="input-main" value={fields.REQUIRE_PR_APPROVAL || ''} onChange={(e) => onApproval(e.target.value)}>
                    <option value="">Not required (default)</option>
                    <option value="true">Required — a maintainer must approve</option>
                    <option value="false">Not required</option>
                  </select>
                  <div className="field-hint">When required, runs pause after the agent finishes until someone approves</div>
                </div>
                <div style={{ gridColumn: 'span 2' }}>
                  <label className="label-main">Protected paths (agent may never change these)</label>
                  <textarea
                    className="input-main"
                    rows={3}
                    value={fields.PROTECTED_PATHS || ''}
                    onChange={(e) => setField('PROTECTED_PATHS')(e.target.value)}
                    placeholder={['.github/workflows/*', 'ci/*', '*.tf'].join('\n')}
                    style={{ fontFamily: 'var(--font-mono)', fontSize: 12.5, resize: 'vertical' }}
                  />
                  <div className="field-hint">One pattern per line or comma-separated. A run that touches one is stopped before anything is pushed.</div>
                  {fields.REQUIRE_PR_APPROVAL === 'true' && suggestions && (
                    <div className="field-hint">
                      🔎 Suggested from {suggestions.repo || 'the repository'}: <code>{suggestions.protected_paths.join(', ')}</code>
                      {(fields.PROTECTED_PATHS || '').trim() !== suggestions.protected_paths.join('\n') && (
                        <> · <button type="button" className="link-btn" onClick={() => setField('PROTECTED_PATHS')(suggestions.protected_paths.join('\n'))}>Use these</button></>
                      )}
                    </div>
                  )}
                </div>
              </Section>
            )}
          </>
        )}
      </div>
    </div>
  );
}
