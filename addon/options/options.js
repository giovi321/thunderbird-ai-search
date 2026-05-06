// Options page logic for AI Email Search addon.

let pollInterval = null;

const SHORTCUT_COMMAND_NAME = "_execute_browser_action";
const SHORTCUT_DEFAULT = "Ctrl+Alt+S";

async function loadSettings() {
  const data = await messenger.storage.local.get("settings");
  const s = data.settings || {};

  document.getElementById("server-url").value = s.serverUrl || "http://localhost:8342";
  document.getElementById("api-key").value = s.apiKey || "";
  document.getElementById("limit").value = s.limit || 10;

  const openIn = s.openIn || "tab";
  document.querySelector(`input[name="open-in"][value="${openIn}"]`).checked = true;
}

async function saveSettings() {
  const settings = {
    serverUrl: document.getElementById("server-url").value.trim(),
    apiKey: document.getElementById("api-key").value,
    limit: parseInt(document.getElementById("limit").value, 10) || 10,
    openIn: document.querySelector('input[name="open-in"]:checked').value,
  };

  await messenger.storage.local.set({ settings });

  const btn = document.getElementById("save-btn");
  const msg = document.createElement("span");
  msg.className = "saved-msg";
  msg.textContent = "Saved";
  btn.parentNode.appendChild(msg);
  setTimeout(() => msg.remove(), 2000);
}

function getConnectionInfo() {
  const serverUrl = document.getElementById("server-url").value.trim().replace(/\/+$/, "");
  const apiKey = document.getElementById("api-key").value;
  const headers = {};
  if (apiKey) headers["X-API-Key"] = apiKey;
  return { serverUrl, headers };
}

async function apiGet(path) {
  const { serverUrl, headers } = getConnectionInfo();
  const resp = await fetch(serverUrl + path, { headers });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function apiPost(path) {
  const { serverUrl, headers } = getConnectionInfo();
  headers["Content-Type"] = "application/json";
  const resp = await fetch(serverUrl + path, { method: "POST", headers });
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (body && body.detail) detail = body.detail;
    } catch (_) { /* response was not JSON */ }
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  return resp.json();
}

// Persist expand/collapse state across the auto-refresh's innerHTML rebuilds.
// Keys are "<account>:<section>" (e.g. "write@gmail.com:folders").
const expandedSections = new Set();

function showReindexFeedback(message, kind) {
  const el = document.getElementById("reindex-feedback");
  if (!el) return;
  if (!message) {
    el.classList.add("hidden");
    el.textContent = "";
    return;
  }
  el.classList.remove("hidden");
  el.className = `reindex-feedback ${kind || ""}`;
  el.textContent = message;
  clearTimeout(showReindexFeedback._timer);
  showReindexFeedback._timer = setTimeout(() => {
    el.classList.add("hidden");
  }, 6000);
}

// --- Health ---

async function loadHealth() {
  const el = document.getElementById("health-section");
  try {
    const data = await apiGet("/health");
    el.innerHTML = `
      <div class="health-row">
        <span class="health-dot ${data.qdrant === "ok" ? "ok" : "err"}"></span>
        <span class="health-label">Qdrant</span>
        <span class="health-status">${data.qdrant}</span>
      </div>
      <div class="health-row">
        <span class="health-dot ${data.ollama === "ok" ? "ok" : "err"}"></span>
        <span class="health-label">Ollama</span>
        <span class="health-status">${data.ollama}</span>
      </div>
    `;
  } catch (e) {
    el.innerHTML = `
      <div class="health-row">
        <span class="health-dot err"></span>
        <span class="health-label">Server unreachable</span>
        <span class="health-status">${escapeHtml(e.message)}</span>
      </div>
    `;
  }
}

// --- Stats ---

async function loadStats() {
  try {
    const stats = await apiGet("/stats");
    document.getElementById("total-emails").textContent = stats.total_emails.toLocaleString();
    document.getElementById("total-accounts").textContent = stats.accounts.length;
  } catch (e) {
    document.getElementById("total-emails").textContent = "...";
    document.getElementById("total-accounts").textContent = "...";
  }
}

// --- Indexer Status ---
//
// The indexer is a 2-step process that matches the actual server architecture:
//
//   Step 1: Indexing: per-folder loop (fetch → filter → embed → upsert → checkpoint).
//            Server's `phase` flips rapidly between "fetching" / "embedding" within
//            this step as it walks the folder list. We collapse them into one card
//            and surface the current sub-activity as a sub-status line.
//
//   Step 2: Cleanup: optional pass that scans IMAP MESSAGE-IDs to remove Qdrant
//            entries for deleted emails. Throttled to once per N hours.
//
// The earlier 4-step UI ("fetch / filter / embed / cleanup" as separate sequential
// phases) didn't match reality and made the rate-limit-during-indexing confusing.

