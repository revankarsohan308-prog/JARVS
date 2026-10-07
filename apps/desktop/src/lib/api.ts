// Typed client for the J.A.R.V.S API. Session cookie is sent automatically
// (SameSite=Lax, HttpOnly — never stored in the renderer).
// Dev mode runs on :5173 (CORS-enabled); the packaged app is served by the
// backend at /jarvis/ so API calls are same-origin.
const API_ORIGIN = 'http://127.0.0.1:8760';
const servedByBackend = window.location.origin === API_ORIGIN;
const BASE = servedByBackend ? '/api/v1' : `${API_ORIGIN}/api/v1`;

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    ...(init.headers as Record<string, string>),
  };
  if (init.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  let resp: Response;
  try {
    resp = await fetch(`${BASE}${path}`, { ...init, headers, credentials: 'include' });
  } catch {
    throw new ApiError(0, 'Backend unreachable. Start the J.A.R.V.S backend, then retry.');
  }
  let body: any = null;
  try {
    body = await resp.json();
  } catch {
    /* non-JSON error body */
  }
  if (!resp.ok) {
    const detail =
      body?.detail ||
      (typeof body === 'string' ? body : `Request failed (${resp.status}).`);
    throw new ApiError(resp.status, typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return body as T;
}

export interface PublicUser {
  id: number;
  public_id: string;
  username: string;
  display_name: string;
  role: string;
  email: string | null;
  conversation_history_enabled: boolean;
  long_term_memory_enabled: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface Task {
  id: number;
  title: string;
  notes: string | null;
  priority: string;
  status: string;
  due_at: string | null;
  recurrence: string | null;
  reminder_at: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface MemoryRow {
  id: number;
  category: string;
  key: string;
  content: string;
  source: string;
  created_at: string;
  updated_at: string;
}

export interface HealthCheck {
  status: string;
  version: string;
  environment: string;
  checks: {
    database: { status: string; detail?: string };
    ai_provider: { provider: string; model: string; available: boolean; detail?: string; mode?: string };
  };
}

export const auth = {
  me: () => api<{ user: PublicUser }>('/auth/me'),
  login: (username: string, password: string) =>
    api<{ user: PublicUser }>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: () => api<{ ok: boolean }>('/auth/logout', { method: 'POST' }),
};

export const system = {
  health: () => api<HealthCheck>('/system/health'),
};

export const tasks = {
  list: (includeCompleted = false) =>
    api<{ tasks: Task[] }>(`/tasks?include_completed=${includeCompleted}`),
};

export const memory = {
  list: () => api<{ memories: MemoryRow[] }>('/memory'),
};

export const chat = {
  send: (message: string) =>
    api<{ text: string; tools: unknown[]; needs_confirmation: boolean; confirmation_id: string }>(
      '/brain/chat',
      { method: 'POST', body: JSON.stringify({ message }) },
    ),
};