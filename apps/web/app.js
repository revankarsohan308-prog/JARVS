/* ALYO web interface — talks only to the real /api/v1 endpoints. */
"use strict";

const API = "/api/v1";
let token = localStorage.getItem("alyo_token") || null;
let me = null;
let pendingConfirmation = null;

/* ---------------- helpers ---------------- */

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const resp = await fetch(API + path, Object.assign({}, opts, { headers }));
  let data = null;
  try { data = await resp.json(); } catch (_) { /* non-JSON */ }
  if (!resp.ok) {
    const detail = data && data.detail
      ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))
      : `Request failed (HTTP ${resp.status}).`;
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  return data;
}

let toastTimer = null;
function toast(text, isError = false) {
  const el = $("toast");
  el.textContent = text;
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4200);
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

/* ---------------- auth ---------------- */

let authMode = "login";

function showAuth() {
  $("app-view").classList.add("hidden");
  $("auth-view").classList.remove("hidden");
  loadProviders();
}

async function showApp() {
  try {
    me = await api("/auth/me");
    $("auth-view").classList.add("hidden");
    $("app-view").classList.remove("hidden");
    $("user-name").textContent = me.user.username;
    $("user-role").textContent = me.user.role;
    $("conn-badge").textContent = "online";
    $("conn-badge").classList.add("online");
    loadTasks();
    loadMemory();
    loadSystem();
    loadSecurity();
    loadPrivacy();
  } catch (err) {
    token = null;
    localStorage.removeItem("alyo_token");
    showAuth();
  }
}

$("tab-login").addEventListener("click", () => setAuthMode("login"));
$("tab-register").addEventListener("click", () => setAuthMode("register"));

function setAuthMode(mode) {
  authMode = mode;
  $("tab-login").classList.toggle("active", mode === "login");
  $("tab-register").classList.toggle("active", mode === "register");
  $("auth-submit").textContent = mode === "login" ? "Sign in" : "Create account";
  $("auth-hint").textContent = mode === "login"
    ? "The first account created becomes the administrator."
    : "Password: at least 8 characters. Later accounts start as STUDENT.";
  $("auth-error").classList.add("hidden");
}

$("auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = $("auth-username").value.trim();
  const password = $("auth-password").value;
  try {
    const data = await api(authMode === "login" ? "/auth/login" : "/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    token = data.token;
    localStorage.setItem("alyo_token", token);
    await showApp();
  } catch (err) {
    const box = $("auth-error");
    box.textContent = err.message;
    box.classList.remove("hidden");
  }
});

$("logout-btn").addEventListener("click", async () => {
  try { await api("/auth/logout", { method: "POST" }); } catch (_) { /* already gone */ }
  token = null;
  localStorage.removeItem("alyo_token");
  showAuth();
});

/* ---------------- social sign-in (OAuth 2.0) ---------------- */

async function loadProviders() {
  let list = [];
  try {
    const data = await api("/auth/oauth/providers");
    list = data.providers || [];
  } catch (_) { /* server unreachable — buttons stay hidden */ }
  document.querySelectorAll(".oauth-btn").forEach((btn) => {
    const p = list.find((x) => x.id === btn.dataset.provider);
    if (!p) {
      btn.classList.add("hidden");
      return;
    }
    btn.disabled = false;
    btn.classList.toggle("not-configured", !p.configured);
    btn.title = p.configured ? `Sign in with ${p.display_name}` : p.note;
    btn.onclick = () => {
      if (p.configured) {
        window.location.href = `${API}/auth/oauth/${p.id}/start`;
        return;
      }
      // Honest state: never pretend a provider works.
      const note = $("oauth-note");
      note.textContent = p.note;
      note.classList.remove("hidden");
    };
  });
}

/* ---------------- navigation ---------------- */

document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`panel-${btn.dataset.panel}`).classList.add("active");
  });
});

/* ---------------- chat ---------------- */

function addBubble(role, text, meta) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = text;
  if (meta) {
    const span = document.createElement("span");
    span.className = "meta";
    span.innerHTML = meta;
    div.appendChild(span);
  }
  const log = $("chat-log");
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

function toolMeta(tools) {
  if (!tools || !tools.length) return "";
  return tools
    .map((t) => `<span class="${t.verified ? "verified" : ""}">` +
      `${esc(t.name)} — ${t.verified ? "verified result" : "not executed"}</span>`)
    .join(" · ");
}

function showConfirmation(id, summary) {
  pendingConfirmation = id;
  $("confirm-text").textContent = summary || "ALYO needs your approval for this action.";
  $("confirm-bar").classList.remove("hidden");
}