const SUBPHASE_LABEL = {
  fetching: "Fetching emails from IMAP",
  filtering: "Comparing against already-indexed",
  embedding: "Generating embeddings + storing in Qdrant",
};

async function loadIndexerStatus() {
  const container = document.getElementById("indexer-section");

  try {
    const status = await apiGet("/indexer/status");

    if (!status.running) {
      const sched = status.scheduler || {};
      let html = `<div class="indexer-idle">`;
      html += `<div class="indexer-idle-label">`;
      if (status.last_run) {
        html += `Idle. Last cycle ended ${formatDate(status.last_run)} (${formatRelative(status.last_run)})`;
      } else {
        html += `Idle. No indexing runs yet`;
      }
      html += `</div>`;
      const schedRows = [];
      if (sched.interval_minutes > 0) {
        schedRows.push(`Schedule: every ${sched.interval_minutes} min`);
      } else if (sched.interval_minutes === 0) {
        schedRows.push(`Schedule: one-shot (no auto re-runs)`);
      }
      if (sched.next_run_at) {
        schedRows.push(`Next run: ${formatDate(sched.next_run_at)} (${formatRelative(sched.next_run_at)})`);
      }
      if (sched.cleanup_interval_hours) {
        schedRows.push(`Cleanup interval: ${sched.cleanup_interval_hours}h`);
      }
      if (schedRows.length) {
        html += `<div class="indexer-meta">${schedRows.map(escapeHtml).join(" · ")}</div>`;
      }
      if (status.last_error) {
        html += `<div class="indexer-error">Last error: ${escapeHtml(status.last_error)}</div>`;
      }
      html += `</div>`;
      container.innerHTML = html;
      stopAutoRefresh();
      return;
    }

    startAutoRefresh();

    const indexingPhases = ["fetching", "filtering", "embedding"];
    const indexingActive = indexingPhases.includes(status.phase);
    const cleanupActive = status.phase === "cleanup";

    let html = `<div class="auto-refresh" id="auto-refresh-label">auto-refreshing every 3s. Account: ${escapeHtml(status.current_account)}</div>`;
    html += `<div class="pipeline">`;
    html += renderIndexingStep(status, indexingActive, cleanupActive);
    html += renderCleanupStep(status, cleanupActive, indexingActive);
    html += `</div>`;

    if (status.last_error) {
      html += `<div class="indexer-error">Error: ${escapeHtml(status.last_error)}</div>`;
    }

    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = `<div class="indexer-idle"><div class="indexer-idle-label">Could not reach server</div></div>`;
  }
}

function renderIndexingStep(status, active, doneOrNext) {
  // doneOrNext = true when cleanup is the current phase (so indexing has finished this cycle)
  const cls = active ? "active" : doneOrNext ? "done" : "";
  const iconCls = active ? "active" : doneOrNext ? "done" : "pending";
  const icon = doneOrNext ? "✓" : "1";

  // Overall progress: emails_processed / cycle_total_estimate (both in unique-emails units).
  // Fallback to fetched/estimate if processed is 0, then folder count if no estimate.
  let progressPct = 0;
  if (status.cycle_total_estimate > 0) {
    progressPct = Math.min(100, Math.round((status.emails_processed / status.cycle_total_estimate) * 100));
  } else if (active && status.folders_total > 0) {
    progressPct = Math.round((status.folders_done / status.folders_total) * 100);
  } else if (doneOrNext) {
    progressPct = 100;
  }
  const pct = active || doneOrNext ? `${progressPct}%` : "";

  let html = `<div class="pipeline-step ${cls}">`;
  html += `<div class="pipeline-step-header">`;
  html += `<span class="step-icon ${iconCls}">${icon}</span>`;
  html += `<span class="step-name">Indexing (per-folder loop)</span>`;
  if (pct) html += `<span class="step-pct">${pct}</span>`;
  html += `</div>`;

  if (active || doneOrNext) {
    // "Currently" line (active sub-phase + which folder)
    if (active) {
      const subPhase = SUBPHASE_LABEL[status.phase] || "Indexing";
      let cur = `Currently: ${subPhase}`;
      if (status.folders_total > 0) {
        cur += ` · Folder ${status.folders_done + 1}/${status.folders_total}`;
      }
      if (status.current_folder) {
        cur += `: ${status.current_folder}`;
      }
      if (status.folder_emails_total > 0) {
        const fPct = Math.round((status.folder_emails_done / status.folder_emails_total) * 100);
        cur += `\n  ↳ ${status.folder_emails_done.toLocaleString()}/${status.folder_emails_total.toLocaleString()} in this folder (${fPct}%)`;
      }
      html += `<div class="step-detail">${escapeHtml(cur)}</div>`;
    }

    // Three sub-step rows that update in parallel as the per-folder loop runs.
    // Each row's "active" highlight reflects what server.phase is currently set to.
    const fetched = status.emails_fetched || 0;
    const filtered = status.emails_filtered_dup || 0;
    const indexed = status.emails_processed || 0;
    const skippedOllama = status.emails_skipped_ollama || 0;

    const fetchActive = status.phase === "fetching";
    const filterActive = status.phase === "filtering";
    const embedActive = status.phase === "embedding";

    let fetchedDetail;
    if (status.cycle_total_estimate > 0) {
      fetchedDetail = `${fetched.toLocaleString()} fetched / ~${status.cycle_total_estimate.toLocaleString()} unique remaining`;
    } else {
      fetchedDetail = `${fetched.toLocaleString()} fetched`;
    }

    html += `<div class="substeps">`;
    html += renderSubstep("a", "Fetch from IMAP", fetchedDetail, fetchActive);
    html += renderSubstep("b", "Filter already-indexed", `${filtered.toLocaleString()} skipped (already in Qdrant)`, filterActive);
    let embedDetail = `${indexed.toLocaleString()} embedded + stored in Qdrant`;
    if (skippedOllama > 0) {
      embedDetail += ` · ${skippedOllama.toLocaleString()} Ollama errors`;
    }
    html += renderSubstep("c", "Embed + store", embedDetail, embedActive);
    html += `</div>`;

    html += `<div class="step-progress"><div class="step-progress-fill" style="width:${progressPct}%"></div></div>`;
  }

  html += `</div>`;
  return html;
}

