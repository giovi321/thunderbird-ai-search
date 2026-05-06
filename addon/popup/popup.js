// Popup search bar. Submits open the results in a new tab and close the popup.

async function openSearch(query) {
  // Resolve to an absolute moz-extension:// URL so the path isn't treated as
  // relative to the popup's own location (popup/popup.html).
  const base = messenger.runtime.getURL("search/search.html");
  const url = query ? `${base}?q=${encodeURIComponent(query)}` : base;
  await messenger.tabs.create({ url });
  window.close();
}

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("search-form");
  const input = document.getElementById("search-input");
  const fullLink = document.getElementById("open-full");

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) {
      input.focus();
      return;
    }
    openSearch(q);
  });

  fullLink.addEventListener("click", (e) => {
    e.preventDefault();
    openSearch(input.value.trim());
  });

  document.getElementById("open-settings").addEventListener("click", (e) => {
    e.preventDefault();
    messenger.runtime.openOptionsPage();
    window.close();
  });
});
