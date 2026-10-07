import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ApiError,
  auth,
  chat,
  memory,
  system,
  tasks,
  type HealthCheck,
  type MemoryRow,
  type PublicUser,
  type Task,
} from './lib/api';

type CoreState = 'IDLE' | 'THINKING' | 'ERROR' | 'OFFLINE';

const NAV = [
  'Home', 'Chat', 'Tasks', 'Memory', 'Projects', 'Files',
  'Automation', 'Devices', 'Security', 'Notifications', 'Subscription', 'Owner Control',
] as const;

export default function App() {
  const [user, setUser] = useState<PublicUser | null>(null);
  const [health, setHealth] = useState<HealthCheck | null>(null);
  const [healthPending, setHealthPending] = useState(true);
  const [tasksList, setTasksList] = useState<Task[]>([]);
  const [memories, setMemories] = useState<MemoryRow[]>([]);
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
      const [t, m] = await Promise.all([tasks.list(), memory.list()]);
      setTasksList(t.tasks);
      setMemories(m.memories);
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
      const [t, m] = await Promise.all([tasks.list(), memory.list()]);
      setTasksList(t.tasks);
      setMemories(m.memories);
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
    .filter((t) => t.status !== 'COMPLETED' && t.status !== 'CANCELLED')
    .sort((a, b) => (b.priority === 'high' ? 1 : 0) - (a.priority === 'high' ? 1 : 0));

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
            <TaskPanel tasks={tasksList} />
          ) : active === 'Memory' ? (
            <MemoryPanel memories={memories} />
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

function TaskPanel({ tasks: all }: { tasks: Task[] }) {
  const groups = ['TODO', 'IN_PROGRESS', 'BLOCKED', 'COMPLETED', 'OVERDUE', 'CANCELLED'];
  return (
    <div className="panel">
      <h2>Tasks · real data from /api/v1/tasks</h2>
      {groups.map((g) => {
        const rows = all.filter((t) => t.status === g);
        if (rows.length === 0) return null;
        return (
          <section key={g}>
            <h3>{g.replace('_', ' ')}</h3>
            <ul className="task-list wide">
              {rows.map((t) => (
                <li key={t.id} className={t.priority === 'high' ? 'high' : ''}>
                  <b>{t.title}</b>
                  <small>
                    {t.priority} · {t.due_at ? `due ${t.due_at.slice(0, 10)}` : 'no due date'}
                  </small>
                </li>
              ))}
            </ul>
          </section>
        );
      })}
      {all.length === 0 && <p className="muted">No tasks yet — create one from the Home chat.</p>}
    </div>
  );
}

function MemoryPanel({ memories }: { memories: MemoryRow[] }) {
  return (
    <div className="panel">
      <h2>Memory · real data from /api/v1/memory</h2>
      {memories.length === 0 ? (
        <p className="muted">Nothing stored. Say “remember …” in chat and J.A.R.V.S will save it.</p>
      ) : (
        <ul className="memory-list wide">
          {memories.map((m) => (
            <li key={m.id}>
              <span className="tag">{m.category}</span>
              <b>{m.key}</b>
              <span>{m.content}</span>
              <small>{m.source} · {m.updated_at.slice(0, 10)}</small>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}