function renderSubstep(letter, label, detail, isActive) {
  const cls = isActive ? "substep active" : "substep";
  return `
    <div class="${cls}">
      <span class="substep-marker">${letter}.</span>
      <span class="substep-label">${escapeHtml(label)}</span>
      <span class="substep-value">${escapeHtml(detail)}</span>
    </div>
  `;
}

function renderCleanupStep(status, active, indexingActive) {
  const cls = active ? "active" : "";
  const iconCls = active ? "active" : "pending";
  const icon = "2";

  let pct = "";
  let detail = "";
  let progressHtml = "";

  if (active) {
    const cPct = status.cleanup_folders_total > 0
      ? Math.round((status.cleanup_folders_done / status.cleanup_folders_total) * 100) : 0;
    pct = `${cPct}%`;
    detail = `Folder ${status.cleanup_folders_done + 1}/${status.cleanup_folders_total}`;
    if (status.current_folder) {
      detail += `: ${status.current_folder}`;
    }
    progressHtml = `<div class="step-progress"><div class="step-progress-fill" style="width:${cPct}%"></div></div>`;
  } else if (indexingActive) {
    const interval = (status.scheduler && status.scheduler.cleanup_interval_hours) || 24;
    detail = `Pending. Runs after indexing if eligible (throttle: ${interval}h)`;
  }

  let html = `<div class="pipeline-step ${cls}">`;
  html += `<div class="pipeline-step-header">`;
  html += `<span class="step-icon ${iconCls}">${icon}</span>`;
  html += `<span class="step-name">Cleanup deleted emails</span>`;
  if (pct) html += `<span class="step-pct">${pct}</span>`;
  html += `</div>`;
  if (detail) html += `<div class="step-detail">${escapeHtml(detail)}</div>`;
  html += progressHtml;
  html += `</div>`;
  return html;
}

function startAutoRefresh() {
  if (pollInterval) return;
  const el = document.getElementById("auto-refresh-label");
  if (el) el.textContent = "auto-refreshing every 3s";
  pollInterval = setInterval(() => {
    loadIndexerStatus();
    loadStats();
    loadAccounts();
  }, 3000);
}

function stopAutoRefresh() {
  if (pollInterval) {
    clearInterval(pollInterval);
    pollInterval = null;
  }
  const el = document.getElementById("auto-refresh-label");
  if (el) el.textContent = "";
}

// --- Accounts ---