function hideConfirmation() {
  pendingConfirmation = null;
  $("confirm-bar").classList.add("hidden");
}

$("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("chat-input").value.trim();
  if (!text) return;
  $("chat-input").value = "";
  addBubble("user", text);
  try {
    const data = await api("/brain/chat", {
      method: "POST",
      body: JSON.stringify({ message: text }),
    });
    addBubble("assistant", data.text, toolMeta(data.tools));
    if (data.needs_confirmation && data.confirmation_id) {
      showConfirmation(data.confirmation_id, data.confirmation_summary);
    }
  } catch (err) {
    addBubble("assistant", `Request failed: ${err.message}`);
  }
});

async function answerConfirmation(approve) {
  if (!pendingConfirmation) return;
  const id = pendingConfirmation;
  hideConfirmation();
  try {
    const data = await api(`/brain/confirmations/${id}`, {
      method: "POST",
      body: JSON.stringify({ confirmation_id: id, approve }),
    });
    addBubble("assistant", data.text, toolMeta(data.tools));
  } catch (err) {
    addBubble("assistant", `Confirmation failed: ${err.message}`);
  }
}

$("confirm-approve").addEventListener("click", () => answerConfirmation(true));
$("confirm-deny").addEventListener("click", () => answerConfirmation(false));

/* ---------------- tasks ---------------- */

async function loadTasks() {
  try {
    const includeDone = $("task-show-done").checked;
    const data = await api(`/tasks?include_completed=${includeDone}`);
    const box = $("task-list");
    box.innerHTML = "";
    if (!data.tasks.length) {
      box.innerHTML = `<div class="list-item"><span class="muted">No pending tasks.</span></div>`;
      return;
    }
    data.tasks.forEach((t) => {
      const row = document.createElement("div");
      row.className = "list-item";
      row.innerHTML =
        `<div class="grow"><div class="title">${esc(t.title)}</div>` +
        `<div class="sub">${t.priority} priority` +
        `${t.due_at ? " · due " + esc(fmtDate(t.due_at)) : ""}` +
        `${t.recurrence ? " · repeats " + esc(t.recurrence) : ""}</div></div>` +
        `<span class="pill ${t.status === "done" ? "done" : ""}">${esc(t.status)}</span>`;
      const done = document.createElement("button");
      done.textContent = "Done";
      done.className = "ghost";
      done.onclick = async () => {
        try { await api(`/tasks/${t.id}/complete`, { method: "POST" }); loadTasks(); }
        catch (err) { toast(err.message, true); }
      };
      const del = document.createElement("button");
      del.textContent = "Delete";
      del.className = "danger";
      del.onclick = async () => {
        try { await api(`/tasks/${t.id}`, { method: "DELETE" }); loadTasks(); }
        catch (err) { toast(err.message, true); }
      };
      row.appendChild(done);
      row.appendChild(del);
      box.appendChild(row);
    });
  } catch (err) {
    toast(`Tasks: ${err.message}`, true);
  }
}

$("task-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    title: $("task-title").value.trim(),
    priority: $("task-priority").value,
  };
  if ($("task-due").value) body.due_at = $("task-due").value;
  try {
    await api("/tasks", { method: "POST", body: JSON.stringify(body) });
    $("task-title").value = "";
    $("task-due").value = "";
    loadTasks();
  } catch (err) {
    toast(err.message, true);
  }
});
$("task-show-done").addEventListener("change", loadTasks);

/* ---------------- memory ---------------- */

async function loadMemory(query) {
  try {
    const path = query ? `/memory/search?q=${encodeURIComponent(query)}` : "/memory";
    const data = await api(path);
    const rows = query ? data.results : data.memories;
    const box = $("memory-list");
    box.innerHTML = "";
    if (!rows.length) {
      box.innerHTML = `<div class="list-item"><span class="muted">${query ? "Nothing matched." : "Nothing stored yet. Try: remember course = BBA"}</span></div>`;
      return;
    }
    rows.forEach((m) => {
      const row = document.createElement("div");
      row.className = "list-item";
      row.innerHTML =
        `<div class="grow"><div class="title">${esc(m.key)}</div>` +
        `<div class="sub">${esc(m.content)}${m.category ? " · " + esc(m.category) : ""}</div></div>`;
      const del = document.createElement("button");
      del.textContent = "Forget";
      del.className = "danger";
      del.onclick = async () => {
        try { await api(`/memory/${m.id}`, { method: "DELETE" }); loadMemory(); }
        catch (err) { toast(err.message, true); }
      };
      row.appendChild(del);
      box.appendChild(row);
    });
  } catch (err) {
    toast(`Memory: ${err.message}`, true);
  }
}

