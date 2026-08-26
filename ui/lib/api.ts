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
