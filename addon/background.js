// Background script for AI Email Search addon.
// Handles toolbar button click and keyboard shortcut: open or focus the search tab.

const SEARCH_URL = "search/search.html";
const SPACE_NAME = "ai_email_search";

let searchTabId = null;

// Register an entry in Thunderbird's Spaces toolbar so the search is reachable
// from every space (Mail, Calendar, Address Book, Chat, etc.) — not just Mail.
// Clicking the space icon opens or focuses the search tab automatically.
async function ensureSpaceRegistered() {
  if (!messenger.spaces || !messenger.spaces.create) return;
  try {
    await messenger.spaces.create(SPACE_NAME, SEARCH_URL, {
      title: "AI Email Search",
      defaultIcons: {
        16: "icons/search-16.svg",
        32: "icons/search-32.svg",
      },
    });
  } catch (e) {
    // Already registered from a previous load — that's fine.
  }
}

ensureSpaceRegistered();

async function openOrFocusSearchTab() {
  // First try: focus any existing search tab anywhere (including the space tab
  // that Thunderbird opens when the user clicks the Spaces toolbar icon). This
  // avoids creating a duplicate when the addon is opened multiple ways.
  const fullUrl = messenger.runtime.getURL(SEARCH_URL);
  try {
    const allTabs = await messenger.tabs.query({});
    const existing = allTabs.find(t => t.url === fullUrl);
    if (existing) {
      await messenger.tabs.update(existing.id, { active: true });
      if (existing.windowId != null && messenger.windows && messenger.windows.update) {
        try { await messenger.windows.update(existing.windowId, { focused: true }); } catch (_) {}
      }
      searchTabId = existing.id;
      return;
    }
  } catch (e) {
    // tabs.query may fail in restricted contexts — fall through to create.
  }

  const tab = await messenger.tabs.create({ url: SEARCH_URL });
  searchTabId = tab.id;
}

messenger.browserAction.onClicked.addListener(openOrFocusSearchTab);

if (messenger.commands && messenger.commands.onCommand) {
  messenger.commands.onCommand.addListener((command) => {
    if (command === "open-search") {
      openOrFocusSearchTab();
    }
  });
}

messenger.tabs.onRemoved.addListener((tabId) => {
  if (tabId === searchTabId) {
    searchTabId = null;
  }
});

// Set default settings on install
messenger.runtime.onInstalled.addListener(() => {
  messenger.storage.local.get("settings").then((data) => {
    if (!data.settings) {
      messenger.storage.local.set({
        settings: {
          serverUrl: "http://localhost:8342",
          apiKey: "",
          limit: 10,
          openIn: "tab",
        },
      });
    }
  });
});
