// Thin client for the FastAPI backend (proxied through Next's /api/:path* rewrite
// in next.config.ts, so these are always same-origin relative requests).

export type StepState = 'pending' | 'in-progress' | 'success' | 'failed' | 'error' | 'human';

export type StepInfo = {
  key: string;
  label: string;
  state: StepState;
};

export type RunStatus = 'in-progress' | 'awaiting-approval' | 'success' | 'failed' | 'error' | 'human';

// Set when the run's application requires sign-off before a PR opens.
export type RunApproval = {
  status: 'pending' | 'approved' | 'rejected' | 'expired';
  by: string | null;
  note: string | null;
  at: string | null;
};
export type CostStatus = 'pending' | 'estimated' | 'partial' | 'unavailable';

export type RunRecord = {
  id: string;
  app_id: string;
  triggered_by: string | null;
  ticket_key: string;
  status: RunStatus;
  pr_url: string | null;
  started_at: string;
  duration_sec: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
  cost_status: CostStatus;
  llm_request_count: number;
  priced_request_count: number;
  steps: StepInfo[];
  logs: Record<string, string[]>;
  approval: RunApproval | null;
};

export type RunStats = {
  total: number;
  success: number;
  failed: number;
  human: number;
  success_rate: number;
  avg_duration_sec: number | null;
  monthly_cost_usd: number | null;
  monthly_prompt_tokens: number;
  monthly_completion_tokens: number;
  monthly_usage_runs: number;
  monthly_incomplete_cost_runs: number;
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

// mine=true -> only runs the signed-in user started, across every application.
export function getRuns(appId?: string, opts: { mine?: boolean } = {}): Promise<RunRecord[]> {
  const q = new URLSearchParams();
  if (appId) q.set('app_id', appId);
  if (opts.mine) q.set('mine', 'true');
  const qs = q.toString();
  return request<RunRecord[]>(`/api/runs${qs ? `?${qs}` : ''}`);
}

export function decideApproval(runId: string, decision: 'approve' | 'reject', note = ''): Promise<RunRecord> {
  return request<RunRecord>(`/api/runs/${encodeURIComponent(runId)}/approval`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision, note }),
  });
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
  loads_in?: string[];
  source?: 'shared' | 'app';
  overrides_shared?: boolean;
  error?: string;
};

export type SkillContent = {
  slug: string;
  content: string;
};

export type SkillVersion = {
  version_id: string;
  saved_at: string;
  size_bytes: number;
};

export type MarketplaceSkill = {
  slug: string;
  name: string;
  description: string;
  license: string;
  is_installed: boolean;
};

export function listSkills(stage?: string, appId?: string): Promise<SkillSummary[]> {
  const params = new URLSearchParams();
  if (stage) params.set('stage', stage);
  if (appId) params.set('app_id', appId);
  const queryString = params.toString();
  const query = queryString ? `?${queryString}` : '';
  return request<SkillSummary[]>(`/api/skills${query}`);
}

export function getStageMap(): Promise<Record<string, string[]>> {
  return request<Record<string, string[]>>('/api/stages');
}

export function listMarketplaceSkills(appId?: string): Promise<MarketplaceSkill[]> {
  const query = appId ? `?app_id=${encodeURIComponent(appId)}` : '';
  return request<MarketplaceSkill[]>(`/api/skills/marketplace${query}`);
}

export function getMarketplaceSkill(slug: string): Promise<SkillContent> {
  return request<SkillContent>(
    `/api/skills/marketplace/${encodeURIComponent(slug)}`,
  );
}

export function installMarketplaceSkill(
  slug: string,
  stage: string,
  appId: string,
): Promise<{ slug: string; app_id: string; stage: string }> {
  return request<{ slug: string; app_id: string; stage: string }>(
    `/api/skills/marketplace/${encodeURIComponent(slug)}/install`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stage, app_id: appId }),
    },
  );
}

const skillAppQuery = (appId?: string) => appId ? `?app_id=${encodeURIComponent(appId)}` : '';

export function getSkill(slug: string, appId?: string): Promise<SkillContent> {
  return request<SkillContent>(`/api/skills/${encodeURIComponent(slug)}${skillAppQuery(appId)}`);
}

