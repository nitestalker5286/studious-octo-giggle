(() => {
  "use strict";

  const STORAGE_KEY = "starCitizenSalvageTracker.pinnedComponents.v1";
  const SORT_KEY = "starCitizenSalvageTracker.watchlistSort.v1";
  const PRIORITY_LABELS = {
    high: "High",
    medium: "Medium",
    low: "Low",
    none: "No priority",
  };
  const PRIORITY_RANK = { high: 3, medium: 2, low: 1, none: 0 };
  const SORT_OPTIONS = new Set(["priority", "recent", "name", "category"]);

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

  function normalizePriority(value) {
    const priority = String(value || "none").toLowerCase();
    return Object.prototype.hasOwnProperty.call(PRIORITY_LABELS, priority) ? priority : "none";
  }

  function readSort() {
    if (!canStorePins) return "priority";
    try {
      const sort = window.localStorage.getItem(SORT_KEY) || "priority";
      return SORT_OPTIONS.has(sort) ? sort : "priority";
    } catch (_error) {
      return "priority";
    }
  }

  function writeSort(sort) {
    if (!canStorePins || !SORT_OPTIONS.has(sort)) return false;
    try {
      window.localStorage.setItem(SORT_KEY, sort);
      return true;
    } catch (_error) {
      return false;
    }
  }

  function sortPinnedItems(items, sort = readSort()) {
    return [...items].sort((a, b) => {
      if (sort === "recent") {
        return String(b.pinnedAt).localeCompare(String(a.pinnedAt));
      }
      if (sort === "name") {
        return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
      }
      if (sort === "category") {
        return a.category.localeCompare(b.category, undefined, { sensitivity: "base" })
          || a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
      }
      return (PRIORITY_RANK[b.priority] - PRIORITY_RANK[a.priority])
        || a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
    });
  }

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
    let shipSources = Array.isArray(source.sources)
      ? source.sources.map((value) => String(value).trim()).filter(Boolean)
      : [];
    if (!shipSources.length && source.source) shipSources = [String(source.source).trim()];
    return {
      shipId,
      shipName: source.ship_name || source.shipName || "Unknown ship",
      manufacturer: source.manufacturer || "",
      sources: [...new Set(shipSources)],
      source: shipSources.join(" | "),
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
        priority: normalizePriority(raw.priority),
        notes: typeof raw.notes === "string" ? raw.notes.slice(0, 500) : "",
      };
      const rawPriority = normalizePriority(raw.priority);
      if (PRIORITY_RANK[rawPriority] > PRIORITY_RANK[existing.priority]) {
        existing.priority = rawPriority;
      }
      if (!existing.notes && typeof raw.notes === "string") {
        existing.notes = raw.notes.slice(0, 500);
      }
      const sourceMap = new Map(
        existing.sources.map((source) => [source.shipId, source])
      );
      sources.forEach((source) => {
        const key = source.shipId;
        const current = sourceMap.get(key);
        if (current) {
          current.quantity += source.quantity;
          current.sources = [...new Set([...(current.sources || []), ...(source.sources || [])])];
          current.source = current.sources.join(" | ");
        } else sourceMap.set(key, source);
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
    return [...grouped.values()].slice(0, 500);
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
      priority: "none",
      notes: "",
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

  function makePriorityBadge(priority) {
    const normalized = normalizePriority(priority);
    const badge = makeElement(
      "span",
      `priority-badge priority-${normalized}`,
      PRIORITY_LABELS[normalized]
    );
    badge.dataset.priorityBadge = normalized;
    return badge;
  }

  function sourceCount(item) {
    return item.sources.reduce(
      (total, source) => total + (source.sources && source.sources.length ? source.sources.length : 0),
      0
    );
  }

  function makeMiniWatchlistItem(item) {
    const article = makeElement("article", "watchlist-mini-item");
    const body = makeElement("div", "watchlist-mini-body");
    const name = makeElement("a", "watchlist-mini-name", item.name);
    name.href = item.primaryShipUrl || "#";
    const count = sourceCount(item);
    const sourceLabel = `${count} source${count === 1 ? "" : "s"}`;
    const meta = makeElement("span", "watchlist-mini-meta", `${sourceLabel} · ${item.category}`);
    body.append(name, meta);
    if (item.priority !== "none") body.append(makePriorityBadge(item.priority));
    if (item.notes) {
      const note = makeElement("span", "watchlist-mini-note", item.notes);
      note.dataset.miniNotesId = item.id;
      body.append(note);
    }
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
    const headingTools = makeElement("div", "watchlist-card-tools");
    headingTools.append(makePriorityBadge(item.priority), makeUnpinButton(item));
    heading.append(titleWrap, headingTools);

    const facts = makeElement("dl", "result-facts watchlist-facts");
    [["Size", item.size], ["Grade", item.grade], ["Spec", item.spec], ["Qty", item.quantity]].forEach(
      ([label, value]) => {
        const group = makeElement("div", "");
        group.append(makeElement("dt", "", label), makeElement("dd", "", value || "—"));
        facts.append(group);
      }
    );
    const details = makeElement("details", "sources-disclosure watchlist-sources");
    const count = sourceCount(item);
    details.append(makeElement("summary", "", `${count} source${count === 1 ? "" : "s"}`));
    const sourceList = makeElement("div", "source-list");
    item.sources.forEach((source) => {
      const link = makeElement("a", "source-item");
      link.href = source.shipUrl || (source.shipId ? `/ships/${source.shipId}` : "#");
      const sourceBody = makeElement("span", "");
      sourceBody.append(makeElement("strong", "", source.shipName));
      if (source.manufacturer) sourceBody.append(makeElement("small", "", source.manufacturer));
      (source.sources || []).forEach((shipSource) =>
        sourceBody.append(makeElement("small", "", `Source: ${shipSource}`))
      );
      link.append(sourceBody, makeElement("strong", "source-quantity", `Qty ${source.quantity}`));
      sourceList.append(link);
    });
    details.append(sourceList);

    const editor = makeElement("div", "watchlist-editor");
    const priorityField = makeElement("label", "field watchlist-priority-field");
    const priorityLabel = makeElement("span", "", "Priority");
    const prioritySelect = makeElement("select", "");
    prioritySelect.dataset.priorityId = item.id;
    Object.entries(PRIORITY_LABELS).forEach(([value, label]) => {
      const option = makeElement("option", "", label);
      option.value = value;
      option.selected = value === item.priority;
      prioritySelect.append(option);
    });
    priorityField.append(priorityLabel, prioritySelect);

    const notesField = makeElement("label", "field watchlist-notes-field");
    const notesHeading = makeElement("span", "watchlist-notes-heading");
    notesHeading.append(
      document.createTextNode("Personal notes "),
      makeElement("small", "", "Saved in this browser")
    );
    const notes = makeElement("textarea", "watchlist-notes");
    notes.dataset.notesId = item.id;
    notes.maxLength = 500;
    notes.rows = 3;
    notes.placeholder = "Add reminders, loadout plans, or acquisition notes…";
    notes.value = item.notes;
    notesField.append(notesHeading, notes);
    editor.append(priorityField, notesField);

    const open = makeElement("a", "button button-secondary button-small watchlist-open", "Open first source");
    open.href = item.primaryShipUrl || (item.sources[0] && item.sources[0].shipUrl) || "#";
    article.append(heading, facts, details, editor, open);
    return article;
  }

  function renderWatchlists(items) {
    const sortedItems = sortPinnedItems(items);
    const fullContainer = document.querySelector("[data-watchlist-items]");
    const fullEmpty = document.querySelector("[data-watchlist-empty]");
    if (fullContainer) {
      fullContainer.replaceChildren(...sortedItems.map(makeWatchlistCard));
      fullContainer.hidden = sortedItems.length === 0;
    }
    if (fullEmpty) fullEmpty.hidden = sortedItems.length !== 0;

    const homeContainer = document.querySelector("[data-home-watchlist-items]");
    const homeEmpty = document.querySelector("[data-home-watchlist-empty]");
    if (homeContainer) {
      homeContainer.replaceChildren(...sortedItems.slice(0, 8).map(makeMiniWatchlistItem));
      homeContainer.hidden = sortedItems.length === 0;
    }
    if (homeEmpty) homeEmpty.hidden = sortedItems.length !== 0;

    document.querySelectorAll("[data-watchlist-sort]").forEach((select) => {
      select.value = readSort();
      select.disabled = !canStorePins;
    });

    document.querySelectorAll("[data-clear-pins]").forEach((button) => {
      button.disabled = sortedItems.length === 0 || !canStorePins;
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

  function updatePinMetadata(id, updates, rerender = false) {
    if (!canStorePins) return;
    const items = readPins();
    const item = items.find((candidate) => String(candidate.id) === String(id));
    if (!item) return;
    Object.assign(item, updates);
    if (!writePins(items)) return;
    if (rerender) refreshWatchlistUI();
    else {
      document.querySelectorAll("[data-mini-notes-id]").forEach((note) => {
        if (note.dataset.miniNotesId === String(id)) note.textContent = item.notes;
      });
    }
  }

  function renumberSourceRows(editor) {
    editor.querySelectorAll("[data-source-row]").forEach((row, index) => {
      const label = row.querySelector(".field > span");
      if (label) label.textContent = `Source ${index + 1}`;
    });
  }

  function addSourceRow(editor) {
    const template = editor.querySelector("[data-source-template]");
    const list = editor.querySelector("[data-source-list]");
    if (!template || !list) return;
    list.append(template.content.cloneNode(true));
    renumberSourceRows(editor);
    const inputs = list.querySelectorAll('input[name="sources"]');
    if (inputs.length) inputs[inputs.length - 1].focus();
  }

  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const addSource = event.target.closest("[data-add-source]");
    if (addSource) {
      const editor = addSource.closest("[data-source-editor]");
      if (editor) addSourceRow(editor);
      return;
    }
    const removeSource = event.target.closest("[data-remove-source]");
    if (removeSource) {
      const editor = removeSource.closest("[data-source-editor]");
      const row = removeSource.closest("[data-source-row]");
      if (editor && row) {
        const rows = editor.querySelectorAll("[data-source-row]");
        if (rows.length === 1) {
          const input = row.querySelector('input[name="sources"]');
          if (input) input.value = "";
        } else row.remove();
        renumberSourceRows(editor);
      }
      return;
    }
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

  document.addEventListener("change", (event) => {
    if (!(event.target instanceof Element)) return;
    const changeTarget = event.target.closest("[data-change-target]");
    if (changeTarget) {
      updateChangeRequestContext(changeTarget.closest("[data-change-request-form]"));
      return;
    }
    const priority = event.target.closest("[data-priority-id]");
    if (priority) {
      updatePinMetadata(
        priority.dataset.priorityId,
        { priority: normalizePriority(priority.value) },
        true
      );
      return;
    }
    const sort = event.target.closest("[data-watchlist-sort]");
    if (sort && writeSort(sort.value)) refreshWatchlistUI();
  });

  document.addEventListener("input", (event) => {
    if (!(event.target instanceof Element)) return;
    const notes = event.target.closest("[data-notes-id]");
    if (notes) {
      updatePinMetadata(notes.dataset.notesId, { notes: notes.value.slice(0, 500) });
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

  function updateChangeRequestContext(form) {
    if (!form) return;
    const target = form.querySelector("[data-change-target]");
    if (!target) return;
    form.querySelectorAll("[data-target-context]").forEach((section) => {
      section.hidden = section.dataset.targetContext !== target.value;
    });
    const customSubject = form.querySelector("[data-custom-subject]");
    if (customSubject) customSubject.hidden = target.value === "General";
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
    if (event.key === STORAGE_KEY || event.key === SORT_KEY) refreshWatchlistUI();
  });
  document.querySelectorAll("[data-change-request-form]").forEach(updateChangeRequestContext);
  document.querySelectorAll("[data-source-editor]").forEach(renumberSourceRows);
  refreshWatchlistUI();
})();