async function loadAccounts() {
  const container = document.getElementById("accounts-section");
  try {
    const data = await apiGet("/accounts");
    const accounts = data.accounts || [];

    if (accounts.length === 0) {
      container.innerHTML = '<div style="font-size:13px;color:var(--text-muted);">No accounts configured</div>';
      return;
    }

    container.innerHTML = accounts.map(acct => renderAccountCard(acct)).join("");

    container.querySelectorAll(".btn-reindex").forEach(btn => {
      btn.addEventListener("click", () => reindexAccount(btn.dataset.account));
    });
    container.querySelectorAll(".btn-pause").forEach(btn => {
      btn.addEventListener("click", () => pauseAccount(btn.dataset.account));
    });
    container.querySelectorAll(".btn-resume").forEach(btn => {
      btn.addEventListener("click", () => resumeAccount(btn.dataset.account));
    });
    container.querySelectorAll(".btn-info[data-popover-target]").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const target = document.getElementById(btn.dataset.popoverTarget);
        if (!target) return;
        const wasOpen = target.classList.contains("open");
        document.querySelectorAll(".info-popover.open").forEach(p => p.classList.remove("open"));
        if (!wasOpen) target.classList.add("open");
      });
    });
    container.querySelectorAll(".details-toggle").forEach(btn => {
      btn.addEventListener("click", () => {
        const key = btn.dataset.key;
        const target = btn.parentElement.querySelector(btn.dataset.target);
        if (!target || !key) return;
        if (expandedSections.has(key)) {
          expandedSections.delete(key);
          target.classList.remove("open");
          btn.textContent = btn.textContent.replace(/^▾/, "▸");
        } else {
          expandedSections.add(key);
          target.classList.add("open");
          btn.textContent = btn.textContent.replace(/^▸/, "▾");
        }
      });
    });
  } catch (e) {
    container.innerHTML = '<div style="font-size:13px;color:var(--text-muted);">Could not load accounts</div>';
  }
}

const OUTCOME_LABELS = {
  completed: { label: "completed", cls: "ok" },
  aborted_rate_limit: { label: "rate limited", cls: "warn" },
  aborted_embed: { label: "embed failed", cls: "err" },
  connection_failed: { label: "connect failed", cls: "err" },
  cap_reached: { label: "paused (cap)", cls: "info" },
};