export function saveSkill(slug: string, content: string, appId?: string): Promise<SkillContent> {
  return request<SkillContent>(`/api/skills/${encodeURIComponent(slug)}${skillAppQuery(appId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
}

export function createSkill(slug: string, content: string, appId?: string): Promise<SkillContent> {
  const params = new URLSearchParams({ slug });
  if (appId) params.set('app_id', appId);
  return request<SkillContent>(`/api/skills?${params.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
}

export function listSkillVersions(
  slug: string,
  appId?: string,
): Promise<SkillVersion[]> {
  return request<SkillVersion[]>(
    `/api/skills/${encodeURIComponent(slug)}/versions${skillAppQuery(appId)}`,
  );
}

export function getSkillVersion(
  slug: string,
  versionId: string,
  appId?: string,
): Promise<SkillContent & { version_id: string }> {
  return request<SkillContent & { version_id: string }>(
    `/api/skills/${encodeURIComponent(slug)}/versions/${encodeURIComponent(versionId)}${skillAppQuery(appId)}`,
  );
}

export function restoreSkillVersion(
  slug: string,
  versionId: string,
  appId?: string,
): Promise<SkillContent> {
  return request<SkillContent>(
    `/api/skills/${encodeURIComponent(slug)}/versions/${encodeURIComponent(versionId)}/restore${skillAppQuery(appId)}`,
    { method: 'POST' },
  );
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
  NOTIFY_ON_SUCCESS: string;
  NOTIFY_ON_FAILURE: string;
  NOTIFY_EMAIL_TO: string;
  SMTP_HOST: string;
  SMTP_PORT: string;
  SMTP_FROM: string;
  SMTP_USERNAME: string;
  SMTP_STARTTLS: string;
  JIRA_WEBHOOK_TOKEN: SecretField;
  ADO_WEBHOOK_TOKEN: SecretField;
  JIRA_API_TOKEN: SecretField;
  ADO_PAT: SecretField;
  GITHUB_TOKEN: SecretField;
  AZURE_OPENAI_API_KEY: SecretField;
  SLACK_WEBHOOK_URL: SecretField;
  TEAMS_WEBHOOK_URL: SecretField;
  SMTP_PASSWORD: SecretField;
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
  team_id: string | null; // null = shared with everyone
  team: string;           // team name ('' when shared)
  description: string;
  is_default: boolean;
  ticket_provider: 'jira' | 'ado';
  target_repo: string;
  target_branch: string;
  default_persona: string;
  require_pr_approval: boolean;
  protected_paths: string;
  verify_mode: '' | 'command' | 'github_actions';
  test_command: string;
  ci_workflow: string;
  created_at: string;
  updated_at: string;
};

export const APP_SECRET_FIELDS = [
  'JIRA_API_TOKEN', 'ADO_PAT', 'GITHUB_TOKEN', 'AZURE_OPENAI_API_KEY',
  'SLACK_WEBHOOK_URL', 'TEAMS_WEBHOOK_URL', 'SMTP_PASSWORD', 'JIRA_WEBHOOK_TOKEN',
  'ADO_WEBHOOK_TOKEN',
] as const;
export type AppSecretKey = typeof APP_SECRET_FIELDS[number];
export type AppSettingKey = Exclude<keyof IntegrationConfig, AppSecretKey>
  | 'DEFAULT_PERSONA' | 'BRANCH_PREFIX' | 'GATE_MIN_WORDS' | 'REQUIRE_PR_APPROVAL' | 'PROTECTED_PATHS'
  | 'VERIFY_MODE' | 'TEST_SETUP_COMMAND' | 'TEST_COMMAND' | 'TEST_TIMEOUT_MIN' | 'CI_WORKFLOW';

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

export function createApp(body: { name: string; team_id?: string | null; description?: string; config?: AppConfigUpdate }): Promise<Application> {
  return request<Application>('/api/apps', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

// Sending team_id (a team's id, or null for shared) moves the app — admins only.
export function updateApp(appId: string, body: { name?: string; description?: string; team_id?: string | null }): Promise<Application> {
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

export type Theme = 'light' | 'dark' | 'system';

// Saved server-side so they follow the person to any device or browser.
export type UserPreferences = {
  default_app_id?: string;
  theme?: Theme;
  active_team_id?: string; // narrows the app switcher to one team; never grants access
};

export type GithubUser = {
  login: string;
  avatar_url: string | null;
  role: Role;
  preferences: UserPreferences;
};

export function updatePreferences(updates: { default_app_id?: string; theme?: Theme | ''; active_team_id?: string }): Promise<UserPreferences> {
  return request<UserPreferences>('/api/me/preferences', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
}

export type SessionInfo = {
  id: string;
  user_agent: string;
  created_at: string;
  last_seen_at: string;
  current: boolean;
};

export function listMySessions(): Promise<SessionInfo[]> {
  return request<SessionInfo[]>('/api/me/sessions');
}

export function revokeMySession(id: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/api/me/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export function signOutOtherSessions(): Promise<{ signed_out: number }> {
  return request<{ signed_out: number }>('/api/me/sessions/sign-out-others', { method: 'POST' });
}

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

// ── Teams (who can see which applications) ───────────────────────────
export type Team = {
  id: string;
  name: string;
  created_at: string;
  members: string[];
  app_count: number;
};

export function listTeams(): Promise<Team[]> {
  return request<Team[]>('/api/teams');
}

export function createTeam(name: string): Promise<Team> {
  return request<Team>('/api/teams', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
}

export function deleteTeam(teamId: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/api/teams/${encodeURIComponent(teamId)}`, { method: 'DELETE' });
}

export function addTeamMember(teamId: string, login: string): Promise<Team> {
  return request<Team>(`/api/teams/${encodeURIComponent(teamId)}/members`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ login }),
  });
}

export function removeTeamMember(teamId: string, login: string): Promise<Team> {
  return request<Team>(`/api/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(login)}`, { method: 'DELETE' });
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
