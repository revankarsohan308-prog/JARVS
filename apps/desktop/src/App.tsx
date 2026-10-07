import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ApiError,
  auth,
  chat,
  devices,
  memory,
  MEMORY_CATEGORIES,
  system,
  tasks,
  TASK_PRIORITIES,
  TASK_RECURRENCE,
  TASK_STATUSES,
  type DeviceRow,
  type HealthCheck,
  type MemoryRow,
  type MemoryStats,
  type PublicUser,
  type Task,
  type VoiceStatus,
} from './lib/api';

type CoreState = 'IDLE' | 'THINKING' | 'ERROR' | 'OFFLINE';

const NAV = [
  'Home', 'Chat', 'Tasks', 'Memory', 'Projects', 'Files',
  'Automation', 'Devices', 'Security', 'Notifications', 'Subscription', 'Owner Control',
] as const;

const PRIORITY_RANK: Record<string, number> = { low: 0, normal: 1, high: 2, urgent: 3 };

// Backend stores datetimes as naive UTC serialized with a 'Z'. Inputs (date /
// datetime-local) are interpreted as the owner's local time, so display must
// convert back to local — slicing the raw string would show UTC (off-by-one day
// for UTC+ timezones).
function fmtDate(v: string | null): string {
  if (!v) return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(v);
  if (!m) return v;
  if (v.endsWith('Z')) {
    const d = new Date(v);
    if (!Number.isNaN(d.getTime())) {
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    }
  }
  return `${m[1]}-${m[2]}-${m[3]}`;
}

