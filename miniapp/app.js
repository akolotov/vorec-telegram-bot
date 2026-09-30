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
  const titleEditorScreen = document.getElementById("title-editor-screen");
  const titleInput = document.getElementById("title-editor-input");
  const titleEditorStatus = document.getElementById("title-editor-status");
  const titleEditorApply = document.getElementById("title-editor-apply");
  const titleEditorCancel = document.getElementById("title-editor-cancel");
  const titleEditorGenerate = document.getElementById("title-editor-generate");
  const tagPickerScreen = document.getElementById("tag-picker-screen");
  const tagPickerList = document.getElementById("tag-picker-list");
  const tagPickerStatus = document.getElementById("tag-picker-status");
  const tagPickerApply = document.getElementById("tag-picker-apply");
  const tagPickerCancel = document.getElementById("tag-picker-cancel");
  const settingsScreen = document.getElementById("settings-screen");
  const settingsStatus = document.getElementById("settings-status");
  const tagList = document.getElementById("tag-list");
  const addTagButton = document.getElementById("add-tag-button");
  const groupsElement = document.getElementById("groups");
  const listStatus = document.getElementById("list-status");
  const refreshButton = document.getElementById("refresh-button");
  const detailStatus = document.getElementById("detail-status");
  const detailElement = document.getElementById("detail");
  const copyDetailButton = document.getElementById("copy-detail-text");
  const copyDetailStatus = document.getElementById("copy-detail-status");
  let copyRequestNumber = 0;
  const tabs = {date: document.getElementById("date-tab"), tags: document.getElementById("tags-tab")};
  let currentView = "date";
  let requestNumber = 0;
  let tagRequestNumber = 0;
  let settingsSession = 0;
  let currentDetailId = null;
  let currentDetailTitle = "";
  let currentDetailTagIds = [];
  let detailDirty = false;
  let titleGenerationRequestNumber = 0;
  let titleGenerationPending = false;
  let titleGenerationTaskId = null;
  let pickerRequestNumber = 0;
  let pickerLoaded = false;
  let savePending = false;
  let saveUncertain = false;
  let listAnchor = null;
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

  function httpError(message, status) {
    const error = new Error(message);
    error.status = status;
    return error;
  }

  async function api(path, options = {}) {
    let response;
    try {
      response = await fetch(new URL(path, base), {
        ...options,
        headers: {"X-Telegram-Init-Data": initData, ...options.headers},
        cache: "no-store",
      });
    } catch (_) {
      throw new Error("Network error. Please try again.");
    }
    if (response.status === 401) throw httpError("Сессия завершилась. Откройте приложение снова из меню бота.", 401);
    if (response.status === 409) throw httpError("A category with this name already exists.", 409);
    if (response.status === 404) {
      const message = path.startsWith("api/title-jobs/")
        ? "Generation expired. Please try again."
        : path.startsWith("api/transcripts/")
          ? "Memo not found. Refresh the list."
          : "Category not found. Refresh the list.";
      throw httpError(message, 404);
    }
    if (response.status === 429) throw httpError("Too many title requests. Try again shortly.", 429);
    if (response.status === 400) {
      const result = await response.json();
      throw httpError(result.error || "Invalid category details.", 400);
    }
    if (response.status === 502 && path.endsWith("/generate-title")) {
      throw httpError("Could not generate title. Please try again.", 502);
    }
    if (!response.ok) throw httpError("The request failed. Please try again.", response.status);
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
        button.dataset.transcriptId = String(item.id);
        const title = document.createElement("span");
        title.className = "transcript-title";
        title.textContent = item.title;
        button.append(title, makeTags(item.tags));
        button.addEventListener("click", () => {
          listAnchor = {id: item.id, top: button.getBoundingClientRect().top};
          openDetail(item.id);
        });
        section.append(button);
      });
      groupsElement.append(section);
    });
  }

  function restoreListAnchor(anchor, request) {
    requestAnimationFrame(() => {
      if (listScreen.hidden || request !== requestNumber) return;
      const button = groupsElement.querySelector(`[data-transcript-id="${anchor.id}"]`);
      if (button) {
        window.scrollTo(0, window.scrollY + button.getBoundingClientRect().top - anchor.top);
      } else {
        window.scrollTo(0, 0);
      }
    });
  }

  async function loadList(anchor = null) {
    const request = ++requestNumber;
    refreshButton.disabled = true;
    groupsElement.replaceChildren();
    listStatus.textContent = "Загрузка…";
    try {
      const params = new URLSearchParams({view: currentView, timezone});
      const result = await api(`api/groups?${params}`);
      if (request === requestNumber) {
        renderGroups(result.groups);
        if (anchor) restoreListAnchor(anchor, request);
      }
    } catch (error) {
      if (request === requestNumber) {
        showError(listStatus, error.message, () => loadList(anchor));
      }
    } finally {
      if (request === requestNumber) refreshButton.disabled = false;
    }
  }

  function showList() {
    if (savePending) return;
    resetCopyState();
    ++requestNumber;
    ++tagRequestNumber;
    ++settingsSession;
    const returningFromDetail = !detailScreen.hidden;
    ++pickerRequestNumber;
    detailScreen.hidden = true;
    titleEditorScreen.hidden = true;
    tagPickerScreen.hidden = true;
    const returningFromSettings = !settingsScreen.hidden;
    settingsScreen.hidden = true;
    listScreen.hidden = false;
    webApp?.BackButton?.hide();
    const anchor = returningFromDetail ? listAnchor : null;
    const refresh = returningFromSettings || (returningFromDetail && detailDirty);
    detailDirty = false;
    if (refresh) loadList(anchor);
    else if (anchor) restoreListAnchor(anchor, requestNumber);
    else window.scrollTo(0, 0);
  }

  function resetCopyState() {
    ++copyRequestNumber;
    copyDetailButton.disabled = false;
    copyDetailStatus.textContent = "";
  }

  async function copyDetailText() {
    if (copyDetailButton.disabled || detailScreen.hidden || detailElement.hidden) return;
    const request = ++copyRequestNumber;
    copyDetailStatus.textContent = "";
    const clipboard = window.navigator?.clipboard;
    if (typeof clipboard?.writeText !== "function") {
      copyDetailStatus.textContent = "Copying is unavailable in this client.";
      return;
    }
    copyDetailButton.disabled = true;
    try {
      await clipboard.writeText(document.getElementById("detail-text").textContent);
      if (request === copyRequestNumber) copyDetailStatus.textContent = "Copied";
    } catch {
      if (request === copyRequestNumber) {
        copyDetailStatus.textContent = "Could not copy text. Please try again.";
      }
    } finally {
      if (request === copyRequestNumber) copyDetailButton.disabled = false;
    }
  }

  function renderDetail(item) {
    resetCopyState();
    currentDetailTitle = item.title;
    currentDetailTagIds = item.tag_ids;
    document.getElementById("detail-date").textContent = formatInstant(item.created_at);
    document.getElementById("detail-title").textContent = item.title;
    document.getElementById("detail-tags").replaceWith(makeDetailTags(item.tags));
    document.getElementById("detail-text").textContent = item.text;
    detailStatus.textContent = "";
    detailElement.hidden = false;
  }

  async function openDetail(id) {
    resetCopyState();
    const request = ++requestNumber;
    currentDetailId = id;
    listScreen.hidden = true;
    settingsScreen.hidden = true;
    titleEditorScreen.hidden = true;
    detailScreen.hidden = false;
    detailElement.hidden = true;
    detailStatus.textContent = "Загрузка…";
    webApp?.BackButton?.show();
    window.scrollTo(0, 0);
    try {
      const item = await api(`api/transcripts/${id}`);
      if (request !== requestNumber) return;
      renderDetail(item);
    } catch (error) {
      if (request === requestNumber) showError(detailStatus, error.message, () => openDetail(id));
    }
  }

  function makeDetailTags(names) {
    const tags = makeTags(names);
    tags.id = "detail-tags";
    return tags;
  }

  function setTitleEditorControls() {
    const busy = savePending || titleGenerationPending;
    titleInput.disabled = busy;
    titleEditorApply.disabled = busy;
    titleEditorGenerate.disabled = busy;
    titleEditorCancel.disabled = savePending;
    if (!titleEditorScreen.hidden) {
      if (savePending) webApp?.BackButton?.hide();
      else webApp?.BackButton?.show();
    }
  }

  function openTitleEditor() {
    ++titleGenerationRequestNumber;
    titleGenerationPending = false;
    titleGenerationTaskId = null;
    saveUncertain = false;
    titleInput.value = currentDetailTitle;
    titleEditorStatus.textContent = "";
    detailScreen.hidden = true;
    titleEditorScreen.hidden = false;
    setTitleEditorControls();
    window.scrollTo(0, 0);
    titleInput.focus?.({preventScroll: true});
  }

  function returnFromTitleEditor() {
    ++titleGenerationRequestNumber;
    titleGenerationPending = false;
    titleGenerationTaskId = null;
    titleEditorScreen.hidden = true;
    detailScreen.hidden = false;
    webApp?.BackButton?.show();
    window.scrollTo(0, 0);
  }

  async function generateTitle() {
    if (savePending || titleGenerationPending || titleEditorScreen.hidden) return;
    const request = ++titleGenerationRequestNumber;
    titleGenerationPending = true;
    setTitleEditorControls();
    titleEditorStatus.textContent = "Generating…";
    try {
      let taskId = titleGenerationTaskId;
      if (!taskId) {
        const started = await api(`api/transcripts/${currentDetailId}/generate-title`, {
          method: "POST",
        });
        if (request !== titleGenerationRequestNumber || titleEditorScreen.hidden) return;
        if (typeof started.task_id !== "string" || !started.task_id) {
          throw new Error("Could not start title generation. Please try again.");
        }
        taskId = started.task_id;
        titleGenerationTaskId = taskId;
      }
      while (request === titleGenerationRequestNumber && !titleEditorScreen.hidden) {
        const result = await api(`api/title-jobs/${encodeURIComponent(taskId)}`);
        if (request !== titleGenerationRequestNumber || titleEditorScreen.hidden) return;
        if (result.status === "ready") {
          if (typeof result.title !== "string" || !result.title.trim()) {
            throw new Error("The service returned an empty title. Please try again.");
          }
          titleGenerationTaskId = null;
          titleInput.value = result.title;
          titleEditorStatus.textContent = "";
          break;
        }
        if (result.status === "failed") {
          titleGenerationTaskId = null;
          throw new Error("Could not generate title. Please try again.");
        }
        if (result.status !== "pending") {
          throw new Error("Unexpected generation status. Please try again.");
        }
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
    } catch (error) {
      if (request === titleGenerationRequestNumber && !titleEditorScreen.hidden) {
        if (error.status === 404) titleGenerationTaskId = null;
        titleEditorStatus.textContent = error.message;
      }
    } finally {
      if (request === titleGenerationRequestNumber && !titleEditorScreen.hidden) {
        titleGenerationPending = false;
        setTitleEditorControls();
      }
    }
  }

  async function cancelTitleEditor() {
    if (savePending || titleEditorScreen.hidden) return;
    if (!saveUncertain) {
      returnFromTitleEditor();
      return;
    }
    savePending = true;
    setTitleEditorControls();
    titleEditorStatus.textContent = "Checking saved title…";
    try {
      const item = await api(`api/transcripts/${currentDetailId}`);
      renderDetail(item);
      detailDirty = true;
      saveUncertain = false;
      returnFromTitleEditor();
    } catch (_) {
      titleEditorStatus.textContent = "Could not confirm saved title. Try Cancel again when connected.";
    } finally {
      savePending = false;
      setTitleEditorControls();
    }
  }

  async function applyTitleEditor(event) {
    event.preventDefault();
    if (savePending || titleGenerationPending || titleEditorScreen.hidden) return;
    const title = titleInput.value.trim();
    if (!title) {
      titleEditorStatus.textContent = "A title must not be empty.";
      return;
    }
    if (!saveUncertain && title === currentDetailTitle) {
      returnFromTitleEditor();
      return;
    }
    savePending = true;
    setTitleEditorControls();
    titleEditorStatus.textContent = "Saving…";
    try {
      const item = await api(`api/transcripts/${currentDetailId}/title`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({title}),
      });
      renderDetail(item);
      detailDirty = true;
      saveUncertain = false;
      returnFromTitleEditor();
    } catch (error) {
      const rejected = [400, 401, 404, 409, 415].includes(error.status);
      saveUncertain ||= !rejected;
      detailDirty ||= saveUncertain || error.status === 404;
      titleEditorStatus.textContent = saveUncertain
        ? `${error.message} Retry Apply or Cancel to check saved title.`
        : error.message;
    } finally {
      savePending = false;
      setTitleEditorControls();
    }
  }

  function setPickerBusy(busy) {
    tagPickerApply.disabled = busy || !pickerLoaded;
    tagPickerCancel.disabled = busy;
    tagPickerList.querySelectorAll("input").forEach((input) => { input.disabled = busy; });
    if (!tagPickerScreen.hidden) {
      if (busy) webApp?.BackButton?.hide();
      else webApp?.BackButton?.show();
    }
  }

  async function loadPickerTags() {
    const request = ++pickerRequestNumber;
    pickerLoaded = false;
    tagPickerApply.disabled = true;
    tagPickerList.replaceChildren();
    tagPickerStatus.textContent = "Loading…";
    try {
      const result = await api("api/tags");
      if (request !== pickerRequestNumber || tagPickerScreen.hidden) return;
      const tags = result.tags.sort((a, b) =>
        b.usage_count - a.usage_count || a.name.localeCompare(b.name) || a.id - b.id
      );
      tags.forEach((tag) => {
        const row = document.createElement("label");
        row.className = "tag-picker-row";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = String(tag.id);
        checkbox.checked = currentDetailTagIds.includes(tag.id);
        const name = document.createElement("span");
        name.textContent = `#${tag.name}`;
        row.append(checkbox, name);
        tagPickerList.append(row);
      });
      if (!tags.length) tagPickerStatus.textContent = "No categories yet.";
      else tagPickerStatus.textContent = "";
      pickerLoaded = true;
      setPickerBusy(false);
    } catch (error) {
      if (request === pickerRequestNumber && !tagPickerScreen.hidden) {
        showError(tagPickerStatus, error.message, loadPickerTags);
      }
    }
  }

  function openTagPicker() {
    detailScreen.hidden = true;
    tagPickerScreen.hidden = false;
    tagPickerCancel.disabled = false;
    saveUncertain = false;
    webApp?.BackButton?.show();
    window.scrollTo(0, 0);
    loadPickerTags();
  }

  function returnToDetail() {
    ++pickerRequestNumber;
    tagPickerScreen.hidden = true;
    detailScreen.hidden = false;
    webApp?.BackButton?.show();
    window.scrollTo(0, 0);
  }

  async function cancelTagPicker() {
    if (savePending || tagPickerScreen.hidden) return;
    if (!saveUncertain) {
      returnToDetail();
      return;
    }
    savePending = true;
    setPickerBusy(true);
    tagPickerStatus.textContent = "Checking saved tags…";
    try {
      const item = await api(`api/transcripts/${currentDetailId}`);
      renderDetail(item);
      detailDirty = true;
      saveUncertain = false;
      returnToDetail();
    } catch (_) {
      tagPickerStatus.textContent = "Could not confirm saved tags. Try Cancel again when connected.";
    } finally {
      savePending = false;
      setPickerBusy(false);
    }
  }

  async function applyTagPicker(event) {
    event.preventDefault();
    if (savePending || !pickerLoaded || tagPickerScreen.hidden) return;
    const tagIds = [...tagPickerList.querySelectorAll("input:checked")]
      .map((input) => Number(input.value));
    if (!saveUncertain && tagIds.length === currentDetailTagIds.length
        && tagIds.every((id) => currentDetailTagIds.includes(id))) {
      returnToDetail();
      return;
    }
    savePending = true;
    setPickerBusy(true);
    tagPickerStatus.textContent = "Saving…";
    try {
      const item = await api(`api/transcripts/${currentDetailId}/tags`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({tag_ids: tagIds}),
      });
      renderDetail(item);
      detailDirty = true;
      saveUncertain = false;
      returnToDetail();
    } catch (error) {
      const rejected = [400, 401, 404, 409, 415].includes(error.status);
      saveUncertain ||= !rejected;
      detailDirty ||= saveUncertain || error.status === 404;
      tagPickerStatus.textContent = saveUncertain
        ? `${error.message} Retry Apply or Cancel to check saved tags.`
        : error.message;
    } finally {
      savePending = false;
      setPickerBusy(false);
    }
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

  async function refreshAfterTagChange(session) {
    if (!settingsScreen.hidden) {
      if (session === settingsSession || editingTagId === null) await loadTags();
    } else if (!listScreen.hidden) {
      await loadList();
    } else if (!detailScreen.hidden && currentDetailId !== null) {
      await openDetail(currentDetailId);
    }
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
      const session = settingsSession;
      save.disabled = true;
      cancel.disabled = true;
      errorMessage.textContent = "";
      try {
        await api(isNew ? "api/tags" : `api/tags/${tag.id}`, {
          method: isNew ? "POST" : "PUT",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: name.value, description: description.value}),
        });
        if (session === settingsSession) editingTagId = null;
        await refreshAfterTagChange(session);
      } catch (error) {
        if (session === settingsSession && !settingsScreen.hidden) {
          errorMessage.textContent = error.message;
        }
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
        const session = settingsSession;
        remove.disabled = true;
        settingsStatus.textContent = "";
        try {
          await api(`api/tags/${tag.id}`, {method: "DELETE"});
          await refreshAfterTagChange(session);
        } catch (error) {
          if (session === settingsSession && !settingsScreen.hidden) {
            settingsStatus.textContent = error.message;
          }
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
    const request = ++tagRequestNumber;
    addTagButton.disabled = true;
    tagList.replaceChildren();
    settingsStatus.textContent = "Loading…";
    try {
      const result = await api("api/tags");
      if (request === tagRequestNumber && !settingsScreen.hidden) {
        currentTags = result.tags;
        renderTags();
        settingsStatus.textContent = "";
      }
    } catch (error) {
      if (request === tagRequestNumber && !settingsScreen.hidden) {
        settingsStatus.textContent = error.message;
      }
    } finally {
      if (request === tagRequestNumber && !settingsScreen.hidden) addTagButton.disabled = false;
    }
  }

  function openSettings() {
    ++requestNumber;
    ++settingsSession;
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
  refreshButton.addEventListener("click", () => {
    if (refreshButton.disabled) return;
    window.scrollTo(0, 0);
    return loadList();
  });
  document.getElementById("settings-button").addEventListener("click", openSettings);
  document.getElementById("settings-back-button").addEventListener("click", showList);
  addTagButton.addEventListener("click", () => {
    editingTagId = "new";
    settingsStatus.textContent = "";
    renderTags();
    document.getElementById("editing-tag-name").focus({preventScroll: true});
  });
  document.getElementById("back-button").addEventListener("click", showList);
  document.getElementById("edit-detail-title").addEventListener("click", openTitleEditor);
  copyDetailButton.addEventListener("click", copyDetailText);
  document.getElementById("title-editor-form").addEventListener("submit", applyTitleEditor);
  titleEditorGenerate.addEventListener("click", generateTitle);
  titleEditorCancel.addEventListener("click", cancelTitleEditor);
  document.getElementById("edit-detail-tags").addEventListener("click", openTagPicker);
  document.getElementById("tag-picker-form").addEventListener("submit", applyTagPicker);
  tagPickerCancel.addEventListener("click", cancelTagPicker);
  webApp?.BackButton?.onClick(() => {
    if (savePending) return;
    if (!titleEditorScreen.hidden) cancelTitleEditor();
    else if (!tagPickerScreen.hidden) cancelTagPicker();
    else showList();
  });
  if (!initData) {
    showError(listStatus, "Откройте приложение через меню бота в Telegram.");
    return;
  }
  webApp.ready();
  webApp.expand();
  loadList();
})();
