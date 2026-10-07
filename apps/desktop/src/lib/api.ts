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

// Engine truth — these match core/tasks/service.py and core/memory/service.py.
// The spec's richer statuses (TODO/IN_PROGRESS/BLOCKED/OVERDUE) are a future
// engine upgrade, honestly NOT IMPLEMENTED today.
export const TASK_STATUSES = ['pending', 'done', 'cancelled'] as const;
export const TASK_PRIORITIES = ['low', 'normal', 'high', 'urgent'] as const;
export const TASK_RECURRENCE = ['', 'daily', 'weekly', 'monthly'] as const;
export const MEMORY_CATEGORIES = [
  'PREFERENCES',
  'IMPORTANT_FACTS',
  'PROJECTS',
  'TASKS',
  'CONVERSATION_SUMMARY',
] as const;

export interface MemoryStats {
  total: number;
  by_category: Record<string, number>;
}

export interface DeviceRow {
  device_id: string;
  name: string;
  kind: string;
  firmware: string;
  state: string;
  battery_pct: number | null;
  signal_dbm: number | null;
  last_seen_at: string | null;
  paired: boolean;
  created_at: string;
  token?: string | null;
}

export interface VoicePlaneStatus {
  provider: string;
  model?: string;
  voice?: string;
  language?: string;
  available: boolean;
  detail: string;
}

export interface VoiceStatus {
  voice_server: string;
  stt: VoicePlaneStatus | null;
  tts: VoicePlaneStatus | null;
  connected_devices: string[];
}

export const devices = {
  list: () => api<{ devices: DeviceRow[] }>('/devices'),
  pairing: (name = 'J.A.R.V.S LINK') =>
    api<{ code: string; expires_at: number; expires_in_seconds: number }>('/devices/pairing', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  pair: (code: string, name = 'J.A.R.V.S LINK') =>
    api<DeviceRow>('/devices/pair', { method: 'POST', body: JSON.stringify({ code, name }) }),
  revoke: (deviceId: string) => api<{ ok: boolean }>(`/devices/${deviceId}`, { method: 'DELETE' }),
  voiceStatus: () => api<VoiceStatus>('/devices/voice-status'),
};

export interface MemoryExport {
  exported_for: string;
  category: string;
  count: number;
  memories: MemoryRow[];
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

export interface TaskDraft {
  title: string;
  notes?: string;
  priority?: string;
  due_at?: string | null;
  recurrence?: string;
  reminder_at?: string | null;
}

export const tasks = {
  list: (includeCompleted = false, day?: string) => {
    const q = new URLSearchParams();
    q.set('include_completed', String(includeCompleted));
    if (day) q.set('day', day);
    return api<{ tasks: Task[] }>(`/tasks?${q.toString()}`);
  },
  create: (input: TaskDraft) =>
    api<{ ok: boolean; task: Task }>('/tasks', {
      method: 'POST',
      body: JSON.stringify({
        title: input.title,
        notes: input.notes ?? '',
        priority: input.priority ?? 'normal',
        due_at: input.due_at || null,
        recurrence: input.recurrence ?? '',
        reminder_at: input.reminder_at || null,
      }),
    }),
  update: (id: number, patch: Partial<TaskDraft & { status: string }>) =>
    api<{ ok: boolean; task: Task }>(`/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
  complete: (id: number) =>
    api<{ ok: boolean; task: Task }>(`/tasks/${id}/complete`, { method: 'POST' }),
  remove: (id: number) =>
    api<{ ok: boolean; deleted: number }>(`/tasks/${id}`, { method: 'DELETE' }),
  clearCompleted: () =>
    api<{ ok: boolean; deleted: number }>('/tasks/clear-completed', { method: 'POST' }),
};

export const memory = {
  list: (category?: string) =>
    api<{ memories: MemoryRow[]; stats: MemoryStats }>(
      `/memory${category ? `?category=${encodeURIComponent(category)}` : ''}`,
    ),
  search: (q: string) =>
    api<{ results: MemoryRow[] }>(`/memory/search?q=${encodeURIComponent(q)}`),
  export: () => api<MemoryExport>('/memory/export'),
  create: (input: { key: string; content: string; category?: string }) =>
    api<{ ok: boolean; memory: MemoryRow }>('/memory', {
      method: 'POST',
      body: JSON.stringify({
        key: input.key,
        content: input.content,
        category: input.category ?? 'IMPORTANT_FACTS',
      }),
    }),
  update: (id: number, patch: { key?: string; content?: string; category?: string }) =>
    api<{ ok: boolean; memory: MemoryRow }>(`/memory/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
  remove: (id: number) =>
    api<{ ok: boolean; deleted: number }>(`/memory/${id}`, { method: 'DELETE' }),
};

export const chat = {
  send: (message: string) =>
    api<{ text: string; tools: unknown[]; needs_confirmation: boolean; confirmation_id: string }>(
      '/brain/chat',
      { method: 'POST', body: JSON.stringify({ message }) },
    ),
};