function fmtTime(v: string | null): string {
  if (!v) return '';
  if (v.endsWith('Z')) {
    const d = new Date(v);
    if (!Number.isNaN(d.getTime())) {
      return `${fmtDate(v)} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    }
  }
  return v.slice(0, 16).replace('T', ' ');
}

export default function App() {
  const [user, setUser] = useState<PublicUser | null>(null);
  const [health, setHealth] = useState<HealthCheck | null>(null);
  const [healthPending, setHealthPending] = useState(true);
  const [tasksList, setTasksList] = useState<Task[]>([]);
  const [memories, setMemories] = useState<MemoryRow[]>([]);
  const [memStats, setMemStats] = useState<MemoryStats | null>(null);
  const [history, setHistory] = useState<{ role: 'user' | 'jarvis'; text: string }[]>([]);
  const [core, setCore] = useState<CoreState>('IDLE');
  const [loginError, setLoginError] = useState('');
  const [notice, setNotice] = useState('');
  const [active, setActive] = useState<string>('Home');
  const timer = useRef<number | null>(null);

  const refreshHealth = useCallback(async () => {
    setHealthPending(true);
    try {
      setHealth(await system.health());
    } catch {
      setHealth(null);
    } finally {
      setHealthPending(false);
    }
  }, []);

  const loadData = useCallback(async () => {
    try {
      const me = await auth.me();
      setUser(me.user);
      const [t, m] = await Promise.all([tasks.list(true), memory.list()]);
      setTasksList(t.tasks);
      setMemories(m.memories);
      setMemStats(m.stats);
      await refreshHealth();
    } catch (err) {
      if (err instanceof ApiError && err.status === 0) setNotice(err.message);
    }
  }, [refreshHealth]);

  useEffect(() => {
    auth.me().then(
      (me) => {
        setUser(me.user);
        void loadData();
      },
      () => {
        void refreshHealth(); // still show backend status on the login screen
      },
    );
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [loadData, refreshHealth]);

  useEffect(() => {
    timer.current = window.setInterval(() => void refreshHealth(), 15000);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [refreshHealth]);

  const doLogin = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoginError('');
    const fd = new FormData(e.currentTarget);
    try {
      const me = await auth.login(String(fd.get('username')), String(fd.get('password')));
      setUser(me.user);
      await loadData();
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : 'Login failed.');
    }
  };

  const doLogout = async () => {
    try {
      await auth.logout();
    } catch {
      /* session may already be gone */
    }
    setUser(null);
    setTasksList([]);
    setMemories([]);
    setMemStats(null);
    setHistory([]);
  };

  const sendChat = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const input = e.currentTarget.elements.namedItem('message') as HTMLInputElement;
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    setHistory((h) => [...h, { role: 'user', text }]);
    setCore('THINKING');
    try {
      const res = await chat.send(text);
      setHistory((h) => [...h, { role: 'jarvis', text: res.text }]);
      const [t, m] = await Promise.all([tasks.list(true), memory.list()]);
      setTasksList(t.tasks);
      setMemories(m.memories);
      setMemStats(m.stats);
      setCore('IDLE');
    } catch (err) {
      setHistory((h) => [
        ...h,
        { role: 'jarvis', text: err instanceof Error ? err.message : 'Chat failed.' },
      ]);
      setCore('ERROR');
    }
  };

  if (!user) {
    return <LoginView health={health} healthPending={healthPending} onLogin={doLogin} error={loginError} notice={notice} />;
  }

  const backendOnline = health?.status === 'ONLINE';
  const aiMode = health?.checks.ai_provider.mode ?? 'unknown';
  const aiLabel =
    aiMode === 'cloud'
      ? `AI ONLINE (${health!.checks.ai_provider.model})`
      : aiMode === 'degraded-to-local'
        ? 'AI: LOCAL RULES — add an OpenAI key'
        : 'AI OFFLINE';
  const today = new Date().toISOString().slice(0, 10);
  const pending = tasksList
    .filter((t) => t.status === 'pending')
    .sort((a, b) => (PRIORITY_RANK[b.priority] ?? 1) - (PRIORITY_RANK[a.priority] ?? 1));

  return (
    <div className="shell">
      <TopBar
        health={backendOnline}
        healthPending={healthPending}
        security={backendOnline ? 'SECURE' : 'OFFLINE'}
        user={user}
        aiLabel={aiLabel}
        onLogout={doLogout}
      />
      <div className="body">
        <nav className="sidebar">
          {NAV.map((n) => (
            <button
              key={n}
              className={active === n ? 'active' : ''}
              onClick={() => setActive(n)}
            >
              {n}
            </button>
          ))}
        </nav>

        <main className="center">
          <AIcore state={core} />
          {active === 'Home' || active === 'Chat' ? (
            <>
              <h2>{user.display_name || user.username}, how can I help you?</h2>
              <div className="chat-feed">
                {history.map((m, i) => (
                  <div key={i} className={`chat-msg ${m.role}`}>
                    {m.text}
                  </div>
                ))}
              </div>
              <form className="chat-input" onSubmit={sendChat}>
                <input name="message" placeholder="Ask J.A.R.V.S — e.g. “remind me to submit my assignment tomorrow” or “system status”" autoComplete="off" />
                <button type="submit">Send</button>
              </form>
              <div className="mic-row">
                <button className="mic" title="Voice is NOT IMPLEMENTED yet — real STT/TTS engine comes in a later phase">
                  🎙 <span>Voice: NOT IMPLEMENTED</span>
                </button>
              </div>
            </>
          ) : active === 'Tasks' ? (
            <TaskPanel tasks={tasksList} onMutated={loadData} />
          ) : active === 'Memory' ? (
            <MemoryPanel memories={memories} stats={memStats} onMutated={loadData} />
          ) : active === 'Devices' ? (
            <DevicesPanel />
          ) : (
            <div className="placeholder">
              <strong>{active}</strong>
              <p>Foundation screen. Real data wiring lands with its phase.</p>
            </div>
          )}
        </main>

        <aside className="right">
          <section>
            <h3>Today · {today}</h3>
            {pending.length === 0 ? (
              <p className="muted">No pending tasks.</p>
            ) : (
              <ul className="task-list">
                {pending.slice(0, 6).map((t) => (
                  <li key={t.id} className={t.priority === 'high' ? 'high' : ''}>
                    <span className="dot" /> {t.title}
                    <small>{t.status.replace('_', ' ')}</small>
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section>
            <h3>System</h3>
            <ul className="kv">
              <li><span>Backend</span><b className={backendOnline ? 'ok' : 'err'}>{healthPending ? 'CHECKING…' : backendOnline ? 'ONLINE' : 'OFFLINE'}</b></li>
              <li><span>Database</span><b className="ok">{health?.checks.database.status === 'ONLINE' ? 'ONLINE' : 'OFFLINE'}</b></li>
              <li><span>AI</span><b className={aiMode === 'cloud' ? 'ok' : 'warn'}>{aiLabel}</b></li>
            </ul>
          </section>
          <section>
            <h3>Recent memories</h3>
            {memories.length === 0 ? (
              <p className="muted">Nothing stored yet.</p>
            ) : (
              <ul className="memory-list">
                {memories.slice(0, 4).map((m) => (
                  <li key={m.id}>
                    <span className="tag">{m.category}</span>
                    {m.content.slice(0, 60)}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}

/* ---------- pieces ---------- */

function TopBar(props: {
  health: boolean;
  healthPending: boolean;
  security: string;
  user: PublicUser;
  aiLabel: string;
  onLogout: () => void;
}) {
  return (
    <header className="topbar">
      <div className="brand">
        <span className="logo" aria-hidden>◈</span>
        <div>
          <strong>J.A.R.V.S</strong>
          <small>JUST A RATHER VERY INTELLIGENT SYSTEM</small>
        </div>
      </div>
      <div className="statuses">
        <span className={props.healthPending ? 'warn' : props.health ? 'ok' : 'err'}>
          {props.healthPending ? 'SYSTEM CHECKING' : props.health ? 'SYSTEM ONLINE' : 'SYSTEM OFFLINE'}
        </span>
        <span className="ok">SECURITY: {props.security}</span>
        <span className={props.aiLabel.startsWith('AI ONLINE') ? 'ok' : 'warn'}>{props.aiLabel}</span>
      </div>
      <div className="user">
        <span className="avatar">{props.user.display_name?.charAt(0) || props.user.username.charAt(0)}</span>
        <div>
          <b>{props.user.display_name || props.user.username}</b>
          <small>{props.user.role}</small>
        </div>
        <button onClick={props.onLogout} title="Sign out">Sign out</button>
      </div>
    </header>
  );
}

function AIcore({ state }: { state: CoreState }) {
  const cls = `core ${state.toLowerCase()}`;
  return (
    <div className={`core-wrap ${state === 'IDLE' ? '' : 'busy'}`} aria-live="polite">
      <div className={cls}>
        <div className="ring r1" />
        <div className="ring r2" />
        <div className="ring r3" />
        <span className="state">{state}</span>
      </div>
    </div>
  );
}

function LoginView(props: {
  health: HealthCheck | null;
  healthPending: boolean;
  onLogin: (e: React.FormEvent<HTMLFormElement>) => void;
  error: string;
  notice: string;
}) {
  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="brand center">
          <span className="logo big" aria-hidden>◈</span>
          <h1>J.A.R.V.S</h1>
          <p>JUST A RATHER VERY INTELLIGENT SYSTEM</p>
        </div>
        {props.notice && <div className="notice">{props.notice}</div>}
        <div className="kv-line">
          Backend: <b className={props.healthPending ? 'warn' : props.health ? 'ok' : 'err'}>{props.healthPending ? 'CHECKING…' : props.health ? 'ONLINE' : 'OFFLINE'}</b>
          {props.health && (
            <>
              · Database: <b className="ok">{props.health.checks.database.status}</b>
              · AI: <b className="warn">{props.health.checks.ai_provider.mode}</b>
            </>
          )}
        </div>
        <form onSubmit={props.onLogin}>
          <label>Username or email</label>
          <input name="username" autoComplete="username" required autoFocus />
          <label>Password</label>
          <input name="password" type="password" autoComplete="current-password" required />
          {props.error && <div className="error">{props.error}</div>}
          <button type="submit">Sign in</button>
        </form>
        <p className="hint">Use the account you created, or demo-admin / demo-pass-123.</p>
        <p className="hint">Google/LinkedIn sign-in: configured only after the owner adds OAuth credentials — the API reports exactly what’s missing.</p>
      </div>
    </div>
  );
}

function TaskPanel({ tasks: all, onMutated }: { tasks: Task[]; onMutated: () => Promise<void> }) {
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState({
    title: '',
    notes: '',
    priority: 'normal',
    due_at: '',
    recurrence: '',
    reminder_at: '',
  });

  const run = async (fn: () => Promise<unknown>): Promise<boolean> => {
    setError('');
    setBusy(true);
    try {
      await fn();
      await onMutated();
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action failed.');
      return false;
    } finally {
      setBusy(false);
    }
  };

  const create = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!draft.title.trim()) return;
    const ok = await run(() =>
      tasks.create({
        title: draft.title.trim(),
        notes: draft.notes,
        priority: draft.priority,
        due_at: draft.due_at || null,
        recurrence: draft.recurrence,
        reminder_at: draft.reminder_at || null,
      }),
    );
    if (ok) {
      setShowForm(false);
      setDraft({ title: '', notes: '', priority: 'normal', due_at: '', recurrence: '', reminder_at: '' });
    }
  };

  const complete = (id: number) => run(() => tasks.complete(id));
  const setStatus = (id: number, status: string) => run(() => tasks.update(id, { status }));
  const remove = (id: number) => {
    if (window.confirm('Delete this task permanently?')) void run(() => tasks.remove(id));
  };
  const clearCompleted = () => run(tasks.clearCompleted);

  const groups = TASK_STATUSES.map((s) => ({
    status: s,
    label: s.toUpperCase(),
    items: all
      .filter((t) => t.status === s)
      .sort((a, b) => (PRIORITY_RANK[b.priority] ?? 1) - (PRIORITY_RANK[a.priority] ?? 1)),
  }));

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Tasks</h2>
        <div className="row-actions">
          <button className="btn" onClick={() => setShowForm((v) => !v)}>
            {showForm ? 'Close form' : '+ New task'}
          </button>
          <button className="btn" onClick={clearCompleted} disabled={busy}>
            Clear completed
          </button>
        </div>
      </div>
      {error && <div className="error">{error}</div>}
      {showForm && (
        <form className="grid-form" onSubmit={create}>
          <label>
            Title *
            <input
              value={draft.title}
              onChange={(e) => setDraft({ ...draft, title: e.target.value })}
              placeholder="e.g. Submit physics assignment"
              required
            />
          </label>
          <label>
            Priority
            <select value={draft.priority} onChange={(e) => setDraft({ ...draft, priority: e.target.value })}>
              {TASK_PRIORITIES.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </label>
          <label>
            Due date
            <input type="date" value={draft.due_at} onChange={(e) => setDraft({ ...draft, due_at: e.target.value })} />
          </label>
          <label>
            Repeat
            <select value={draft.recurrence} onChange={(e) => setDraft({ ...draft, recurrence: e.target.value })}>
              {TASK_RECURRENCE.map((r) => (
                <option key={r} value={r}>{r === '' ? 'none' : r}</option>
              ))}
            </select>
          </label>
          <label>
            Reminder
            <input
              type="datetime-local"
              value={draft.reminder_at}
              onChange={(e) => setDraft({ ...draft, reminder_at: e.target.value })}
            />
          </label>
          <label className="span2">
            Notes
            <textarea rows={2} value={draft.notes} onChange={(e) => setDraft({ ...draft, notes: e.target.value })} />
          </label>
          <div className="row-actions span2">
            <button type="submit" className="btn ok" disabled={busy}>{busy ? 'Saving…' : 'Create task'}</button>
            <button type="button" className="btn" onClick={() => setShowForm(false)}>Cancel</button>
          </div>
        </form>
      )}
      {all.length === 0 ? (
        <p className="muted">No tasks yet — create one above or from the Home chat.</p>
      ) : (
        groups.map((g) =>
          g.items.length === 0 ? null : (
            <section key={g.status}>
              <h3>{g.label} · {g.items.length}</h3>
              <ul className="task-list wide">
                {g.items.map((t) => (
                  <li key={t.id} className={`p-${t.priority}`}>
                    <div className="trow">
                      <b>{t.title}</b>
                      <span className={`badge p-${t.priority}`}>{t.priority}</span>
                    </div>
                    {t.notes ? <p className="muted">{t.notes}</p> : null}
                    <small className="meta">
                      {t.due_at ? `due ${fmtDate(t.due_at)}` : 'no due date'}
                      {t.recurrence ? ` · repeats ${t.recurrence}` : ''}
                      {t.reminder_at ? ` · reminder ${fmtTime(t.reminder_at)}` : ''}
                      {t.status === 'done' && t.completed_at ? ` · done ${fmtDate(t.completed_at)}` : ''}
                    </small>
                    <div className="row-actions">
                      {t.status === 'pending' && (
                        <>
                          <button className="btn ok" disabled={busy} onClick={() => complete(t.id)}>Complete</button>
                          <button className="btn" disabled={busy} onClick={() => setStatus(t.id, 'cancelled')}>Cancel</button>
                        </>
                      )}
                      {(t.status === 'done' || t.status === 'cancelled') && (
                        <button className="btn" disabled={busy} onClick={() => setStatus(t.id, 'pending')}>Reopen</button>
                      )}
                      <button className="btn err" disabled={busy} onClick={() => remove(t.id)}>Delete</button>
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          ),
        )
      )}
      <p className="footnote">
        Engine statuses: pending · done · cancelled. The spec’s TODO / IN_PROGRESS / BLOCKED / OVERDUE
        statuses are a future engine upgrade and are NOT IMPLEMENTED.
      </p>
    </div>
  );
}

function MemoryPanel({
  memories,
  stats,
  onMutated,
}: {
  memories: MemoryRow[];
  stats: MemoryStats | null;
  onMutated: () => Promise<void>;
}) {
  const [rows, setRows] = useState<MemoryRow[]>(memories);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [category, setCategory] = useState('');
  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [draft, setDraft] = useState({ key: '', content: '', category: 'IMPORTANT_FACTS' });
  const [editing, setEditing] = useState<number | null>(null);
  const [edit, setEdit] = useState({ key: '', content: '', category: 'IMPORTANT_FACTS' });

  useEffect(() => {
    if (!searching) setRows(memories);
  }, [memories, searching]);

  const run = async (fn: () => Promise<unknown>): Promise<boolean> => {
    setError('');
    setBusy(true);
    try {
      await fn();
      await onMutated();
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action failed.');
      return false;
    } finally {
      setBusy(false);
    }
  };

  const create = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!draft.key.trim() || !draft.content.trim()) return;
    const ok = await run(() =>
      memory.create({ key: draft.key.trim(), content: draft.content, category: draft.category }),
    );
    if (ok) {
      setShowForm(false);
      setDraft({ key: '', content: '', category: 'IMPORTANT_FACTS' });
    }
  };

  const saveEdit = async (id: number) => {
    const ok = await run(() => memory.update(id, { key: edit.key, content: edit.content, category: edit.category }));
    if (ok) setEditing(null);
  };

  const remove = (id: number) => {
    if (window.confirm('Delete this memory permanently?')) void run(() => memory.remove(id));
  };

  const doSearch = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const q = query.trim();
    if (!q) {
      setSearching(false);
      setRows(memories);
      return;
    }
    setError('');
    setBusy(true);
    try {
      const res = await memory.search(q);
      setRows(res.results);
      setSearching(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Search failed.');
    } finally {
      setBusy(false);
    }
  };

  const clearSearch = () => {
    setQuery('');
    setSearching(false);
    setRows(memories);
  };

  const exportMemories = async () => {
    setError('');
    try {
      const data = await memory.export();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `jarvis-memory-${data.exported_for}-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Export failed.');
    }
  };

  const visible = category ? rows.filter((m) => m.category === category) : rows;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Memory</h2>
        <div className="row-actions">
          <button className="btn" onClick={exportMemories}>Export JSON</button>
          <button className="btn" onClick={() => setShowForm((v) => !v)}>
            {showForm ? 'Close form' : '+ Remember'}
          </button>
        </div>
      </div>
      {error && <div className="error">{error}</div>}
      {showForm && (
        <form className="grid-form" onSubmit={create}>
          <label>
            Key *
            <input
              value={draft.key}
              onChange={(e) => setDraft({ ...draft, key: e.target.value })}
              placeholder="e.g. hometown"
              required
            />
          </label>
          <label>
            Category
            <select value={draft.category} onChange={(e) => setDraft({ ...draft, category: e.target.value })}>
              {MEMORY_CATEGORIES.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </label>
          <label className="span2">
            Content *
            <textarea rows={2} value={draft.content} onChange={(e) => setDraft({ ...draft, content: e.target.value })} required />
          </label>
          <div className="row-actions span2">
            <button type="submit" className="btn ok" disabled={busy}>{busy ? 'Saving…' : 'Store memory'}</button>
            <button type="button" className="btn" onClick={() => setShowForm(false)}>Cancel</button>
          </div>
        </form>
      )}
      <div className="search-row">
        <form onSubmit={doSearch}>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search memories…"
            autoComplete="off"
          />
          <button type="submit" className="btn">Search</button>
        </form>
        {searching && (
          <button className="btn" onClick={clearSearch}>Clear search</button>
        )}
        <select value={category} onChange={(e) => setCategory(e.target.value)}>
          <option value="">All categories</option>
          {MEMORY_CATEGORIES.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
      </div>
      {stats && (
        <div className="stats-row">
          <span className="chip">Total: {stats.total}</span>
          {MEMORY_CATEGORIES.map((c) =>
            (stats.by_category[c] ?? 0) > 0 ? (
              <span key={c} className="chip">{c}: {stats.by_category[c]}</span>
            ) : null,
          )}
        </div>
      )}
      {visible.length === 0 ? (
        <p className="muted">
          {searching
            ? 'No matches. Clear the search to browse everything.'
            : 'Nothing stored. Say “remember …” in chat or add a memory above.'}
        </p>
      ) : (
        <ul className="memory-list wide">
          {visible.map((m) =>
            editing === m.id ? (
              <li key={m.id} className="mem-edit">
                <input
                  value={edit.key}
                  onChange={(e) => setEdit({ ...edit, key: e.target.value })}
                  placeholder="key"
                />
                <select value={edit.category} onChange={(e) => setEdit({ ...edit, category: e.target.value })}>
                  {MEMORY_CATEGORIES.map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
                <textarea rows={2} value={edit.content} onChange={(e) => setEdit({ ...edit, content: e.target.value })} />
                <div className="row-actions">
                  <button className="btn ok" disabled={busy} onClick={() => saveEdit(m.id)}>Save</button>
                  <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
                </div>
              </li>
            ) : (
              <li key={m.id}>
                <span className="tag">{m.category}</span>
                <b>{m.key}</b>
                <span>{m.content}</span>
                <small>{m.source} · {m.updated_at.slice(0, 10)}</small>
                <div className="row-actions">
                  <button
                    className="btn"
                    onClick={() => {
                      setEditing(m.id);
                      setEdit({ key: m.key, content: m.content, category: m.category });
                    }}
                  >
                    Edit
                  </button>
                  <button className="btn err" disabled={busy} onClick={() => remove(m.id)}>Delete</button>
                </div>
              </li>
            ),
          )}
        </ul>
      )}
    </div>
  );
}

function DevicesPanel() {
  const [list, setList] = useState<DeviceRow[]>([]);
  const [voice, setVoice] = useState<VoiceStatus | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [pairState, setPairState] = useState<'idle' | 'code' | 'token'>('idle');
  const [code, setCode] = useState('');
  const [expiresIn, setExpiresIn] = useState(0);
  const [codeInput, setCodeInput] = useState('');
  const [name, setName] = useState('J.A.R.V.S LINK');
  const [token, setToken] = useState<string | null>(null);
  const [newDeviceId, setNewDeviceId] = useState('');
  const [tokenCopied, setTokenCopied] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [d, v] = await Promise.all([devices.list(), devices.voiceStatus()]);
      setList(d.devices);
      setVoice(v);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load devices.');
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), 15000);
    return () => window.clearInterval(id);
  }, [refresh]);

  // One-second countdown while a pairing code is live.
  useEffect(() => {
    if (pairState !== 'code') return;
    const id = window.setInterval(() => setExpiresIn((s) => Math.max(0, s - 1)), 1000);
    return () => window.clearInterval(id);
  }, [pairState]);

  const startPairing = async () => {
    setError('');
    setBusy(true);
    try {
      const res = await devices.pairing(name || 'J.A.R.V.S LINK');
      setCode(res.code);
      setExpiresIn(res.expires_in_seconds);
      setPairState('code');
      setToken(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start pairing.');
    } finally {
      setBusy(false);
    }
  };

  const pair = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const c = codeInput.trim();
    if (c.length !== 6) return;
    setError('');
    setBusy(true);
    try {
      const dev = await devices.pair(c, name || 'J.A.R.V.S LINK');
      setToken(dev.token ?? null);
      setNewDeviceId(dev.device_id);
      setPairState('token');
      setCode('');
      setCodeInput('');
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Pairing failed.');
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (deviceId: string) => {
    if (!window.confirm('Revoke this device? It will be disconnected and its token stops working.')) return;
    setError('');
    setBusy(true);
    try {
      await devices.revoke(deviceId);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Revoke failed.');
    } finally {
      setBusy(false);
    }
  };

  const copyToken = async () => {
    if (!token) return;
    try {
      await navigator.clipboard.writeText(token);
      setTokenCopied(true);
      window.setTimeout(() => setTokenCopied(false), 2000);
    } catch {
      /* clipboard unavailable in some containers */
    }
  };

  const connected = new Set(voice?.connected_devices ?? []);
  const fmtCountdown = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  const voiceLabel =
    voice == null ? 'Voice server CHECKING…' : voice.voice_server === 'ONLINE' ? 'Voice server ONLINE' : 'Voice server OFFLINE';

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Devices · J.A.R.V.S LINK</h2>
        <div className="row-actions">
          <button className="btn" onClick={() => void refresh()}>Refresh</button>
          <button className="btn ok" onClick={startPairing} disabled={busy}>
            {pairState === 'code' ? 'Generate new code' : '+ Start pairing'}
          </button>
        </div>
      </div>
      {error && <div className="error">{error}</div>}

      <div className="stats-row">
        <span className="chip">{voiceLabel}</span>
        {voice && voice.stt && (
          <span className="chip" title={voice.stt.detail}>
            STT: {voice.stt.available ? `${voice.stt.provider}${voice.stt.model ? ` · ${voice.stt.model}` : ''}` : 'OFFLINE'}
          </span>
        )}
        {voice && voice.tts && (
          <span className="chip" title={voice.tts.detail}>
            TTS: {voice.tts.available ? `${voice.tts.provider}${voice.tts.voice ? ` · ${voice.tts.voice}` : ''}` : 'OFFLINE'}
          </span>
        )}
        <span className="chip">{connected.size} connected now</span>
      </div>

      {pairState === 'code' && (
        <div className="pair-box">
          <h3>Pairing code</h3>
          <div className="pair-code">{code}</div>
          <p className="muted">
            {expiresIn > 0
              ? `Expires in ${fmtCountdown(expiresIn)}. `
              : 'This code has expired — generate a new one. '}
            Enter it below to receive the one-time device token.
          </p>
          <form className="grid-form" onSubmit={pair}>
            <label>
              Pairing code
              <input
                value={codeInput}
                onChange={(e) => setCodeInput(e.target.value.toUpperCase())}
                maxLength={6}
                placeholder="e.g. K7M2QP"
                required
              />
            </label>
            <label>
              Device name
              <input value={name} onChange={(e) => setName(e.target.value)} />
            </label>
            <div className="row-actions span2">
              <button type="submit" className="btn ok" disabled={busy || expiresIn <= 0 || codeInput.length !== 6}>
                {busy ? 'Pairing…' : 'Pair device'}
              </button>
            </div>
          </form>
        </div>
      )}

      {pairState === 'token' && token && (
        <div className="pair-box token-box">
          <h3>Device paired — one-time token</h3>
          <p className="muted">
            This token is shown exactly once; only its SHA-256 digest is stored server-side. Flash it onto the ESP32:
          </p>
          <ol className="token-steps">
            <li>Serial console: <code>set id {newDeviceId}</code></li>
            <li>Then: <code>set token {token}</code></li>
            <li>The LINK connects to the voice plane on <code>:8761</code> automatically.</li>
          </ol>
          <pre className="token">{token}</pre>
          <div className="row-actions">
            <button className="btn" onClick={copyToken}>{tokenCopied ? 'Copied ✓' : 'Copy token'}</button>
            <button className="btn" onClick={() => setPairState('idle')}>Done</button>
          </div>
        </div>
      )}

      {list.length === 0 ? (
        <p className="muted">
          No devices. Start pairing to enroll your first J.A.R.V.S LINK — the one-time token is flashed on the
          ESP32 through its serial console.
        </p>
      ) : (
        <ul className="device-list">
          {list.map((d) => {
            const online = connected.has(d.device_id);
            return (
              <li key={d.device_id} className={d.paired ? '' : 'pending'}>
                <div className="trow">
                  <b>{d.name}</b>
                  {d.paired ? (
                    online ? (
                      <span className="badge ok">ONLINE</span>
                    ) : (
                      <span className="badge">OFFLINE</span>
                    )
                  ) : (
                    <span className="badge warn">Pending pairing</span>
                  )}
                </div>
                <small className="meta">
                  {d.device_id} · {d.kind} · fw {d.firmware || 'unknown'}
                  {d.paired && ` · last seen ${d.last_seen_at ? fmtTime(d.last_seen_at) : 'never'}`}
                  {!d.paired && ' · superseded when a new pairing code is generated'}
                </small>
                {d.paired && d.battery_pct != null && (
                  <div className="tele">
                    <span className="batt">
                      <i style={{ '--fill': `${d.battery_pct}%` } as React.CSSProperties} />
                      {d.battery_pct}%
                    </span>
                    {d.signal_dbm != null && <span className="muted">{d.signal_dbm} dBm</span>}
                    {d.state ? <span className="muted">state: {d.state}</span> : null}
                  </div>
                )}
                {d.paired && (
                  <div className="row-actions">
                    <button className="btn err" disabled={busy} onClick={() => revoke(d.device_id)}>Revoke</button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}