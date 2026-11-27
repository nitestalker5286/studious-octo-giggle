(() => {
  "use strict";

  function enableLiveResults(formSelector, inputSelector, targetSelector, delay, statusSelector) {
    const form = document.querySelector(formSelector);
    if (!form || !window.fetch || !window.DOMParser) return;
    const input = form.querySelector(inputSelector);
    const target = document.querySelector(targetSelector);
    const status = statusSelector ? document.querySelector(statusSelector) : null;
    if (!input || !target) return;

    let timer;
    let controller;

    async function updateResults() {
      if (controller) controller.abort();
      controller = new AbortController();
      const params = new URLSearchParams(new FormData(form));
      const queryString = params.toString();
      const url = `${window.location.pathname}${queryString ? `?${queryString}` : ""}`;
      target.setAttribute("aria-busy", "true");
      if (status) status.textContent = "Updating results…";

      try {
        const response = await fetch(url, {
          headers: { "X-Requested-With": "live-search" },
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("Search request failed");
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        const replacement = page.querySelector(targetSelector);
        if (!replacement) throw new Error("Search results missing");
        target.replaceChildren(...replacement.childNodes);
        window.history.replaceState({}, "", url);
        if (status) status.textContent = "Results updated.";
      } catch (error) {
        if (error.name !== "AbortError") window.location.assign(url);
      } finally {
        target.removeAttribute("aria-busy");
      }
    }

    input.addEventListener("input", () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(updateResults, delay);
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      window.clearTimeout(timer);
      updateResults();
    });
  }

  enableLiveResults(
    "[data-live-component-search]",
    'input[name="q"]',
    "#component-results",
    300,
    "#live-search-status"
  );
  enableLiveResults(
    "[data-live-ship-search]",
    'input[name="q"]',
    "#ship-browser-dynamic",
    250,
    null
  );
})();