$("memory-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/memory", {
      method: "POST",
      body: JSON.stringify({
        key: $("memory-key").value.trim(),
        content: $("memory-content").value.trim(),
      }),
    });
    $("memory-key").value = "";
    $("memory-content").value = "";
    loadMemory();
  } catch (err) {
    toast(err.message, true);
  }
});

$("memory-search").addEventListener("submit", (e) => {
  e.preventDefault();
  loadMemory($("memory-query").value.trim());
});

/* ---------------- system ---------------- */

function card(k, v) {
  return `<div class="card"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`;
}

async function loadSystem() {
  try {
    const health = await api("/system/health");
    const metrics = await api("/system/metrics");
    $("system-cards").innerHTML =
      card("API", health.status) +
      card("Database", health.checks.database.status) +
      card("AI provider", health.checks.ai_provider.mode) +
      card("CPU", metrics.cpu_percent + "%") +
      card("RAM", metrics.ram_percent + "%") +
      card("Disk", metrics.disk_percent + "%") +
      (metrics.battery_percent != null ? card("Battery", metrics.battery_percent + "%") : "");
    $("system-alerts").innerHTML = (metrics.alerts || [])
      .map((a) => `<div class="list-item">${esc(a)}</div>`)
      .join("");
  } catch (err) {
    toast(`System: ${err.message}`, true);
  }
}
$("system-refresh").addEventListener("click", loadSystem);

/* ---------------- security ---------------- */

async function loadSecurity() {
  try {
    const status = await api("/security/status");
    const bursts = (status.failed_logins && status.failed_logins.accounts_with_bursts) || [];
    $("security-cards").innerHTML =
      card("Threat level", status.threat_level) +
      card("Risk", `${status.overall_risk_score}/100 ${status.risk_band}`) +
      card("Open events", status.open_event_count) +
      card("Active incidents", (status.active_incidents || []).length) +
      card("Login bursts (15m)", bursts.length) +
      card("Auto-containment", status.auto_containment_enabled ? "on" : "off");
  } catch (err) {
    toast(`Security: ${err.message}`, true);
  }

  try {
    const data = await api("/security/events?limit=25&status_filter=ALL");
    const box = $("event-list");
    box.innerHTML = "";
    if (!data.events.length) {
      box.innerHTML = `<div class="list-item"><span class="muted">No security events. Nothing to report.</span></div>`;
    }
    data.events.forEach((e) => {
      const row = document.createElement("div");
      row.className = "list-item";
      row.innerHTML =
        `<div class="grow">` +
        `<div class="title">${esc(e.what_happened)}</div>` +
        `<div class="sub">Why flagged: ${esc(e.why_flagged || "—")}</div>` +
        `<div class="sub">Seriousness: ${esc(e.seriousness)}</div>` +
        `<div class="sub">What ALYO did: ${esc(e.what_alyo_did)}</div>` +
        `<div class="sub">What you can do: ${esc(e.what_you_can_do)}</div>` +
        `</div><span class="pill risk-${esc(e.risk_band)}">${esc(e.status)}</span>`;
      box.appendChild(row);
    });
  } catch (err) {
    $("event-list").innerHTML = `<div class="list-item"><span class="muted">Events unavailable: ${esc(err.message)}</span></div>`;
  }

  try {
    const data = await api("/security/incidents?include_resolved=true");
    const box = $("incident-list");
    box.innerHTML = "";
    if (!data.incidents.length) {
      box.innerHTML = `<div class="list-item"><span class="muted">No incidents recorded.</span></div>`;
    }
    data.incidents.forEach((i) => {
      const row = document.createElement("div");
      row.className = "list-item";
      row.innerHTML =
        `<div class="grow"><div class="title">${esc(i.title)}</div>` +
        `<div class="sub">${esc(i.component)} · risk ${i.risk_score}/100 · updated ${esc(fmtDate(i.updated_at))}</div></div>` +
        `<span class="pill risk-${i.risk_score >= 80 ? "CRITICAL" : i.risk_score >= 60 ? "HIGH" : i.risk_score >= 30 ? "MEDIUM" : "LOW"}">${esc(i.state)}</span>`;
      box.appendChild(row);
    });
  } catch (err) {
    $("incident-list").innerHTML = `<div class="list-item"><span class="muted">Incidents: ${esc(err.message)}</span></div>`;
  }
}

/* ---------------- privacy ---------------- */

