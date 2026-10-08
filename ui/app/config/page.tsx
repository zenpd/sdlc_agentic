'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import Nav from '@/components/Nav';
import PageHeader from '@/components/PageHeader';
import { useActiveApp } from '@/components/AppContext';
import {
  getConfig, updateConfig, getAppConfig, updateAppConfig, listSkills,
  type ConfigUpdate, type AppConfigUpdate, type SkillSummary,
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

type SecretState = { set: boolean; inherited?: boolean };

function SecretField({
  label, state, value, onChange,
}: { label: string; state: SecretState; value: string; onChange: (v: string) => void }) {
  const note = !state.set ? null : state.inherited ? '· inherited from server' : '· configured';
  return (
    <div>
      <label className="label-main">
        {label} {note && <span style={{ color: state.inherited ? 'var(--color-text-muted)' : 'var(--color-green)', fontWeight: 400 }}>{note}</span>}
      </label>
      <input
        className="input-main"
        type="password"
        value={value}
        placeholder={state.set ? '•••••••• (leave blank to keep current value)' : 'Not set'}
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
  'JIRA_BASE_URL', 'JIRA_USER_EMAIL', 'JIRA_JQL', 'ADO_ORG_URL', 'ADO_PROJECT', 'ADO_ASSIGNEE', 'ADO_WIQL',
  'TARGET_REPO', 'TARGET_BRANCH', 'AZURE_OPENAI_ENDPOINT', 'AZURE_OPENAI_DEPLOYMENT',
  'STATUS_DONE', 'STATUS_IN_PROGRESS', 'STATUS_FAILED',
] as const;
const APP_DEFAULT_KEYS = ['DEFAULT_PERSONA', 'BRANCH_PREFIX', 'GATE_MIN_WORDS'] as const;
const SECRET_KEYS = ['JIRA_API_TOKEN', 'ADO_PAT', 'GITHUB_TOKEN', 'AZURE_OPENAI_API_KEY'] as const;

const EMPTY_SECRETS: Record<string, string> = Object.fromEntries(SECRET_KEYS.map((k) => [k, '']));
const NO_SECRETS: Record<string, SecretState> = Object.fromEntries(SECRET_KEYS.map((k) => [k, { set: false }]));

type Scope = 'app' | 'server';

export default function ConfigPage() {
  const { activeApp, activeAppId, refreshApps } = useActiveApp();
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

  useEffect(() => {
    listSkills().then((s) => setSkills(s.filter((k) => !k.error))).catch(() => {});
  }, []);

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
        setSecretState(c.secrets);
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
    setLoading(true);
    setSaved(false);
    refresh().finally(() => setLoading(false));
  }, [refresh]);

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
                disabled={saving || loading}
                className="btn-primary"
                style={{ opacity: saving || loading ? 0.5 : 1 }}
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

        {loading ? (
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
                  <Field label="Jira JQL (optional override)" value={fields.JIRA_JQL || ''} onChange={setField('JIRA_JQL')} inherited={inh('JIRA_JQL')} placeholder='labels = "Agent-ready" AND status = "Ready for Agent"' />
                </>
              ) : (
                <>
                  <Field label="ADO Org URL" value={fields.ADO_ORG_URL || ''} onChange={setField('ADO_ORG_URL')} inherited={inh('ADO_ORG_URL')} placeholder="https://dev.azure.com/your-org" />
                  <Field label="ADO Project" value={fields.ADO_PROJECT || ''} onChange={setField('ADO_PROJECT')} inherited={inh('ADO_PROJECT')} placeholder="Your-Project" />
                  <SecretField label="ADO Personal Access Token" state={secretState.ADO_PAT} value={secrets.ADO_PAT} onChange={setSecret('ADO_PAT')} />
                  <Field label="ADO Assignee (optional)" value={fields.ADO_ASSIGNEE || ''} onChange={setField('ADO_ASSIGNEE')} inherited={inh('ADO_ASSIGNEE')} placeholder="you@company.com" />
                  <Field label="ADO WIQL (optional override)" value={fields.ADO_WIQL || ''} onChange={setField('ADO_WIQL')} inherited={inh('ADO_WIQL')} placeholder="Default: tag 'Agent-ready', state 'New'" />
                </>
              )}
            </Section>

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
          </>
        )}
      </div>
    </div>
  );
}
