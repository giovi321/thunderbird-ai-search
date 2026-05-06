// Background script for AI Email Search addon.
// Handles toolbar button click: open or focus the search tab.

let searchTabId = null;

messenger.browserAction.onClicked.addListener(async () => {
  if (searchTabId !== null) {
    try {
      await messenger.tabs.get(searchTabId);
      await messenger.tabs.update(searchTabId, { active: true });
      return;
    } catch (e) {
      // Tab was closed
      searchTabId = null;
    }
  }
  const tab = await messenger.tabs.create({
    url: "search/search.html",
  });
  searchTabId = tab.id;
});

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
