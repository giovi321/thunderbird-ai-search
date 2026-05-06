// Search page logic for AI Email Search addon.

let settings = null;
let pollInterval = null;

async function getSettings() {
  if (!settings) {
    const data = await messenger.storage.local.get("settings");
    settings = data.settings || {
      serverUrl: "http://localhost:8342",
      apiKey: "",
      limit: 10,
      openIn: "tab",
    };
  }
  return settings;
}

async function apiRequest(path, options = {}) {
  const s = await getSettings();
  const url = s.serverUrl.replace(/\/+$/, "") + path;
  const headers = { "Content-Type": "application/json" };
  if (s.apiKey) {
    headers["X-API-Key"] = s.apiKey;
  }
  const resp = await fetch(url, { ...options, headers: { ...headers, ...options.headers } });
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`${resp.status}: ${body}`);
  }
  return resp.json();
}

// --- Search ---

async function doSearch() {
  const query = document.getElementById("search-input").value.trim();
  if (!query) return;

  const account = document.getElementById("account-filter").value || undefined;
  const s = await getSettings();
  const statusEl = document.getElementById("status");
  const resultsEl = document.getElementById("results");

  statusEl.textContent = "Searching...";
  statusEl.className = "status searching";
  resultsEl.innerHTML = "";

  const startTime = performance.now();

  try {
    const data = await apiRequest("/search", {
      method: "POST",
      body: JSON.stringify({ query, limit: s.limit, account }),
    });

    const elapsed = ((performance.now() - startTime) / 1000).toFixed(2);
    const count = data.results.length;

    if (count === 0) {
      statusEl.textContent = `No results (${elapsed}s)`;
      statusEl.className = "status empty";
      return;
    }

    statusEl.textContent = `Found ${count} result${count !== 1 ? "s" : ""} in ${elapsed}s`;
    statusEl.className = "status success";

    for (const r of data.results) {
      resultsEl.appendChild(createResultCard(r));
    }
  } catch (e) {
    statusEl.textContent = `Error: ${e.message}`;
    statusEl.className = "status error";
  }
}

function createResultCard(result) {
  const card = document.createElement("div");
  card.className = "result-card";
  card.addEventListener("click", () => openEmail(result.message_id));

  const score = Math.round((result.score || 0) * 100);
  const date = formatDate(result.date);

  card.innerHTML = `
    <div class="result-header">
      <span class="result-subject">${escapeHtml(result.subject || "(no subject)")}</span>
      <span class="result-score">${score}%</span>
    </div>
    <div class="result-meta">
      <span class="result-from">${escapeHtml(result.from || "")}</span>
      <span class="result-date">${escapeHtml(date)}</span>
    </div>
    <div class="result-tags">
      <span class="badge badge-account">${escapeHtml(result.account || "")}</span>
      <span class="badge badge-folder">${escapeHtml(result.folder || "")}</span>
    </div>
    <div class="result-snippet">${escapeHtml(result.snippet || "")}</div>
  `;

  return card;
}

async function openEmail(messageId) {
  const s = await getSettings();
  const cleanId = messageId.replace(/^<|>$/g, "");

  try {
    await messenger.messageDisplay.open({
      headerMessageId: cleanId,
      location: s.openIn || "tab",
    });
  } catch (e) {
    showToast("Email not found in Thunderbird. It may have been deleted or not synced yet.");
  }
}

// --- Management Panel ---

async function loadAccounts() {
  try {
    const data = await apiRequest("/accounts");
    populateAccountFilter(data.accounts);
    renderAccountCards(data.accounts);
  } catch (e) {
    // Server may not be running yet
    console.warn("Could not load accounts:", e.message);
  }
}

function populateAccountFilter(accounts) {
  const select = document.getElementById("account-filter");
  // Keep the "All accounts" option, remove the rest
  while (select.options.length > 1) {
    select.remove(1);
  }
  for (const acct of accounts) {
    const opt = document.createElement("option");
    opt.value = acct.name;
    opt.textContent = acct.name;
    select.appendChild(opt);
  }
}

function renderAccountCards(accounts) {
  const container = document.getElementById("account-cards");
  container.innerHTML = "";

  for (const acct of accounts) {
    const card = document.createElement("div");
    card.className = "account-card";

    const statusClass =
      acct.status === "indexing" ? "status-indexing" :
      acct.status === "error" ? "status-error" : "status-idle";

    card.innerHTML = `
      <div class="account-card-header">
        <span class="account-name">${escapeHtml(acct.name)}</span>
        <span class="account-status ${statusClass}">${escapeHtml(acct.status)}</span>
      </div>
      <div class="account-details">
        <span>${acct.email_count} emails indexed</span>
        <span>Folders: ${escapeHtml(acct.folders.join(", "))}</span>
        ${acct.last_index_time ? `<span>Last indexed: ${escapeHtml(acct.last_index_time)}</span>` : ""}
      </div>
      <button class="btn-reindex" data-account="${escapeHtml(acct.name)}">Reindex</button>
    `;

    card.querySelector(".btn-reindex").addEventListener("click", (e) => {
      e.stopPropagation();
      reindexAccount(acct.name);
    });

    container.appendChild(card);
  }
}

