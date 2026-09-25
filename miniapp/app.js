(() => {
  "use strict";

  const webApp = window.Telegram?.WebApp;
  const initData = webApp?.initData || "";
  const base = new URL(window.location.href);
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const dateFormatter = new Intl.DateTimeFormat("ru-RU", {
    day: "numeric", month: "short", year: "numeric", timeZone: timezone,
  });
  const listScreen = document.getElementById("list-screen");
  const detailScreen = document.getElementById("detail-screen");
  const settingsScreen = document.getElementById("settings-screen");
  const settingsStatus = document.getElementById("settings-status");
  const tagList = document.getElementById("tag-list");
  const addTagButton = document.getElementById("add-tag-button");
  const groupsElement = document.getElementById("groups");
  const listStatus = document.getElementById("list-status");
  const detailStatus = document.getElementById("detail-status");
  const detailElement = document.getElementById("detail");
  const tabs = {date: document.getElementById("date-tab"), tags: document.getElementById("tags-tab")};
  let currentView = "date";
  let requestNumber = 0;
  let currentTags = [];
  let editingTagId = null;

  function formatInstant(value) {
    return dateFormatter.format(new Date(value));
  }

  function formatDay(value) {
    const [year, month, day] = value.split("-").map(Number);
    return new Intl.DateTimeFormat("ru-RU", {
      day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
    }).format(new Date(Date.UTC(year, month - 1, day)));
  }

  function makeTags(names) {
    const container = document.createElement("div");
    container.className = "tags";
    names.forEach((name) => {
      const tag = document.createElement("span");
      tag.textContent = `#${name}`;
      container.append(tag);
    });
    return container;
  }

  function showError(container, message, retry) {
    container.replaceChildren(document.createTextNode(message));
    if (retry) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "retry";
      button.textContent = "Повторить";
      button.addEventListener("click", retry);
      container.append(document.createElement("br"), button);
    }
  }

  async function api(path, options = {}) {
    const response = await fetch(new URL(path, base), {
      ...options,
      headers: {"X-Telegram-Init-Data": initData, ...options.headers},
      cache: "no-store",
    });
    if (response.status === 401) throw new Error("Сессия завершилась. Откройте приложение снова из меню бота.");
    if (response.status === 409) throw new Error("A category with this name already exists.");
    if (response.status === 404) throw new Error("Category not found. Refresh the list.");
    if (response.status === 400) {
      const result = await response.json();
      throw new Error(result.error || "Invalid category details.");
    }
    if (!response.ok) throw new Error("The request failed. Please try again.");
    if (response.status === 204) return null;
    return response.json();
  }

  function renderGroups(groups) {
    groupsElement.replaceChildren();
    if (!groups.length) {
      listStatus.textContent = "Заметок пока нет.";
      return;
    }
    listStatus.textContent = "";
    groups.forEach((group) => {
      const section = document.createElement("section");
      section.className = "group";
      const heading = document.createElement("h2");
      heading.className = "group-title";
      heading.textContent = currentView === "date"
        ? formatDay(group.key)
        : group.untagged ? "Uncategorized" : `#${group.tag}`;
      section.append(heading);
      group.items.forEach((item) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "transcript";
        const title = document.createElement("span");
        title.className = "transcript-title";
        title.textContent = item.title;
        button.append(title, makeTags(item.tags));
        button.addEventListener("click", () => openDetail(item.id));
        section.append(button);
      });
      groupsElement.append(section);
    });
  }

  async function loadList() {
    const request = ++requestNumber;
    groupsElement.replaceChildren();
    listStatus.textContent = "Загрузка…";
    try {
      const params = new URLSearchParams({view: currentView, timezone});
      const result = await api(`api/groups?${params}`);
      if (request === requestNumber) renderGroups(result.groups);
    } catch (error) {
      if (request === requestNumber) showError(listStatus, error.message, loadList);
    }
  }

  function showList() {
    ++requestNumber;
    detailScreen.hidden = true;
    const returningFromSettings = !settingsScreen.hidden;
    settingsScreen.hidden = true;
    listScreen.hidden = false;
    webApp?.BackButton?.hide();
    window.scrollTo(0, 0);
    if (returningFromSettings) loadList();
  }

  async function openDetail(id) {
    const request = ++requestNumber;
    listScreen.hidden = true;
    settingsScreen.hidden = true;
    detailScreen.hidden = false;
    detailElement.hidden = true;
    detailStatus.textContent = "Загрузка…";
    webApp?.BackButton?.show();
    window.scrollTo(0, 0);
    try {
      const item = await api(`api/transcripts/${id}`);
      if (request !== requestNumber) return;
      document.getElementById("detail-date").textContent = formatInstant(item.created_at);
      document.getElementById("detail-title").textContent = item.title;
      document.getElementById("detail-tags").replaceWith(makeDetailTags(item.tags));
      document.getElementById("detail-text").textContent = item.text;
      detailStatus.textContent = "";
      detailElement.hidden = false;
    } catch (error) {
      if (request === requestNumber) showError(detailStatus, error.message, () => openDetail(id));
    }
  }

  function makeDetailTags(names) {
    const tags = makeTags(names);
    tags.id = "detail-tags";
    return tags;
  }

  function makeIconButton(kind, label) {
    const paths = {
      edit: "M16.5 3.5a2.1 2.1 0 0 1 3 3L9 17l-4 1 1-4L16.5 3.5Z",
      delete: "M4 7h16M10 11v6m4-6v6M6 7l1 14h10l1-14M9 7V4h6v3",
    };
    const button = document.createElement("button");
    button.type = "button";
    button.className = "icon-button";
    button.setAttribute("aria-label", label);
    button.title = label;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", paths[kind]);
    if (kind === "edit") path.setAttribute("transform", "translate(0 1.5)");
    svg.append(path);
    button.append(svg);
    return button;
  }

  function makeTagEditor(tag) {
    const isNew = tag === null;
    const card = document.createElement("article");
    card.className = "tag-card";
    const form = document.createElement("form");
    form.className = "tag-form";
    const title = document.createElement("h2");
    title.textContent = isNew ? "New category" : `Edit #${tag.name}`;
    const nameLabel = document.createElement("label");
    nameLabel.textContent = "Name";
    const name = document.createElement("input");
    name.type = "text";
    name.required = true;
    name.autocomplete = "off";
    name.value = tag?.name || "";
    name.id = "editing-tag-name";
    nameLabel.htmlFor = name.id;
    const descriptionLabel = document.createElement("label");
    descriptionLabel.textContent = "Description";
    const description = document.createElement("textarea");
    description.required = true;
    description.rows = 3;
    description.value = tag?.description || "";
    description.id = "editing-tag-description";
    descriptionLabel.htmlFor = description.id;
    const errorMessage = document.createElement("div");
    errorMessage.className = "tag-form-error";
    errorMessage.setAttribute("role", "alert");
    const actions = document.createElement("div");
    actions.className = "form-actions";
    const save = document.createElement("button");
    save.className = "primary-button";
    save.type = "submit";
    save.textContent = isNew ? "Create" : "Save";
    const cancel = document.createElement("button");
    cancel.className = "secondary-button";
    cancel.type = "button";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", () => {
      editingTagId = null;
      renderTags();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      cancel.disabled = true;
      errorMessage.textContent = "";
      try {
        await api(isNew ? "api/tags" : `api/tags/${tag.id}`, {
          method: isNew ? "POST" : "PUT",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: name.value, description: description.value}),
        });
        editingTagId = null;
        await loadTags();
      } catch (error) {
        errorMessage.textContent = error.message;
      } finally {
        save.disabled = false;
        cancel.disabled = false;
      }
    });
    actions.append(save, cancel);
    form.append(title, nameLabel, name, descriptionLabel, description, errorMessage, actions);
    card.append(form);
    return card;
  }

  function renderTags() {
    tagList.replaceChildren();
    addTagButton.disabled = editingTagId !== null;
    if (editingTagId === "new") tagList.append(makeTagEditor(null));
    if (!currentTags.length && editingTagId !== "new") {
      const empty = document.createElement("p");
      empty.textContent = "No categories yet.";
      tagList.append(empty);
    }
    currentTags.forEach((tag) => {
      if (editingTagId === tag.id) {
        tagList.append(makeTagEditor(tag));
        return;
      }
      const card = document.createElement("article");
      card.className = "tag-card";
      const name = document.createElement("div");
      name.className = "tag-card-name";
      name.textContent = `#${tag.name}`;
      const description = document.createElement("p");
      description.className = "tag-card-description";
      description.textContent = tag.description;
      const body = document.createElement("div");
      body.className = "tag-card-body";
      const actions = document.createElement("div");
      actions.className = "tag-actions";
      const edit = makeIconButton("edit", `Edit #${tag.name}`);
      edit.disabled = editingTagId !== null;
      edit.addEventListener("click", () => {
        editingTagId = tag.id;
        settingsStatus.textContent = "";
        renderTags();
        document.getElementById("editing-tag-name").focus({preventScroll: true});
      });
      const remove = makeIconButton("delete", `Delete #${tag.name}`);
      remove.disabled = editingTagId !== null;
      remove.addEventListener("click", async () => {
        if (!window.confirm(`Delete #${tag.name}? It will be removed from existing memos.`)) return;
        remove.disabled = true;
        settingsStatus.textContent = "";
        try {
          await api(`api/tags/${tag.id}`, {method: "DELETE"});
          await loadTags();
        } catch (error) {
          settingsStatus.textContent = error.message;
          remove.disabled = false;
        }
      });
      actions.append(edit, remove);
      body.append(description, actions);
      card.append(name, body);
      tagList.append(card);
    });
  }

  async function loadTags() {
    const request = ++requestNumber;
    addTagButton.disabled = true;
    tagList.replaceChildren();
    settingsStatus.textContent = "Loading…";
    try {
      const result = await api("api/tags");
      if (request === requestNumber) {
        currentTags = result.tags;
        renderTags();
        settingsStatus.textContent = "";
      }
    } catch (error) {
      if (request === requestNumber) settingsStatus.textContent = error.message;
    } finally {
      if (request === requestNumber) addTagButton.disabled = false;
    }
  }

  function openSettings() {
    ++requestNumber;
    listScreen.hidden = true;
    settingsScreen.hidden = false;
    editingTagId = null;
    currentTags = [];
    tagList.replaceChildren();
    webApp?.BackButton?.show();
    window.scrollTo(0, 0);
    loadTags();
  }

  function setView(view) {
    if (view === currentView) return;
    currentView = view;
    Object.entries(tabs).forEach(([name, button]) => {
      button.classList.toggle("active", name === view);
      button.setAttribute("aria-selected", String(name === view));
    });
    loadList();
  }

  tabs.date.addEventListener("click", () => setView("date"));
  tabs.tags.addEventListener("click", () => setView("tags"));
  document.getElementById("settings-button").addEventListener("click", openSettings);
  document.getElementById("settings-back-button").addEventListener("click", showList);
  addTagButton.addEventListener("click", () => {
    editingTagId = "new";
    settingsStatus.textContent = "";
    renderTags();
    document.getElementById("editing-tag-name").focus({preventScroll: true});
  });
  document.getElementById("back-button").addEventListener("click", showList);
  webApp?.BackButton?.onClick(() => showList());
  if (!initData) {
    showError(listStatus, "Откройте приложение через меню бота в Telegram.");
    return;
  }
  webApp.ready();
  webApp.expand();
  loadList();
})();
