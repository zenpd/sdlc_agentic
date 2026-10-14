// Thin client for the FastAPI backend (proxied through Next's /api/:path* rewrite
// in next.config.ts, so these are always same-origin relative requests).

export type StepState = 'pending' | 'in-progress' | 'success' | 'failed' | 'error' | 'human';

export type StepInfo = {
  key: string;
  label: string;
  state: StepState;
};

export type RunStatus = 'in-progress' | 'success' | 'failed' | 'error' | 'human';

export type RunRecord = {
  id: string;
  app_id: string;
  triggered_by: string | null;
  ticket_key: string;
  status: RunStatus;
  pr_url: string | null;
  started_at: string;
  duration_sec: number | null;
  steps: StepInfo[];
  logs: Record<string, string[]>;
};

export type RunStats = {
  total: number;
  success: number;
  failed: number;
  human: number;
  success_rate: number;
  avg_duration_sec: number | null;
  daily: Array<{ date: string; success: number; failed: number }>;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      // response body wasn't JSON — fall back to statusText
    }
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json();
}

const appQuery = (appId?: string) => (appId ? `?app_id=${encodeURIComponent(appId)}` : '');

// appId scopes to one onboarded application; omit it for every application.
export function getStats(appId?: string): Promise<RunStats> {
  return request<RunStats>(`/api/stats${appQuery(appId)}`);
}

export function getRuns(appId?: string): Promise<RunRecord[]> {
  return request<RunRecord[]>(`/api/runs${appQuery(appId)}`);
}

export function getRunStatus(runId: string): Promise<RunRecord> {
  return request<RunRecord>(`/api/run-status/${runId}`);
}

export function runTicket(ticketKey: string, appId?: string): Promise<{ run_id: string; app_id: string }> {
  return request<{ run_id: string; app_id: string }>('/api/run-ticket', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticket_key: ticketKey, app_id: appId }),
  });
}

export type SkillSummary = {
  slug: string;
  name?: string;
  description?: string;
  sdlc_stage?: string;
  keywords?: string[];
  error?: string;
};

export type SkillContent = {
  slug: string;
  content: string;
};

export function listSkills(): Promise<SkillSummary[]> {
  return request<SkillSummary[]>('/api/skills');
}

export function getSkill(slug: string): Promise<SkillContent> {
  return request<SkillContent>(`/api/skills/${slug}`);
}

