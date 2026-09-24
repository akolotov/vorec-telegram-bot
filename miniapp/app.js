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
  const groupsElement = document.getElementById("groups");
  const listStatus = document.getElementById("list-status");
  const detailStatus = document.getElementById("detail-status");
  const detailElement = document.getElementById("detail");
  const tabs = {date: document.getElementById("date-tab"), tags: document.getElementById("tags-tab")};
  let currentView = "date";
  let requestNumber = 0;

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

  async function api(path) {
    const response = await fetch(new URL(path, base), {
      headers: {"X-Telegram-Init-Data": initData},
      cache: "no-store",
    });
    if (response.status === 401) throw new Error("Сессия завершилась. Откройте приложение снова из меню бота.");
    if (!response.ok) throw new Error("Не удалось загрузить данные.");
    return response.json();
  }

  function renderGroups(groups) {
    groupsElement.replaceChildren();
    if (!groups.length) {
      listStatus.textContent = "Транскриптов пока нет.";
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
        : group.untagged ? "Без тегов" : `#${group.tag}`;
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
    listScreen.hidden = false;
    webApp?.BackButton?.hide();
    window.scrollTo(0, 0);
  }

  async function openDetail(id) {
    const request = ++requestNumber;
    listScreen.hidden = true;
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
  document.getElementById("back-button").addEventListener("click", showList);
  webApp?.BackButton?.onClick(showList);
  if (!initData) {
    showError(listStatus, "Откройте приложение через меню бота в Telegram.");
    return;
  }
  webApp.ready();
  webApp.expand();
  loadList();
})();
