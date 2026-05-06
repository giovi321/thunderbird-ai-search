// Background script for AI Email Search addon.
//
// The toolbar button now opens a search-bar popup (popup/popup.html) instead of
// navigating to a tab; the popup itself opens results in a new tab on submit.
// The Spaces toolbar entry still opens the full search page so the addon is
// reachable from every Thunderbird space.

const SEARCH_URL = "search/search.html";
const SPACE_NAME = "ai_email_search";

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
    // Already registered from a previous load — ignore.
  }
}

ensureSpaceRegistered();

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