function renderAccountCard(acct) {
  const badgeClass = acct.status === "indexing" ? "indexing" :
                     acct.status === "error" ? "error" : "idle";
  const folders = Array.isArray(acct.folders) ? acct.folders.join(", ") : "(all)";
  const lastRun = acct.last_run || {};
  const cleanup = acct.cleanup || {};
  const foldersDetail = acct.folders_detail || [];
  const history = acct.history || [];

  // Last cycle summary line
  let lastCycleLine = "";
  if (lastRun.ended_at) {
    const out = OUTCOME_LABELS[lastRun.outcome] || { label: lastRun.outcome || "unknown", cls: "" };
    const dur = lastRun.duration_seconds ? formatDuration(lastRun.duration_seconds) : "";
    lastCycleLine = `Last cycle: <span class="outcome ${out.cls}">${escapeHtml(out.label)}</span>`;
    lastCycleLine += ` · ${lastRun.indexed.toLocaleString()} new`;
    if (dur) lastCycleLine += ` · ${escapeHtml(dur)}`;
    lastCycleLine += ` · ${formatRelative(lastRun.ended_at)}`;
  } else if (acct.last_index_time) {
    lastCycleLine = `Last indexed: ${escapeHtml(acct.last_index_time)}`;
  } else {
    lastCycleLine = "Never indexed";
  }

  let nextRunLine = "";
  if (acct.next_run_at) {
    nextRunLine = `Next run: ${formatDate(acct.next_run_at)} (${formatRelative(acct.next_run_at)})`;
  }

  // Lifetime totals: survive history-window pruning so the user always sees
  // the cumulative big picture even when individual cycles roll off.
  let lifetimeLine = "";
  const lt = acct.lifetime || {};
  if (lt.cycles) {
    const parts = [`${lt.cycles.toLocaleString()} cycles`];
    if (lt.indexed) parts.push(`${lt.indexed.toLocaleString()} new emails indexed`);
    if (lt.bytes) parts.push(`${formatBytes(lt.bytes)} downloaded`);
    let suffix = "";
    if (lt.since) suffix = ` since ${formatRelative(lt.since)}`;
    lifetimeLine = `Lifetime: ${parts.join(" · ")}${suffix}`;
  }

  // Manual pause state: when set, the scheduler skips this account and
  // the dashboard shows a banner + "Resume" button. Manual reindex still
  // bypasses the pause if the user really wants to force a cycle.
  const paused = acct.paused || {};
  let pausedHtml = "";
  let pauseButtonHtml = "";
  if (paused.until) {
    pausedHtml = `<div class="account-paused">Paused until ${escapeHtml(formatDate(paused.until))} (${escapeHtml(formatRelative(paused.until))}). Scheduled cycles skipped, but manual reindex still works.</div>`;
    pauseButtonHtml = `<button class="btn-resume" data-account="${escapeHtml(acct.name)}">Resume</button>`;
  } else {
    pauseButtonHtml = `<button class="btn-pause" data-account="${escapeHtml(acct.name)}" title="Pause scheduled cycles for 6 hours">Pause 6h</button>`;
  }

  let cleanupLine = "";
  if (cleanup.last_at) {
    cleanupLine = `Cleanup: ${formatRelative(cleanup.last_at)} · removed ${(cleanup.last_removed || 0).toLocaleString()}`;
    if (cleanup.next_eligible_at) {
      cleanupLine += ` · next eligible ${formatRelative(cleanup.next_eligible_at)}`;
    }
  } else if (cleanup.interval_hours) {
    cleanupLine = `Cleanup: never (interval ${cleanup.interval_hours}h)`;
  }

  let lastErrorLine = "";
  if (lastRun.error) {
    lastErrorLine = `<div class="account-error">${escapeHtml(lastRun.error)}</div>`;
  }

  // Simple 24h activity counters: just numbers, no comparisons against
  // hypothetical Gmail limits, no caveat text. Tracks what we sent, not what
  // Gmail allows.
  let activityLine = "";
  const usage = acct.usage_24h || {};
  if (usage.bytes != null || usage.commands != null || usage.fetched_emails != null) {
    const parts = [];
    parts.push(`${(usage.fetched_emails || 0).toLocaleString()} emails fetched`);
    parts.push(`${formatBytes(usage.bytes || 0)} downloaded`);
    parts.push(`${(usage.commands || 0).toLocaleString()} IMAP commands`);
    activityLine = `Activity (last 24h): ${parts.join(" · ")}`;
  }

  // Per-folder breakdown: preserve open/closed state across auto-refreshes
  let folderListHtml = "";
  if (foldersDetail.length > 0) {
    const rows = foldersDetail.slice(0, 50).map(f => {
      const pct = f.indexed_pct != null ? `${f.indexed_pct}%` : "...";
      const total = f.approx_total != null ? f.approx_total.toLocaleString() : "?";
      const indexed = f.last_uid.toLocaleString();
      const remaining = f.remaining != null ? f.remaining.toLocaleString() : "?";
      const fillWidth = f.indexed_pct != null ? f.indexed_pct : 0;
      return `
        <div class="folder-row">
          <div class="folder-row-top">
            <span class="folder-name">${escapeHtml(f.name)}</span>
            <span class="folder-pct">${escapeHtml(pct)}</span>
          </div>
          <div class="folder-bar"><div class="folder-bar-fill" style="width:${fillWidth}%"></div></div>
          <div class="folder-row-detail">UID ${escapeHtml(indexed)} / ${escapeHtml(total)} · ${escapeHtml(remaining)} remaining</div>
        </div>
      `;
    }).join("");
    const extra = foldersDetail.length > 50 ? `<div class="folder-row-detail">… and ${foldersDetail.length - 50} more</div>` : "";
    const folderKey = `${acct.name}:folders`;
    const foldersOpen = expandedSections.has(folderKey);
    folderListHtml = `
      <button class="details-toggle" data-key="${escapeHtml(folderKey)}" data-target=".folder-list">${foldersOpen ? "▾" : "▸"} Folders (${foldersDetail.length})</button>
      <div class="folder-list${foldersOpen ? " open" : ""}">${rows}${extra}</div>
    `;
  }

  // Recent cycle history: also state-preserving
  let historyHtml = "";
  if (history.length > 0) {
    const rows = history.slice().reverse().map(h => {
      const out = OUTCOME_LABELS[h.outcome] || { label: h.outcome || "unknown", cls: "" };
      const dur = h.duration_seconds ? formatDuration(h.duration_seconds) : "";
      return `
        <div class="history-row">
          <span class="outcome ${out.cls}">${escapeHtml(out.label)}</span>
          <span>${(h.indexed || 0).toLocaleString()} new</span>
          <span>${escapeHtml(dur)}</span>
          <span class="history-when">${escapeHtml(formatDate(h.ended_at))}</span>
        </div>
      `;
    }).join("");
    const historyKey = `${acct.name}:history`;
    const historyOpen = expandedSections.has(historyKey);
    historyHtml = `
      <button class="details-toggle" data-key="${escapeHtml(historyKey)}" data-target=".history-list">${historyOpen ? "▾" : "▸"} Recent cycles (${history.length})</button>
      <div class="history-list${historyOpen ? " open" : ""}">${rows}</div>
    `;
  }

  return `
    <div class="account-card">
      <div class="account-header">
        <span class="account-name">${escapeHtml(acct.name)}</span>
        <span class="account-badge ${badgeClass}">${escapeHtml(acct.status)}</span>
      </div>
      <div class="account-details">
        <span>${acct.email_count.toLocaleString()} emails indexed${acct.folders_total ? ` across ${acct.folders_total} folders` : ""}</span>
        <span>Configured folders: ${escapeHtml(folders)}</span>
        <span>${lastCycleLine}</span>
        ${nextRunLine ? `<span>${escapeHtml(nextRunLine)}</span>` : ""}
        ${cleanupLine ? `<span>${escapeHtml(cleanupLine)}</span>` : ""}
        ${activityLine ? `<span>${escapeHtml(activityLine)}</span>` : ""}
        ${lifetimeLine ? `<span class="account-lifetime">${escapeHtml(lifetimeLine)}</span>` : ""}
      </div>
      ${pausedHtml}
      ${lastErrorLine}
      ${folderListHtml}
      ${historyHtml}
      <div class="account-actions">
        <button class="btn-reindex" data-account="${escapeHtml(acct.name)}">Reindex</button>
        <span class="info-anchor">
          <button type="button" class="btn-info" data-popover-target="info-${escapeHtml(acct.name)}" aria-label="What does Reindex do?" title="What does Reindex do?">i</button>
          <div class="info-popover" id="info-${escapeHtml(acct.name)}" role="dialog">
            <h4>What does Reindex do?</h4>
            <p>Triggers an immediate indexing cycle for <strong>${escapeHtml(acct.name)}</strong> outside the normal schedule. It does <strong>not</strong> wipe and rebuild the index from scratch.</p>
            <ul>
              <li>Fetches new emails since the last cycle and adds them to the index.</li>
              <li>Runs the cleanup pass that removes stale entries for emails no longer on the server.</li>
              <li>Counts against this account's IMAP rate limits. Large mailboxes can be throttled and produce a partial cycle.</li>
              <li>Bypasses any active pause on this account.</li>
              <li>Cannot start while another cycle is in progress (the server returns 409).</li>
            </ul>
          </div>
        </span>
        ${pauseButtonHtml}
      </div>
    </div>
  `;
}

