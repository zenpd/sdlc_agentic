'use client';

import { useEffect, useState, useCallback } from 'react';
import Nav from '@/components/Nav';
import { getConfig, updateConfig, type IntegrationConfig, type ConfigUpdate } from '@/lib/api';

type FieldProps = {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
};

function Field({ label, value, onChange, placeholder, type = 'text' }: FieldProps) {
  return (
    <div>
      <label className="label-main">{label}</label>
      <input
        className="input-main"
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

function SecretField({
  label, set, value, onChange,
}: { label: string; set: boolean; value: string; onChange: (v: string) => void }) {
  return (
    <div>
      <label className="label-main">
        {label} {set && <span style={{ color: 'var(--color-green)', fontWeight: 400 }}>· configured</span>}
      </label>
      <input
        className="input-main"
        type="password"
        value={value}
        placeholder={set ? '•••••••• (leave blank to keep current value)' : 'Not set'}
        onChange={(e) => onChange(e.target.value)}
        autoComplete="new-password"
      />
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card-main" style={{ padding: 24, marginBottom: 20 }}>
      <h2 style={{ fontSize: 16, fontWeight: 700, marginBottom: 18 }}>{title}</h2>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 16 }}>
        {children}
      </div>
    </div>
  );
}

const EMPTY_SECRETS = {
  JIRA_API_TOKEN: '',
  ADO_PAT: '',
  GITHUB_TOKEN: '',
  AZURE_OPENAI_API_KEY: '',
};