async function loadVersion() {
  const el = document.getElementById("version-info");
  if (!el) return;
  const clientVersion = messenger.runtime.getManifest().version;
  el.textContent = `addon v${clientVersion}`;
  el.classList.remove("mismatch");
  el.title = `Addon v${clientVersion}`;
  try {
    const data = await apiRequest("/version");
    const serverVersion = data.version || "unknown";
    if (serverVersion === clientVersion) {
      el.textContent = `v${clientVersion}`;
      el.title = `Addon v${clientVersion} · Server v${serverVersion}`;
    } else {
      el.textContent = `addon v${clientVersion} · server v${serverVersion}`;
      el.classList.add("mismatch");
      el.title = `Version mismatch — addon v${clientVersion} vs server v${serverVersion}. Please update both to match.`;
    }
  } catch (e) {
    el.textContent = `addon v${clientVersion} · server unreachable`;
    el.title = `Could not fetch server version: ${e.message}`;
  }
}

async function loadHealth() {
  const el = document.getElementById("health-indicators");
  try {
    const data = await apiRequest("/health");
    el.innerHTML = `
      <span class="health-dot ${data.qdrant === "ok" ? "healthy" : "unhealthy"}"></span>
      <span>Qdrant</span>
      <span class="health-dot ${data.ollama === "ok" ? "healthy" : "unhealthy"}"></span>
      <span>Ollama</span>
    `;
  } catch (e) {
    el.innerHTML = `<span class="health-dot unhealthy"></span><span>Server unreachable</span>`;
  }
}

async function reindexAll() {
  try {
    await apiRequest("/reindex", { method: "POST" });
    showToast("Reindex started for all accounts");
    startPollingStatus();
  } catch (e) {
    showToast(`Error: ${e.message}`);
  }
}

async function reindexAccount(name) {
  try {
    await apiRequest(`/reindex/${encodeURIComponent(name)}`, { method: "POST" });
    showToast(`Reindex started for '${name}'`);
    startPollingStatus();
  } catch (e) {
    showToast(`Error: ${e.message}`);
  }
}

function startPollingStatus() {
  if (pollInterval) return;
  const progressEl = document.getElementById("indexer-progress");
  progressEl.classList.remove("hidden");

  pollInterval = setInterval(async () => {
    try {
      const status = await apiRequest("/indexer/status");

      if (status.running) {
        const label = document.getElementById("progress-label");
        const fill = document.getElementById("progress-fill");
        label.textContent = `Indexing ${status.current_account}... (${status.emails_processed}/${status.emails_total})`;
        const pct = status.emails_total > 0
          ? Math.round((status.emails_processed / status.emails_total) * 100)
          : 0;
        fill.style.width = pct + "%";
      } else {
        clearInterval(pollInterval);
        pollInterval = null;
        progressEl.classList.add("hidden");
        loadAccounts();
        loadHealth();
      }
    } catch (e) {
      clearInterval(pollInterval);
      pollInterval = null;
      progressEl.classList.add("hidden");
    }
  }, 2000);
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
    return d.toLocaleDateString(undefined, {
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

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  setTimeout(() => toast.classList.add("hidden"), 4000);
}

// --- Init ---

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("search-btn").addEventListener("click", doSearch);
  document.getElementById("search-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch();
  });

  document.getElementById("open-settings").addEventListener("click", () => {
    messenger.runtime.openOptionsPage();
  });

  document.getElementById("toggle-management").addEventListener("click", () => {
    const panel = document.getElementById("management-panel");
    panel.classList.toggle("hidden");
    if (!panel.classList.contains("hidden")) {
      loadHealth();
      loadAccounts();
    }
  });

  document.getElementById("reindex-all-btn").addEventListener("click", reindexAll);

  // Initial load
  loadVersion();
  loadAccounts();

  // If opened from the popup with ?q=..., pre-fill and run the search.
  const params = new URLSearchParams(window.location.search);
  const initialQuery = params.get("q");
  if (initialQuery) {
    const input = document.getElementById("search-input");
    input.value = initialQuery;
    doSearch();
  }
});