function formatBytes(n) {
  if (n == null || isNaN(n)) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

// --- Actions ---

// Render the same explanation used in the info popover, so the user always
// sees the consequences before they confirm. Returned as an HTML string.
function reindexExplanationHtml(scope) {
  const target = scope === "all"
    ? "all configured accounts"
    : `<strong>${escapeHtml(scope)}</strong>`;
  return `
    <p>Trigger an immediate indexing cycle for ${target} outside the normal schedule? This does <strong>not</strong> wipe and rebuild the index. It adds new emails and removes stale entries.</p>
    <ul>
      <li>Counts against IMAP rate limits. Large mailboxes (Gmail especially) may be throttled and produce a partial cycle.</li>
      <li>Bypasses any active pause.</li>
      <li>Cannot start while another cycle is in progress.</li>
    </ul>
  `;
}

function showConfirm({ title, html, confirmText = "Confirm" }) {
  return new Promise(resolve => {
    const dlg = document.getElementById("confirm-dialog");
    document.getElementById("confirm-title").textContent = title;
    document.getElementById("confirm-message").innerHTML = html;
    const okBtn = document.getElementById("confirm-ok");
    const cancelBtn = document.getElementById("confirm-cancel");
    okBtn.textContent = confirmText;

    const cleanup = () => {
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      dlg.removeEventListener("close", onClose);
    };
    const onOk = () => { cleanup(); dlg.close("ok"); resolve(true); };
    const onCancel = () => { cleanup(); dlg.close("cancel"); resolve(false); };
    const onClose = () => { cleanup(); resolve(dlg.returnValue === "ok"); };

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    dlg.addEventListener("close", onClose);
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");
  });
}

async function reindexAll() {
  const ok = await showConfirm({
    title: "Reindex all accounts?",
    html: reindexExplanationHtml("all"),
    confirmText: "Reindex all",
  });
  if (!ok) return;
  try {
    await apiPost("/reindex");
    showReindexFeedback("Reindex started for all accounts", "ok");
    startAutoRefresh();
    loadIndexerStatus();
  } catch (e) {
    if (e.status === 409) {
      showReindexFeedback("Indexer is already running. Let the current cycle finish first", "warn");
    } else {
      showReindexFeedback(`Could not start reindex: ${e.message}`, "err");
    }
  }
}

async function reindexAccount(name) {
  const ok = await showConfirm({
    title: `Reindex ${name}?`,
    html: reindexExplanationHtml(name),
    confirmText: "Reindex",
  });
  if (!ok) return;
  try {
    await apiPost(`/reindex/${encodeURIComponent(name)}`);
    showReindexFeedback(`Reindex started for ${name}`, "ok");
    startAutoRefresh();
    loadIndexerStatus();
  } catch (e) {
    if (e.status === 409) {
      showReindexFeedback("Indexer is already running. Let the current cycle finish first", "warn");
    } else {
      showReindexFeedback(`Could not start reindex for ${name}: ${e.message}`, "err");
    }
  }
}

async function pauseAccount(name) {
  try {
    const result = await apiPost(`/accounts/${encodeURIComponent(name)}/pause?hours=6`);
    showReindexFeedback(`Paused ${name} for 6 hours (until ${formatDate(result.until)})`, "ok");
    loadAccounts();
  } catch (e) {
    showReindexFeedback(`Could not pause ${name}: ${e.message}`, "err");
  }
}

async function resumeAccount(name) {
  try {
    await apiPost(`/accounts/${encodeURIComponent(name)}/resume`);
    showReindexFeedback(`Resumed ${name}. Next scheduled cycle will run normally`, "ok");
    loadAccounts();
  } catch (e) {
    showReindexFeedback(`Could not resume ${name}: ${e.message}`, "err");
  }
}

async function testConnection() {
  await loadVersionBanner();
  await loadHealth();
  await loadStats();
  await loadIndexerStatus();
  await loadAccounts();
}

function refreshAll() {
  loadVersionBanner();
  loadHealth();
  loadStats();
  loadIndexerStatus();
  loadAccounts();
}

// --- Utilities ---

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function formatDate(isoDate) {
  if (!isoDate) return "";
  try {
    const d = new Date(isoDate);
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return isoDate;
  }
}

function formatRelative(isoDate) {
  if (!isoDate) return "";
  try {
    const target = new Date(isoDate).getTime();
    const now = Date.now();
    const diffSec = Math.round((target - now) / 1000);
    const abs = Math.abs(diffSec);
    const past = diffSec < 0;
    let unit, value;
    if (abs < 60) { unit = "sec"; value = abs; }
    else if (abs < 3600) { unit = "min"; value = Math.round(abs / 60); }
    else if (abs < 86400) { unit = "h"; value = Math.round(abs / 3600); }
    else { unit = "d"; value = Math.round(abs / 86400); }
    if (past) return value === 0 ? "just now" : `${value}${unit} ago`;
    return value === 0 ? "now" : `in ${value}${unit}`;
  } catch {
    return "";
  }
}

function formatDuration(seconds) {
  if (!seconds && seconds !== 0) return "";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const remM = m % 60;
  return remM ? `${h}h ${remM}m` : `${h}h`;
}

// --- Version banner ---

async function loadVersionBanner() {
  const el = document.getElementById("version-banner");
  if (!el) return;
  const clientVersion = messenger.runtime.getManifest().version;
  el.className = "version-banner";
  el.textContent = `Addon v${clientVersion} — checking server…`;
  try {
    const data = await apiGet("/version");
    const serverVersion = data.version || "unknown";
    if (serverVersion === clientVersion) {
      el.textContent = `Addon v${clientVersion} · Server v${serverVersion}`;
    } else {
      el.classList.add("mismatch");
      el.textContent = `Version mismatch: addon v${clientVersion} vs server v${serverVersion}. Please update both to the same version.`;
    }
  } catch (e) {
    el.classList.add("unreachable");
    el.textContent = `Addon v${clientVersion} · server unreachable (${e.message})`;
  }
}

// --- Keyboard shortcut ---
//
// Thunderbird's MV2 commands API lets us define a default shortcut in
// manifest.json and update it at runtime via messenger.commands.update().
// Empty shortcut string disables the keybinding entirely.

function formatShortcutFromEvent(e) {
  const parts = [];
  // Order matters for the WebExtensions commands API — Ctrl/MacCtrl, Alt, Shift, then key.
  if (e.ctrlKey) parts.push("Ctrl");
  if (e.altKey) parts.push("Alt");
  if (e.shiftKey) parts.push("Shift");
  if (e.metaKey) parts.push("Command");

  let key = e.key;
  // Normalize special keys to the API's expected names
  const SPECIAL = {
    " ": "Space",
    "ArrowUp": "Up",
    "ArrowDown": "Down",
    "ArrowLeft": "Left",
    "ArrowRight": "Right",
    "Escape": "Escape",
    "Tab": "Tab",
    "Insert": "Insert",
    "Delete": "Delete",
    "Home": "Home",
    "End": "End",
    "PageUp": "PageUp",
    "PageDown": "PageDown",
    "Enter": "Enter",
    "Backspace": "Backspace",
    "Comma": "Comma",
    "Period": "Period",
  };
  if (SPECIAL[key]) {
    key = SPECIAL[key];
  } else if (/^F([1-9]|1[0-2])$/.test(key)) {
    // Function keys are already in the right form (F1..F12)
  } else if (key.length === 1) {
    key = key.toUpperCase();
  } else {
    return null; // unsupported key (Shift, Ctrl alone, etc.)
  }

  // Reject pure modifiers
  if (["Control", "Shift", "Alt", "Meta"].includes(key)) return null;
  // Must include at least one modifier (besides Shift+letter alone, which the API rejects)
  const hasNonShiftModifier = e.ctrlKey || e.altKey || e.metaKey;
  const isFunctionKey = /^F([1-9]|1[0-2])$/.test(key);
  if (!hasNonShiftModifier && !isFunctionKey) return null;

  parts.push(key);
  return parts.join("+");
}

function setShortcutFeedback(message, kind) {
  const el = document.getElementById("shortcut-feedback");
  if (!el) return;
  el.className = `shortcut-feedback ${kind || ""}`;
  el.textContent = message || "";
}

async function loadCurrentShortcut() {
  const input = document.getElementById("shortcut-input");
  if (!input || !messenger.commands || !messenger.commands.getAll) return;
  try {
    const cmds = await messenger.commands.getAll();
    const cmd = cmds.find(c => c.name === SHORTCUT_COMMAND_NAME);
    input.value = (cmd && cmd.shortcut) ? cmd.shortcut : "";
  } catch (e) {
    setShortcutFeedback(`Could not read current shortcut: ${e.message}`, "err");
  }
}

async function updateShortcut(shortcut) {
  if (!messenger.commands || !messenger.commands.update) {
    setShortcutFeedback("This Thunderbird version does not support updating shortcuts at runtime.", "err");
    return;
  }
  try {
    await messenger.commands.update({
      name: SHORTCUT_COMMAND_NAME,
      shortcut: shortcut, // empty string clears it
    });
    document.getElementById("shortcut-input").value = shortcut;
    setShortcutFeedback(
      shortcut ? `Shortcut set to ${shortcut}` : "Shortcut cleared",
      "ok"
    );
  } catch (e) {
    setShortcutFeedback(`Could not set shortcut: ${e.message}`, "err");
  }
}

function initShortcutUI() {
  const input = document.getElementById("shortcut-input");
  const resetBtn = document.getElementById("shortcut-reset");
  const clearBtn = document.getElementById("shortcut-clear");
  if (!input) return;

  input.addEventListener("focus", () => {
    setShortcutFeedback("Press the desired key combination…");
  });

  input.addEventListener("keydown", (e) => {
    e.preventDefault();
    e.stopPropagation();
    // Ignore presses of pure modifier keys — wait for a real key.
    if (["Control", "Shift", "Alt", "Meta"].includes(e.key)) return;
    const formatted = formatShortcutFromEvent(e);
    if (!formatted) {
      setShortcutFeedback("Invalid combination — must include Ctrl, Alt, or Cmd plus a key (or be an F-key).", "err");
      return;
    }
    updateShortcut(formatted);
  });

  resetBtn.addEventListener("click", () => updateShortcut(SHORTCUT_DEFAULT));
  clearBtn.addEventListener("click", () => updateShortcut(""));
}

// --- Init ---

document.addEventListener("DOMContentLoaded", () => {
  loadSettings();
  loadCurrentShortcut();
  initShortcutUI();
  document.getElementById("save-btn").addEventListener("click", saveSettings);
  document.getElementById("test-btn").addEventListener("click", testConnection);
  document.getElementById("reindex-all-btn").addEventListener("click", reindexAll);
  document.getElementById("refresh-status").addEventListener("click", refreshAll);

  const reindexInfoBtn = document.getElementById("reindex-info-btn");
  const reindexInfoPopover = document.getElementById("reindex-info-popover");
  if (reindexInfoBtn && reindexInfoPopover) {
    reindexInfoBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const wasOpen = reindexInfoPopover.classList.contains("open");
      document.querySelectorAll(".info-popover.open").forEach(p => p.classList.remove("open"));
      if (!wasOpen) reindexInfoPopover.classList.add("open");
    });
  }

  // Click outside or press Escape closes any open popover
  document.addEventListener("click", (e) => {
    if (e.target.closest(".info-anchor")) return;
    document.querySelectorAll(".info-popover.open").forEach(p => p.classList.remove("open"));
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      document.querySelectorAll(".info-popover.open").forEach(p => p.classList.remove("open"));
    }
  });

  // Auto-load server status on page open
  refreshAll();
});