export default function ConfigPage() {
  const [config, setConfig] = useState<IntegrationConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const [provider, setProvider] = useState<'jira' | 'ado'>('jira');
  const [fields, setFields] = useState<Record<string, string>>({});
  const [secrets, setSecrets] = useState<Record<string, string>>(EMPTY_SECRETS);

  const refresh = useCallback(async () => {
    try {
      const c = await getConfig();
      setConfig(c);
      setProvider((c.TICKET_PROVIDER as 'jira' | 'ado') || 'jira');
      setFields({
        JIRA_BASE_URL: c.JIRA_BASE_URL,
        JIRA_USER_EMAIL: c.JIRA_USER_EMAIL,
        JIRA_JQL: c.JIRA_JQL,
        ADO_ORG_URL: c.ADO_ORG_URL,
        ADO_PROJECT: c.ADO_PROJECT,
        ADO_ASSIGNEE: c.ADO_ASSIGNEE,
        ADO_WIQL: c.ADO_WIQL,
        TARGET_REPO: c.TARGET_REPO,
        TARGET_BRANCH: c.TARGET_BRANCH,
        AZURE_OPENAI_ENDPOINT: c.AZURE_OPENAI_ENDPOINT,
        AZURE_OPENAI_DEPLOYMENT: c.AZURE_OPENAI_DEPLOYMENT,
        STATUS_DONE: c.STATUS_DONE,
        STATUS_IN_PROGRESS: c.STATUS_IN_PROGRESS,
        STATUS_FAILED: c.STATUS_FAILED,
      });
      setSecrets(EMPTY_SECRETS);
      setError(null);
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    refresh().finally(() => setLoading(false));
  }, [refresh]);

  const setField = (key: string) => (v: string) => setFields((f) => ({ ...f, [key]: v }));
  const setSecret = (key: string) => (v: string) => setSecrets((s) => ({ ...s, [key]: v }));

  const handleSave = async () => {
    setSaving(true);
    setSaved(false);
    setError(null);
    const updates: ConfigUpdate = { TICKET_PROVIDER: provider, ...fields, ...secrets };
    try {
      const c = await updateConfig(updates);
      setConfig(c);
      setSecrets(EMPTY_SECRETS);
      setSaved(true);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

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
          <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 8, flexWrap: 'wrap' }}>
            <h1 style={{ fontSize: 24, fontWeight: 800, letterSpacing: '-0.01em' }}>Integration Config</h1>
            <div style={{ flex: 1 }} />
            {saved && <span style={{ fontSize: 13, color: 'var(--color-green)' }}>✓ Saved</span>}
            <button
              onClick={handleSave}
              disabled={saving || loading}
              className="btn-primary"
              style={{ padding: '10px 20px', fontSize: 13, opacity: saving || loading ? 0.5 : 1 }}
            >
              {saving ? 'Saving…' : 'Save changes'}
            </button>
          </div>
          <p style={{ fontSize: 13, color: 'var(--color-text-secondary)', maxWidth: 620 }}>
            Writes straight to the backend&apos;s .env file — never leaves this
            machine. Token/key fields never round-trip back to the browser; leave
            one blank to keep its current value. Most settings apply on the very
            next pipeline run, no restart needed.
          </p>
        </div>

        {error && (
          <div style={{
            marginBottom: 20, padding: '12px 16px', borderRadius: 10,
            background: 'rgba(220,38,38,.08)', color: 'var(--color-red)', fontSize: 13,
          }}>
            {error}
          </div>
        )}

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
                  <Field label="Jira Base URL" value={fields.JIRA_BASE_URL || ''} onChange={setField('JIRA_BASE_URL')} placeholder="https://your-domain.atlassian.net" />
                  <Field label="Jira User Email" value={fields.JIRA_USER_EMAIL || ''} onChange={setField('JIRA_USER_EMAIL')} placeholder="you@company.com" />
                  <SecretField label="Jira API Token" set={config?.JIRA_API_TOKEN.set ?? false} value={secrets.JIRA_API_TOKEN} onChange={setSecret('JIRA_API_TOKEN')} />
                  <Field label="Jira JQL (optional override)" value={fields.JIRA_JQL || ''} onChange={setField('JIRA_JQL')} placeholder='labels = "Agent-ready" AND status = "Ready for Agent"' />
                </>
              ) : (
                <>
                  <Field label="ADO Org URL" value={fields.ADO_ORG_URL || ''} onChange={setField('ADO_ORG_URL')} placeholder="https://dev.azure.com/your-org" />
                  <Field label="ADO Project" value={fields.ADO_PROJECT || ''} onChange={setField('ADO_PROJECT')} placeholder="Your-Project" />
                  <SecretField label="ADO Personal Access Token" set={config?.ADO_PAT.set ?? false} value={secrets.ADO_PAT} onChange={setSecret('ADO_PAT')} />
                  <Field label="ADO Assignee (optional)" value={fields.ADO_ASSIGNEE || ''} onChange={setField('ADO_ASSIGNEE')} placeholder="you@company.com" />
                  <Field label="ADO WIQL (optional override)" value={fields.ADO_WIQL || ''} onChange={setField('ADO_WIQL')} placeholder="Default: tag 'Agent-ready', state 'New'" />
                </>
              )}
            </Section>

            <Section title="GitHub">
              <Field label="Default target repo" value={fields.TARGET_REPO || ''} onChange={setField('TARGET_REPO')} placeholder="owner/repo" />
              <Field label="Default target branch" value={fields.TARGET_BRANCH || ''} onChange={setField('TARGET_BRANCH')} placeholder="dev" />
              <SecretField label="GitHub Token" set={config?.GITHUB_TOKEN.set ?? false} value={secrets.GITHUB_TOKEN} onChange={setSecret('GITHUB_TOKEN')} />
            </Section>

            <Section title="Azure OpenAI">
              <Field label="Endpoint" value={fields.AZURE_OPENAI_ENDPOINT || ''} onChange={setField('AZURE_OPENAI_ENDPOINT')} placeholder="https://your-resource.cognitiveservices.azure.com/" />
              <Field label="Deployment" value={fields.AZURE_OPENAI_DEPLOYMENT || ''} onChange={setField('AZURE_OPENAI_DEPLOYMENT')} placeholder="gpt-4.1-mini" />
              <SecretField label="API Key" set={config?.AZURE_OPENAI_API_KEY.set ?? false} value={secrets.AZURE_OPENAI_API_KEY} onChange={setSecret('AZURE_OPENAI_API_KEY')} />
            </Section>

            <Section title="Status / state names">
              <Field label="Done" value={fields.STATUS_DONE || ''} onChange={setField('STATUS_DONE')} placeholder={provider === 'ado' ? 'Closed' : 'Done'} />
              <Field label="In progress" value={fields.STATUS_IN_PROGRESS || ''} onChange={setField('STATUS_IN_PROGRESS')} placeholder={provider === 'ado' ? 'Active' : 'In Progress'} />
              <Field label="Needs human" value={fields.STATUS_FAILED || ''} onChange={setField('STATUS_FAILED')} placeholder={provider === 'ado' ? 'New' : 'Human In Loop'} />
            </Section>
          </>
        )}
      </div>
    </div>
  );
}
