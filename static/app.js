(() => {
  "use strict";

  const STORAGE_KEY = "starCitizenSalvageTracker.pinnedComponents.v1";

  function storageAvailable() {
    try {
      const testKey = `${STORAGE_KEY}.test`;
      window.localStorage.setItem(testKey, "1");
      window.localStorage.removeItem(testKey);
      return true;
    } catch (_error) {
      return false;
    }
  }

  const canStorePins = storageAvailable();

  function normalizedGroupValue(value) {
    return String(value || "").trim().toLowerCase();
  }

  function groupIdForItem(item) {
    const canonical = [item.name, item.category, item.size, item.grade, item.spec]
      .map(normalizedGroupValue)
      .join("\x1f");
    const bytes = new TextEncoder().encode(canonical);
    let binary = "";
    bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
    return `group-${window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")}`;
  }

  function normalizeSource(source, fallbackUrl = "") {
    const shipId = String(source.ship_id || source.shipId || "");
    return {
      shipId,
      shipName: source.ship_name || source.shipName || "Unknown ship",
      manufacturer: source.manufacturer || "",
      source: source.source || "",
      quantity: Number(source.quantity || 0),
      shipUrl: source.shipUrl || (shipId ? `/ships/${shipId}` : fallbackUrl || "#"),
    };
  }

  function normalizePinnedItems(parsed) {
    const grouped = new Map();
    parsed.forEach((raw) => {
      if (!raw || typeof raw.name !== "string") return;
      const id = String(raw.id || "").startsWith("group-")
        ? String(raw.id)
        : groupIdForItem(raw);
      let sources = Array.isArray(raw.sources)
        ? raw.sources.map((source) => normalizeSource(source, raw.primaryShipUrl || raw.shipUrl || ""))
        : [];
      if (!sources.length && (raw.shipId || raw.shipName)) {
        sources = [normalizeSource(raw, raw.shipUrl || "")];
      }
      const existing = grouped.get(id) || {
        id,
        name: raw.name,
        category: raw.category || "Other",
        size: raw.size || "",
        grade: raw.grade || "",
        spec: raw.spec || "",
        quantity: 0,
        sources: [],
        primaryShipUrl: raw.primaryShipUrl || raw.shipUrl || "",
        pinnedAt: raw.pinnedAt || new Date().toISOString(),
      };
      const sourceMap = new Map(
        existing.sources.map((source) => [`${source.shipId}\x1f${source.source}`, source])
      );
      sources.forEach((source) => {
        const key = `${source.shipId}\x1f${source.source}`;
        const current = sourceMap.get(key);
        if (current) current.quantity += source.quantity;
        else sourceMap.set(key, source);
      });
      existing.sources = [...sourceMap.values()].sort((a, b) =>
        a.shipName.localeCompare(b.shipName, undefined, { sensitivity: "base" })
      );
      existing.quantity = existing.sources.length
        ? existing.sources.reduce((total, source) => total + source.quantity, 0)
        : Number(raw.quantity || existing.quantity || 0);
      if (!existing.primaryShipUrl && existing.sources.length) {
        existing.primaryShipUrl = existing.sources[0].shipUrl;
      }
      grouped.set(id, existing);
    });
    return [...grouped.values()]
      .sort((a, b) => String(b.pinnedAt).localeCompare(String(a.pinnedAt)))
      .slice(0, 500);
  }

  function readPins() {
    if (!canStorePins) return [];
    try {
      const parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "[]");
      if (!Array.isArray(parsed)) return [];
      return normalizePinnedItems(parsed);
    } catch (_error) {
      return [];
    }
  }

  function writePins(items) {
    if (!canStorePins) return false;
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
      return true;
    } catch (_error) {
      return false;
    }
  }

  function itemFromButton(button) {
    let sources = [];
    try {
      sources = JSON.parse(button.dataset.sources || "[]").map((source) =>
        normalizeSource(source, button.dataset.primaryShipUrl || "")
      );
    } catch (_error) {
      sources = [];
    }
    const item = {
      id: String(button.dataset.groupId || ""),
      name: button.dataset.name || "Unnamed component",
      category: button.dataset.category || "Other",
      size: button.dataset.size || "",
      grade: button.dataset.grade || "",
      spec: button.dataset.spec || "",
      quantity: Number(button.dataset.quantity || 0),
      sources,
      primaryShipUrl: button.dataset.primaryShipUrl || (sources[0] && sources[0].shipUrl) || "#",
      pinnedAt: new Date().toISOString(),
    };
    item.id = item.id || groupIdForItem(item);
    return item;
  }

  function pinMap(items) {
    return new Map(items.map((item) => [String(item.id), item]));
  }

  function syncPinButtons(items) {
    const pinned = pinMap(items);
    document.querySelectorAll("[data-pin-component]").forEach((button) => {
      const id = String(button.dataset.groupId || "") || groupIdForItem(button.dataset);
      const active = pinned.has(id);
      button.classList.toggle("pinned", active);
      button.disabled = !canStorePins;
      button.title = canStorePins ? "" : "Browser storage is unavailable";
      button.setAttribute("aria-pressed", active ? "true" : "false");
      const icon = button.querySelector(".pin-icon");
      const label = button.querySelector(".pin-label");
      if (icon) icon.textContent = active ? "★" : "☆";
      if (label) label.textContent = active ? "Unpin" : "Pin";
      button.setAttribute(
        "aria-label",
        `${active ? "Unpin" : "Pin"} ${button.dataset.name || "component"}`
      );
    });
  }

  function updatePinCounts(count) {
    document.querySelectorAll("[data-pin-count]").forEach((element) => {
      element.textContent = String(count);
      element.setAttribute(
        "aria-label",
        `${count} pinned component${count === 1 ? "" : "s"}`
      );
    });
  }

  function makeElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function makeUnpinButton(item, compact = false) {
    const button = makeElement("button", compact ? "pin-button pin-button-icon pinned" : "pin-button pinned");
    button.type = "button";
    button.dataset.unpinId = item.id;
    button.setAttribute("aria-label", `Unpin ${item.name}`);
    const icon = makeElement("span", "pin-icon", "★");
    icon.setAttribute("aria-hidden", "true");
    button.append(icon);
    if (!compact) button.append(makeElement("span", "pin-label", "Unpin"));
    return button;
  }

  function makeMiniWatchlistItem(item) {
    const article = makeElement("article", "watchlist-mini-item");
    const body = makeElement("div", "watchlist-mini-body");
    const name = makeElement("a", "watchlist-mini-name", item.name);
    name.href = item.primaryShipUrl || "#";
    const sourceLabel = `${item.sources.length} source${item.sources.length === 1 ? "" : "s"}`;
    body.append(name, makeElement("span", "watchlist-mini-meta", `${sourceLabel} · ${item.category}`));
    article.append(body, makeUnpinButton(item, true));
    return article;
  }

  function makeWatchlistCard(item) {
    const article = makeElement("article", "watchlist-card");
    const heading = makeElement("div", "watchlist-card-heading");
    const titleWrap = makeElement("div", "");
    titleWrap.append(
      makeElement("p", "eyebrow", item.category),
      makeElement("h2", "", item.name)
    );
    heading.append(titleWrap, makeUnpinButton(item));

    const facts = makeElement("dl", "result-facts watchlist-facts");
    [["Size", item.size], ["Grade", item.grade], ["Spec", item.spec], ["Qty", item.quantity]].forEach(
      ([label, value]) => {
        const group = makeElement("div", "");
        group.append(makeElement("dt", "", label), makeElement("dd", "", value || "—"));
        facts.append(group);
      }
    );
    const details = makeElement("details", "sources-disclosure watchlist-sources");
    details.append(makeElement("summary", "", `${item.sources.length} source${item.sources.length === 1 ? "" : "s"}`));
    const sourceList = makeElement("div", "source-list");
    item.sources.forEach((source) => {
      const link = makeElement("a", "source-item");
      link.href = source.shipUrl || (source.shipId ? `/ships/${source.shipId}` : "#");
      const sourceBody = makeElement("span", "");
      sourceBody.append(makeElement("strong", "", source.shipName));
      if (source.manufacturer) sourceBody.append(makeElement("small", "", source.manufacturer));
      if (source.source) sourceBody.append(makeElement("small", "", `Source: ${source.source}`));
      link.append(sourceBody, makeElement("strong", "source-quantity", `Qty ${source.quantity}`));
      sourceList.append(link);
    });
    details.append(sourceList);
    const open = makeElement("a", "button button-secondary button-small watchlist-open", "Open first source");
    open.href = item.primaryShipUrl || (item.sources[0] && item.sources[0].shipUrl) || "#";
    article.append(heading, facts, details, open);
    return article;
  }

  function renderWatchlists(items) {
    const fullContainer = document.querySelector("[data-watchlist-items]");
    const fullEmpty = document.querySelector("[data-watchlist-empty]");
    if (fullContainer) {
      fullContainer.replaceChildren(...items.map(makeWatchlistCard));
      fullContainer.hidden = items.length === 0;
    }
    if (fullEmpty) fullEmpty.hidden = items.length !== 0;

    const homeContainer = document.querySelector("[data-home-watchlist-items]");
    const homeEmpty = document.querySelector("[data-home-watchlist-empty]");
    if (homeContainer) {
      homeContainer.replaceChildren(...items.slice(0, 8).map(makeMiniWatchlistItem));
      homeContainer.hidden = items.length === 0;
    }
    if (homeEmpty) homeEmpty.hidden = items.length !== 0;

    document.querySelectorAll("[data-clear-pins]").forEach((button) => {
      button.disabled = items.length === 0 || !canStorePins;
    });
  }

  function refreshWatchlistUI() {
    const items = readPins();
    updatePinCounts(items.length);
    syncPinButtons(items);
    renderWatchlists(items);
    document.querySelectorAll("[data-storage-warning]").forEach((warning) => {
      warning.hidden = canStorePins;
    });
  }

  function togglePin(button) {
    const item = itemFromButton(button);
    if (!item.id || !canStorePins) return;
    const items = readPins();
    const index = items.findIndex((existing) => String(existing.id) === item.id);
    if (index >= 0) items.splice(index, 1);
    else items.unshift(item);
    if (writePins(items)) refreshWatchlistUI();
  }

  function unpin(id) {
    if (!canStorePins) return;
    const items = readPins().filter((item) => String(item.id) !== String(id));
    if (writePins(items)) refreshWatchlistUI();
  }

  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const pinButton = event.target.closest("[data-pin-component]");
    if (pinButton) {
      togglePin(pinButton);
      return;
    }
    const unpinButton = event.target.closest("[data-unpin-id]");
    if (unpinButton) {
      unpin(unpinButton.dataset.unpinId);
      return;
    }
    const clearButton = event.target.closest("[data-clear-pins]");
    if (clearButton && readPins().length) {
      if (window.confirm("Clear all pinned components from this browser?")) {
        writePins([]);
        refreshWatchlistUI();
      }
    }
  });

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
        refreshWatchlistUI();
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

  window.addEventListener("storage", (event) => {
    if (event.key === STORAGE_KEY) refreshWatchlistUI();
  });
  refreshWatchlistUI();
})();
