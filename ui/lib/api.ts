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

export function getStats(): Promise<RunStats> {
  return request<RunStats>('/api/stats');
}

export function getRuns(): Promise<RunRecord[]> {
  return request<RunRecord[]>('/api/runs');
}

export function getRunStatus(runId: string): Promise<RunRecord> {
  return request<RunRecord>(`/api/run-status/${runId}`);
}

export function runTicket(ticketKey: string): Promise<{ run_id: string }> {
  return request<{ run_id: string }>('/api/run-ticket', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticket_key: ticketKey }),
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

// ── GitHub OAuth login + repo picker ─────────────────────────────────
export type GithubUser = {
  login: string;
  avatar_url: string | null;
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

export function selectGithubRepo(fullName: string, defaultBranch?: string): Promise<IntegrationConfig> {
  return request<IntegrationConfig>('/api/github/select-repo', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ full_name: fullName, default_branch: defaultBranch }),
  });
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