export function saveSkill(slug: string, content: string): Promise<SkillContent> {
  return request<SkillContent>(`/api/skills/${slug}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
}

export function createSkill(slug: string, content: string): Promise<SkillContent> {
  return request<SkillContent>(`/api/skills?slug=${encodeURIComponent(slug)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
}

// Non-secret fields come back as plain strings; secret fields come back as
// {set: boolean} so a raw token/key value is never present in the response.
export type SecretField = { set: boolean };

export type IntegrationConfig = {
  TICKET_PROVIDER: string;
  JIRA_BASE_URL: string;
  JIRA_USER_EMAIL: string;
  JIRA_JQL: string;
  ADO_ORG_URL: string;
  ADO_PROJECT: string;
  ADO_ASSIGNEE: string;
  ADO_WIQL: string;
  TARGET_REPO: string;
  TARGET_BRANCH: string;
  AZURE_OPENAI_ENDPOINT: string;
  AZURE_OPENAI_DEPLOYMENT: string;
  STATUS_DONE: string;
  STATUS_IN_PROGRESS: string;
  STATUS_FAILED: string;
  JIRA_API_TOKEN: SecretField;
  ADO_PAT: SecretField;
  GITHUB_TOKEN: SecretField;
  AZURE_OPENAI_API_KEY: SecretField;
};

export type ConfigUpdate = Partial<Record<keyof IntegrationConfig, string>>;

export function getConfig(): Promise<IntegrationConfig> {
  return request<IntegrationConfig>('/api/config');
}

export function updateConfig(updates: ConfigUpdate): Promise<IntegrationConfig> {
  return request<IntegrationConfig>('/api/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
}

// ── Onboarded applications ───────────────────────────────────────────
export type Application = {
  id: string;
  name: string;
  team: string;
  description: string;
  is_default: boolean;
  ticket_provider: 'jira' | 'ado';
  target_repo: string;
  target_branch: string;
  default_persona: string;
  created_at: string;
  updated_at: string;
};

export const APP_SECRET_FIELDS = ['JIRA_API_TOKEN', 'ADO_PAT', 'GITHUB_TOKEN', 'AZURE_OPENAI_API_KEY'] as const;
export type AppSecretKey = typeof APP_SECRET_FIELDS[number];
export type AppSettingKey = Exclude<keyof IntegrationConfig, AppSecretKey> | 'DEFAULT_PERSONA' | 'BRANCH_PREFIX' | 'GATE_MIN_WORDS';

// values = this app's own overrides ('' = not overridden), inherited = the
// server-wide (.env) value a blank field falls back to, effective = what a
// run actually uses. Secrets only ever come back as {set, inherited}.
export type AppConfig = {
  app: Application;
  values: Record<AppSettingKey, string>;
  inherited: Record<AppSettingKey, string>;
  effective: Record<AppSettingKey, string>;
  // null for viewers/runners — they don't get to know which tokens are set.
  secrets: Record<AppSecretKey, { set: boolean; inherited: boolean }> | null;
};

export type AppConfigUpdate = Partial<Record<AppSettingKey | AppSecretKey, string>>;

export function listApps(): Promise<Application[]> {
  return request<Application[]>('/api/apps');
}

export function createApp(body: { name: string; team?: string; description?: string; config?: AppConfigUpdate }): Promise<Application> {
  return request<Application>('/api/apps', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function updateApp(appId: string, body: { name?: string; team?: string; description?: string }): Promise<Application> {
  return request<Application>(`/api/apps/${encodeURIComponent(appId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function deleteApp(appId: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/api/apps/${encodeURIComponent(appId)}`, { method: 'DELETE' });
}

export function getAppConfig(appId: string): Promise<AppConfig> {
  return request<AppConfig>(`/api/apps/${encodeURIComponent(appId)}/config`);
}

export function updateAppConfig(appId: string, updates: AppConfigUpdate): Promise<AppConfig> {
  return request<AppConfig>(`/api/apps/${encodeURIComponent(appId)}/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
}

// ── GitHub OAuth login + repo picker ─────────────────────────────────
// Lowest to highest; each role can do everything the ones before it can.
export const ROLES = ['viewer', 'runner', 'maintainer', 'admin'] as const;
export type Role = typeof ROLES[number];

export const ROLE_INFO: Record<Role, string> = {
  viewer: 'Watch runs, logs, and dashboards',
  runner: 'Viewer + start pipeline runs',
  maintainer: 'Runner + edit application config, skills, and repositories',
  admin: 'Everything, including onboarding applications, server defaults, and users',
};

export const hasRole = (role: Role | undefined, minimum: Role) =>
  !!role && ROLES.indexOf(role) >= ROLES.indexOf(minimum);

export type GithubUser = {
  login: string;
  avatar_url: string | null;
  role: Role;
};

export type GithubRepo = {
  full_name: string;
  private: boolean;
  default_branch: string;
  updated_at: string | null;
  description: string | null;
};

export const GITHUB_LOGIN_PATH = '/api/auth/github/login';

export async function getGithubUser(): Promise<GithubUser | null> {
  try {
    return await request<GithubUser>('/api/auth/github/me');
  } catch {
    return null;
  }
}

export function logoutGithub(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>('/api/auth/github/logout', { method: 'POST' });
}

export function getGithubRepos(): Promise<GithubRepo[]> {
  return request<GithubRepo[]>('/api/github/repos');
}

export function selectGithubRepo(fullName: string, defaultBranch: string | undefined, appId: string): Promise<AppConfig> {
  return request<AppConfig>('/api/github/select-repo', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ full_name: fullName, default_branch: defaultBranch, app_id: appId }),
  });
}

// ── Users, roles, audit log ──────────────────────────────────────────
export type UserRecord = {
  login: string;
  avatar_url: string | null;
  role: Role;
  created_at: string;
  last_login_at: string;
};

export type AuditEntry = {
  id: number;
  at: string;
  actor: string;
  action: string;
  app_id: string | null;
  target: string | null;
  details: Record<string, any>;
};

export function listUsers(): Promise<UserRecord[]> {
  return request<UserRecord[]>('/api/users');
}

export function updateUserRole(login: string, role: Role): Promise<UserRecord> {
  return request<UserRecord>(`/api/users/${encodeURIComponent(login)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role }),
  });
}

export function getAudit(params: { appId?: string; actor?: string } = {}): Promise<AuditEntry[]> {
  const q = new URLSearchParams();
  if (params.appId) q.set('app_id', params.appId);
  if (params.actor) q.set('actor', params.actor);
  const qs = q.toString();
  return request<AuditEntry[]>(`/api/audit${qs ? `?${qs}` : ''}`);
}

export function formatDuration(sec: number | null): string {
  if (sec == null) return '—';
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m ${s}s`;
}

export function formatTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