async function loadPrivacy() {
  try {
    const data = await api("/privacy/retention");
    const box = $("retention-table");
    box.innerHTML = "";
    Object.entries(data.policy).forEach(([name, p]) => {
      const row = document.createElement("div");
      row.className = "list-item";
      row.innerHTML =
        `<div class="grow"><div class="title">${esc(name.replace(/_/g, " "))}</div>` +
        `<div class="sub">${esc(p.note || "")}</div></div>` +
        `<span class="pill">${p.retention_days == null ? "kept until deleted" : p.retention_days + " days"}</span>` +
        `<span class="pill">${p.user_can_delete ? "you can delete" : "admin managed"}</span>`;
      box.appendChild(row);
    });
  } catch (err) {
    toast(`Retention: ${err.message}`, true);
  }

  try {
    const data = await api("/privacy/data");
    $("privacy-data").innerHTML = data.categories
      .map((c) =>
        `<div class="list-item"><div class="grow"><div class="title">${esc(c.category)}</div>` +
        `<div class="sub">${c.records == null ? "counted with audit entries" : c.records + " records"}` +
        `${c.retention_days != null ? " · kept " + c.retention_days + " days" : ""}</div></div></div>`)
      .join("");
    const never = document.createElement("div");
    never.className = "list-item";
    never.innerHTML = `<div class="grow"><div class="title">Never stored</div>` +
      `<div class="sub">${esc(data.never_stored.join(", "))}</div></div>`;
    $("privacy-data").appendChild(never);
  } catch (err) {
    toast(`Privacy: ${err.message}`, true);
  }

  if (me) {
    $("toggle-history").checked = !!me.user.conversation_history_enabled;
    $("toggle-memory").checked = !!me.user.long_term_memory_enabled;
  }
}

$("toggle-history").addEventListener("change", async (e) => {
  try {
    const data = await api("/privacy/conversation-history", {
      method: "PUT", body: JSON.stringify({ enabled: e.target.checked }),
    });
    toast(`Conversation history ${data.conversation_history_enabled ? "on" : "off"}.`);
  } catch (err) { toast(err.message, true); e.target.checked = !e.target.checked; }
});

$("toggle-memory").addEventListener("change", async (e) => {
  try {
    const data = await api("/privacy/long-term-memory", {
      method: "PUT", body: JSON.stringify({ enabled: e.target.checked }),
    });
    toast(`Long-term memory ${data.long_term_memory_enabled ? "on" : "off"}.`);
  } catch (err) { toast(err.message, true); e.target.checked = !e.target.checked; }
});

$("export-btn").addEventListener("click", async () => {
  try {
    const data = await api("/privacy/export");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `alyo-export-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast("Export downloaded (passwords and tokens are never included).");
  } catch (err) { toast(err.message, true); }
});

$("clear-history-btn").addEventListener("click", async () => {
  try {
    const data = await api("/privacy/conversations", { method: "DELETE" });
    toast(`${data.status}: ${data.deleted} turns removed.`);
  } catch (err) { toast(err.message, true); }
});

$("delete-account-btn").addEventListener("click", async () => {
  const password = window.prompt(
    "Type your password to permanently delete your account, conversations, " +
    "memories and tasks. Sessions will be revoked. This cannot be undone."
  );
  if (password === null) return;
  try {
    const data = await api("/privacy/account-deletion", {
      method: "POST",
      body: JSON.stringify({ password, confirm: true }),
    });
    alert(`${data.status}\n${data.note}`);
    token = null;
    localStorage.removeItem("alyo_token");
    showAuth();
  } catch (err) {
    toast(err.message, true);
  }
});

/* ---------------- boot ---------------- */

function consumeAuthRedirect() {
  // Token handed back by the OAuth callback. The fragment never reaches the
  // server, so it stays out of access logs; we clear it from the URL bar.
  const m = location.hash.match(/alyo_token=([A-Za-z0-9_-]+)/);
  if (m) {
    token = m[1];
    localStorage.setItem("alyo_token", token);
    history.replaceState(null, "", location.pathname + location.search);
  }
  const params = new URLSearchParams(location.search);
  const oauthError = params.get("oauth_error");
  if (oauthError) {
    setTimeout(() => {
      const box = $("auth-error");
      box.textContent = oauthError;
      box.classList.remove("hidden");
    }, 0);
    params.delete("oauth_error");
    const qs = params.toString();
    history.replaceState(null, "", location.pathname + (qs ? `?${qs}` : ""));
  }
}

(async function boot() {
  consumeAuthRedirect();
  if (token) await showApp();
  else showAuth();
})();
