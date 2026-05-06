// Popup search bar. Submits open the results in a new tab and close the popup.

const SEARCH_PAGE = "search/search.html";

async function openSearch(query) {
  const url = query
    ? `${SEARCH_PAGE}?q=${encodeURIComponent(query)}`
    : SEARCH_PAGE;
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
});
