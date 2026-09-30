const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tagName, registry) {
    this.tagName = tagName;
    this.registry = registry;
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.textContent = "";
    this.attributes = {};
    this.classList = {toggle() {}};
  }

  set id(value) {
    this._id = value;
    this.registry[value] = this;
  }

  get id() { return this._id; }

  append(...children) {
    children.forEach((child) => {
      child.parent = this;
      this.children.push(child);
    });
  }

  replaceChildren(...children) {
    this.children = [];
    this.append(...children);
  }

  replaceWith(replacement) {
    const index = this.parent.children.indexOf(this);
    replacement.parent = this.parent;
    this.parent.children[index] = replacement;
  }

  addEventListener(name, listener) {
    (this.listeners[name] ||= []).push(listener);
  }

  dispatch(name) {
    return Promise.all((this.listeners[name] || []).map((listener) =>
      listener({preventDefault() {}})
    ));
  }

  querySelectorAll(selector) {
    const descendants = this.children.flatMap((child) =>
      [child, ...child.querySelectorAll("*")]
    );
    if (selector === "*") return descendants;
    if (selector === "input") return descendants.filter((item) => item.tagName === "input");
    if (selector === "input:checked") {
      return descendants.filter((item) => item.tagName === "input" && item.checked);
    }
    return [];
  }

  querySelector(selector) {
    const match = selector.match(/^\[data-transcript-id="(\d+)"\]$/);
    return this.querySelectorAll("*").find((item) =>
      match && item.dataset.transcriptId === match[1]
    );
  }

  getBoundingClientRect() { return {top: 100}; }
  setAttribute(name, value) { this.attributes[name] = value; }
}

function response(body) {
  return {ok: true, status: 200, json: async () => body};
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

test("Copy text preserves note content and isolates pending copies across notes", async () => {
  const registry = {};
  const html = fs.readFileSync(path.join(__dirname, "../miniapp/index.html"), "utf8");
  for (const match of html.matchAll(/id="([^"]+)"/g)) {
    new Element("div", registry).id = match[1];
  }
  const tagsParent = new Element("div", registry);
  tagsParent.append(registry["detail-tags"]);
  for (const id of ["detail-screen", "settings-screen", "tag-picker-screen", "title-editor-screen"]) {
    registry[id].hidden = true;
  }
  const notes = [1, 2].map((id) => ({
    id, created_at: "2026-09-24T10:00:00+00:00", title: `Title ${id}`,
    text: id === 1 ? "  First line\n\nCafé 📝\nLast line  \n" : "Second note",
    tags: ["work"], tag_ids: [1],
  }));
  const writes = [];
  let finishCopy;
  let failCopy;
  const clipboard = {
    writeText(text) {
      assert.equal(this, clipboard);
      writes.push(text);
      return new Promise((resolve, reject) => {
        finishCopy = resolve;
        failCopy = reject;
      });
    },
  };
  const window = {
    location: {href: "https://example.test/apps/bot/"}, scrollY: 0,
    scrollTo() {}, navigator: {clipboard},
    Telegram: {WebApp: {
      initData: "signed-data", ready() {}, expand() {},
      BackButton: {onClick() {}, hide() {}, show() {}},
    }},
  };
  let requests = 0;
  const fetch = async (url) => {
    requests++;
    if (url.pathname.endsWith("/api/groups")) {
      return response({groups: [{key: "2026-09-24", items: notes}]});
    }
    const note = notes.find((item) => url.pathname.endsWith(`/api/transcripts/${item.id}`));
    if (note) return response(note);
    throw new Error(`Unexpected request: ${url}`);
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../miniapp/app.js"), "utf8"), {
    window, fetch, URL, URLSearchParams, Intl,
    document: {
      getElementById: (id) => registry[id],
      createElement: (tag) => new Element(tag, registry),
      createTextNode: (value) => new Element(value, registry),
    },
    setTimeout, clearTimeout, AbortController, requestAnimationFrame: (callback) => callback(),
  });
  await flush();
  const open = async (id) => {
    await registry.groups.querySelector(`[data-transcript-id="${id}"]`).dispatch("click");
    await flush();
  };
  await open(1);
  const button = registry["copy-detail-text"];
  const status = registry["copy-detail-status"];
  const requestsBeforeCopy = requests;
  const copying = button.dispatch("click");
  assert.deepEqual(writes, [notes[0].text]);
  assert.equal(button.disabled, true);
  assert.equal(status.textContent, "");
  await button.dispatch("click");
  assert.equal(writes.length, 1);
  finishCopy();
  await copying;
  assert.equal(button.disabled, false);
  assert.equal(status.textContent, "Copied");
  assert.equal(requests, requestsBeforeCopy);

  const failing = button.dispatch("click");
  assert.equal(status.textContent, "");
  failCopy(new Error("Permission denied"));
  await failing;
  assert.equal(status.textContent, "Could not copy text. Please try again.");
  assert.equal(button.disabled, false);

  window.navigator.clipboard = undefined;
  await button.dispatch("click");
  assert.equal(status.textContent, "Copying is unavailable in this client.");
  assert.equal(button.disabled, false);
  window.navigator.clipboard = {writeText() { throw new Error("Blocked"); }};
  await button.dispatch("click");
  assert.equal(status.textContent, "Could not copy text. Please try again.");
  assert.equal(button.disabled, false);
  window.navigator.clipboard = clipboard;

  const staleCopy = button.dispatch("click");
  const finishOldCopy = finishCopy;
  await registry["back-button"].dispatch("click");
  await open(2);
  assert.equal(status.textContent, "");
  assert.equal(button.disabled, false);
  const newCopy = button.dispatch("click");
  assert.equal(writes.at(-1), notes[1].text);
  finishOldCopy();
  await staleCopy;
  assert.equal(status.textContent, "");
  assert.equal(button.disabled, true);
  finishCopy();
  await newCopy;
  assert.equal(status.textContent, "Copied");
  assert.equal(button.disabled, false);

  const staleFailure = button.dispatch("click");
  const rejectOldCopy = failCopy;
  await registry["back-button"].dispatch("click");
  await open(1);
  rejectOldCopy(new Error("Late failure"));
  await staleFailure;
  assert.equal(status.textContent, "");
  assert.equal(button.disabled, false);
});

test("Tag picker blocks navigation during save and handles failed saves", async () => {
  const registry = {};
  const ids = [
    "copy-detail-text", "copy-detail-status",
    "list-screen", "detail-screen", "settings-screen", "tag-picker-screen",
    "title-editor-screen", "title-editor-input", "title-editor-status",
    "title-editor-apply", "title-editor-cancel", "title-editor-generate",
    "title-editor-form", "edit-detail-title",
    "settings-status", "tag-list", "add-tag-button", "groups", "list-status",
    "detail-status", "detail", "date-tab", "tags-tab", "detail-date",
    "detail-title", "detail-tags", "detail-text", "tag-picker-list",
    "tag-picker-status", "tag-picker-apply", "tag-picker-cancel",
    "tag-picker-form", "refresh-button", "settings-button", "settings-back-button", "back-button",
    "edit-detail-tags",
  ];
  ids.forEach((id) => { new Element("div", registry).id = id; });
  registry["detail-tags"].parent = new Element("div", registry);
  registry["detail-tags"].parent.append(registry["detail-tags"]);
  registry["detail-screen"].hidden = true;
  registry["settings-screen"].hidden = true;
  registry["tag-picker-screen"].hidden = true;
  registry["title-editor-screen"].hidden = true;
  let telegramBack;
  let backVisible = false;
  let groupReads = 0;
  let puts = 0;
  let finishPut;
  let rejectNextPut = false;
  let sessionExpired = false;
  const initial = {
    id: 1, created_at: "2026-09-24T10:00:00+00:00", title: "Memo",
    text: "Text", tags: ["work"], tag_ids: [1],
  };
  const updated = {...initial, tags: ["travel"], tag_ids: [2]};
  let memo = initial;
  const document = {
    getElementById: (id) => registry[id],
    createElement: (tag) => new Element(tag, registry),
    createTextNode: (value) => new Element(value, registry),
  };
  const window = {
    location: {href: "https://example.test/apps/bot/"},
    scrollY: 0,
    scrollTo(_x, y) { this.scrollY = y; },
    Telegram: {WebApp: {
      initData: "signed-data", ready() {}, expand() {},
      BackButton: {
        onClick(callback) { telegramBack = callback; },
        hide() { backVisible = false; },
        show() { backVisible = true; },
      },
    }},
  };
  const fetch = async (url, options) => {
    if (sessionExpired) return {ok: false, status: 401};
    if (url.pathname.endsWith("/api/groups")) {
      groupReads++;
      return response({groups: [{key: "2026-09-24", items: [{
        id: 1, title: "Memo", tags: groupReads === 1 ? ["work"] : ["travel"],
      }]}]});
    }
    if (url.pathname.endsWith("/api/tags")) {
      return response({tags: [
        {id: 1, name: "work", usage_count: 1},
        {id: 2, name: "travel", usage_count: 0},
      ]});
    }
    if (url.pathname.endsWith("/api/transcripts/1/tags") && options.method === "PUT") {
      puts++;
      if (rejectNextPut) {
        rejectNextPut = false;
        throw new Error("Connection lost");
      }
      return new Promise((resolve) => {
        finishPut = () => {
          memo = updated;
          resolve(response(updated));
        };
      });
    }
    if (url.pathname.endsWith("/api/transcripts/1")) return response(memo);
    throw new Error(`Unexpected request: ${url}`);
  };
  const source = fs.readFileSync(path.join(__dirname, "../miniapp/app.js"), "utf8");
  vm.runInNewContext(source, {
    document, window, fetch, URL, URLSearchParams, Intl,
    setTimeout, clearTimeout, AbortController, requestAnimationFrame: (callback) => callback(),
  });
  await flush();
  await registry.groups.querySelector('[data-transcript-id="1"]').dispatch("click");
  await flush();
  await registry["edit-detail-tags"].dispatch("click");
  await flush();
  const inputs = registry["tag-picker-list"].querySelectorAll("input");
  inputs.find((input) => input.value === "1").checked = false;
  inputs.find((input) => input.value === "2").checked = true;
  const saving = registry["tag-picker-form"].dispatch("submit");
  await registry["tag-picker-cancel"].dispatch("click");
  telegramBack();
  await registry["tag-picker-form"].dispatch("submit");
  assert.equal(puts, 1);
  assert.equal(registry["tag-picker-screen"].hidden, false);
  assert.equal(registry["detail-screen"].hidden, true);
  assert.equal(registry["tag-picker-apply"].disabled, true);
  assert.equal(registry["tag-picker-cancel"].disabled, true);
  assert.equal(backVisible, false);
  assert.ok(inputs.every((input) => input.disabled));
  finishPut();
  await saving;
  assert.equal(registry["detail-screen"].hidden, false);
  assert.equal(registry["tag-picker-screen"].hidden, true);
  assert.equal(registry["detail-tags"].children[0].textContent, "#travel");
  telegramBack();
  await flush();
  assert.equal(groupReads, 2);

  await registry.groups.querySelector('[data-transcript-id="1"]').dispatch("click");
  await flush();
  await registry["edit-detail-tags"].dispatch("click");
  await flush();
  const retryInputs = registry["tag-picker-list"].querySelectorAll("input");
  retryInputs.find((input) => input.value === "1").checked = true;
  retryInputs.find((input) => input.value === "2").checked = false;
  rejectNextPut = true;
  await registry["tag-picker-form"].dispatch("submit");
  assert.equal(registry["tag-picker-screen"].hidden, false);
  assert.equal(registry["tag-picker-cancel"].disabled, false);
  await registry["tag-picker-cancel"].dispatch("click");
  assert.equal(registry["detail-screen"].hidden, false);
  assert.equal(registry["detail-tags"].children[0].textContent, "#travel");

  await registry["edit-detail-tags"].dispatch("click");
  await flush();
  const expiredInputs = registry["tag-picker-list"].querySelectorAll("input");
  expiredInputs.find((input) => input.value === "1").checked = true;
  expiredInputs.find((input) => input.value === "2").checked = false;
  sessionExpired = true;
  await registry["tag-picker-form"].dispatch("submit");
  assert.equal(registry["tag-picker-screen"].hidden, false);
  await registry["tag-picker-cancel"].dispatch("click");
  assert.equal(registry["tag-picker-screen"].hidden, true);
  assert.equal(registry["detail-screen"].hidden, false);
});

test("Title editor previews AI titles, saves on Apply, and resolves uncertain saves", async () => {
  const registry = {};
  const ids = [
    "copy-detail-text", "copy-detail-status",
    "list-screen", "detail-screen", "settings-screen", "tag-picker-screen",
    "title-editor-screen", "title-editor-input", "title-editor-status",
    "title-editor-apply", "title-editor-cancel", "title-editor-generate",
    "title-editor-form", "edit-detail-title", "settings-status", "tag-list",
    "add-tag-button", "groups", "list-status", "detail-status", "detail",
    "refresh-button",
    "date-tab", "tags-tab", "detail-date", "detail-title", "detail-tags",
    "detail-text", "tag-picker-list", "tag-picker-status", "tag-picker-apply",
    "tag-picker-cancel", "tag-picker-form", "settings-button",
    "settings-back-button", "back-button", "edit-detail-tags",
  ];
  ids.forEach((id) => { new Element("div", registry).id = id; });
  registry["detail-tags"].parent = new Element("div", registry);
  registry["detail-tags"].parent.append(registry["detail-tags"]);
  for (const id of ["detail-screen", "settings-screen", "tag-picker-screen", "title-editor-screen"]) {
    registry[id].hidden = true;
  }
  let memo = {
    id: 1, created_at: "2026-09-24T10:00:00+00:00", title: "Old title",
    text: "Text", tags: ["work"], tag_ids: [1],
  };
  let telegramBack;
  let backVisible = false;
  let groupReads = 0;
  let puts = 0;
  let finishGeneration;
  let deferGeneration = false;
  let failGeneration = false;
  let failJob = false;
  let failPollOnce = false;
  let pendingPolls = 0;
  let postCount = 0;
  let finishPut;
  let deferPut = false;
  let failPutAfterSave = false;
  const window = {
    location: {href: "https://example.test/apps/bot/"},
    scrollY: 0,
    scrollTo(_x, y) { this.scrollY = y; },
    Telegram: {WebApp: {
      initData: "signed-data", ready() {}, expand() {},
      BackButton: {
        onClick(callback) { telegramBack = callback; },
        hide() { backVisible = false; },
        show() { backVisible = true; },
      },
    }},
  };
  const document = {
    getElementById: (id) => registry[id],
    createElement: (tag) => new Element(tag, registry),
    createTextNode: (value) => new Element(value, registry),
  };
  const fetch = async (url, options) => {
    if (url.pathname.endsWith("/api/groups")) {
      groupReads++;
      return response({groups: [{key: "2026-09-24", items: [{
        id: 1, title: memo.title, tags: memo.tags,
      }]}]});
    }
    if (url.pathname.endsWith("/api/transcripts/1/generate-title")) {
      assert.equal(options.method, "POST");
      postCount++;
      if (failGeneration) throw new Error("Offline");
      return response({task_id: `job-${postCount}`});
    }
    if (url.pathname.includes("/api/title-jobs/")) {
      if (failPollOnce) {
        failPollOnce = false;
        throw new Error("Offline");
      }
      if (deferGeneration) {
        return new Promise((resolve) => {
          finishGeneration = () => resolve(response({status: "ready", title: "Late AI title"}));
        });
      }
      if (pendingPolls > 0) {
        pendingPolls--;
        return response({status: "pending"});
      }
      return response(failJob ? {status: "failed"} : {status: "ready", title: "AI suggestion"});
    }
    if (url.pathname.endsWith("/api/transcripts/1/title") && options.method === "PUT") {
      puts++;
      const title = JSON.parse(options.body).title;
      if (failPutAfterSave) {
        failPutAfterSave = false;
        memo = {...memo, title};
        throw new Error("Connection lost");
      }
      if (deferPut) {
        return new Promise((resolve) => {
          finishPut = () => {
            memo = {...memo, title};
            resolve(response(memo));
          };
        });
      }
      memo = {...memo, title};
      return response(memo);
    }
    if (url.pathname.endsWith("/api/transcripts/1")) return response(memo);
    throw new Error(`Unexpected request: ${url}`);
  };
  const source = fs.readFileSync(path.join(__dirname, "../miniapp/app.js"), "utf8");
  vm.runInNewContext(source, {
    document, window, fetch, URL, URLSearchParams, Intl,
    setTimeout, clearTimeout, AbortController, requestAnimationFrame: (callback) => callback(),
    setTimeout: (callback) => setImmediate(callback),
  });
  await flush();
  const initialButton = registry.groups.querySelector('[data-transcript-id="1"]');
  initialButton.getBoundingClientRect = () => ({top: 42});
  await initialButton.dispatch("click");
  await flush();

  await registry["edit-detail-title"].dispatch("click");
  assert.equal(registry["title-editor-input"].value, "Old title");
  registry["title-editor-input"].value = "Manual draft";
  await registry["title-editor-cancel"].dispatch("click");
  assert.equal(registry["detail-title"].textContent, "Old title");
  assert.equal(puts, 0);

  await registry["edit-detail-title"].dispatch("click");
  deferGeneration = true;
  const lateGeneration = registry["title-editor-generate"].dispatch("click");
  await flush();
  assert.equal(registry["title-editor-apply"].disabled, true);
  assert.equal(registry["title-editor-generate"].disabled, true);
  assert.equal(registry["title-editor-cancel"].disabled, false);
  await registry["title-editor-cancel"].dispatch("click");
  await registry["edit-detail-title"].dispatch("click");
  registry["title-editor-input"].value = "Another draft";
  finishGeneration();
  await lateGeneration;
  assert.equal(registry["title-editor-input"].value, "Another draft");

  deferGeneration = false;
  failGeneration = true;
  await registry["title-editor-generate"].dispatch("click");
  assert.equal(registry["title-editor-input"].value, "Another draft");
  assert.equal(registry["title-editor-input"].disabled, false);
  failGeneration = false;
  failJob = true;
  await registry["title-editor-generate"].dispatch("click");
  assert.equal(registry["title-editor-input"].value, "Another draft");
  assert.match(registry["title-editor-status"].textContent, /Could not generate title/);
  failJob = false;
  failPollOnce = true;
  await registry["title-editor-generate"].dispatch("click");
  assert.equal(registry["title-editor-input"].value, "Another draft");
  const postsBeforeResume = postCount;
  pendingPolls = 1;
  await registry["title-editor-generate"].dispatch("click");
  assert.equal(postCount, postsBeforeResume);
  assert.equal(registry["title-editor-input"].value, "AI suggestion");
  registry["title-editor-input"].value = "Edited AI title";
  deferPut = true;
  const saving = registry["title-editor-form"].dispatch("submit");
  await registry["title-editor-cancel"].dispatch("click");
  telegramBack();
  await registry["title-editor-form"].dispatch("submit");
  assert.equal(puts, 1);
  assert.equal(registry["title-editor-screen"].hidden, false);
  assert.equal(registry["title-editor-cancel"].disabled, true);
  assert.equal(backVisible, false);
  finishPut();
  await saving;
  assert.equal(registry["detail-title"].textContent, "Edited AI title");
  telegramBack();
  await flush();
  assert.equal(groupReads, 2);
  assert.equal(registry.groups.querySelector('[data-transcript-id="1"]').children[0].textContent,
               "Edited AI title");
  assert.equal(window.scrollY, 58);

  await registry.groups.querySelector('[data-transcript-id="1"]').dispatch("click");
  await flush();
  await registry["edit-detail-title"].dispatch("click");
  await registry["title-editor-generate"].dispatch("click");
  await registry["title-editor-cancel"].dispatch("click");
  assert.equal(registry["detail-title"].textContent, "Edited AI title");

  await registry["edit-detail-title"].dispatch("click");
  registry["title-editor-input"].value = "Saved despite error";
  deferPut = false;
  failPutAfterSave = true;
  await registry["title-editor-form"].dispatch("submit");
  assert.equal(registry["title-editor-screen"].hidden, false);
  assert.match(registry["title-editor-status"].textContent, /Retry Apply or Cancel/);
  await registry["title-editor-cancel"].dispatch("click");
  assert.equal(registry["detail-title"].textContent, "Saved despite error");
  telegramBack();
  await flush();
  assert.equal(groupReads, 3);
  assert.equal(registry.groups.querySelector('[data-transcript-id="1"]').children[0].textContent,
               "Saved despite error");
});

test("Refresh reloads the current view at the top and preserves detail return position", async () => {
  const registry = {};
  const ids = [
    "copy-detail-text", "copy-detail-status",
    "list-screen", "detail-screen", "settings-screen", "tag-picker-screen",
    "title-editor-screen", "title-editor-input", "title-editor-status",
    "title-editor-apply", "title-editor-cancel", "title-editor-generate",
    "title-editor-form", "edit-detail-title",
    "settings-status", "tag-list", "add-tag-button", "groups", "list-status",
    "detail-status", "detail", "date-tab", "tags-tab", "detail-date",
    "detail-title", "detail-tags", "detail-text", "tag-picker-list",
    "tag-picker-status", "tag-picker-apply", "tag-picker-cancel",
    "tag-picker-form", "refresh-button", "settings-button", "settings-back-button",
    "back-button", "edit-detail-tags",
  ];
  ids.forEach((id) => { new Element("div", registry).id = id; });
  registry["detail-tags"].parent = new Element("div", registry);
  registry["detail-tags"].parent.append(registry["detail-tags"]);
  registry["detail-screen"].hidden = true;
  registry["settings-screen"].hidden = true;
  registry["tag-picker-screen"].hidden = true;
  registry["title-editor-screen"].hidden = true;
  const window = {
    location: {href: "https://example.test/apps/bot/"},
    scrollY: 0,
    scrollTo(_x, y) { this.scrollY = y; },
    Telegram: {WebApp: {
      initData: "signed-data", ready() {}, expand() {},
      BackButton: {onClick() {}, hide() {}, show() {}},
    }},
  };
  const document = {
    getElementById: (id) => registry[id],
    createElement: (tag) => new Element(tag, registry),
    createTextNode: (value) => new Element(value, registry),
  };
  let groupReads = 0;
  let nextGroupResponse;
  const views = [];
  const items = [{id: 1, title: "Memo", tags: ["work"]}];
  const fetch = async (url) => {
    if (url.pathname.endsWith("/api/groups")) {
      groupReads++;
      views.push(url.searchParams.get("view"));
      if (nextGroupResponse) {
        const pending = nextGroupResponse;
        nextGroupResponse = null;
        return pending;
      }
      return response({groups: url.searchParams.get("view") === "tags"
        ? [{key: 1, tag: "work", count: items.length, untagged: false}]
        : [{key: "2026-09-24", items}]});
    }
    if (url.pathname.endsWith("/api/transcripts")) return response({items});
    if (url.pathname.endsWith("/api/transcripts/1")) {
      return response({
        id: 1, created_at: "2026-09-24T10:00:00+00:00", title: "Memo",
        text: "Text", tags: ["work"], tag_ids: [1],
      });
    }
    throw new Error(`Unexpected request: ${url}`);
  };
  const source = fs.readFileSync(path.join(__dirname, "../miniapp/app.js"), "utf8");
  vm.runInNewContext(source, {
    document, window, fetch, URL, URLSearchParams, Intl,
    setTimeout, clearTimeout, AbortController, requestAnimationFrame: (callback) => callback(),
  });
  await flush();
  await registry["tags-tab"].dispatch("click");
  await flush();
  await registry.groups.children[0].children[0].children[0].dispatch("click");
  window.scrollY = 420;
  let finishRefresh;
  nextGroupResponse = new Promise((resolve) => { finishRefresh = resolve; });
  const refreshing = registry["refresh-button"].dispatch("click");
  assert.equal(window.scrollY, 0);
  assert.equal(registry["refresh-button"].disabled, true);
  await registry["refresh-button"].dispatch("click");
  assert.equal(groupReads, 3);
  items.unshift({id: 2, title: "New memo", tags: ["work"]});
  finishRefresh(response({groups: [{key: 1, tag: "work", count: items.length, untagged: false}]}));
  await refreshing;
  assert.equal(registry["refresh-button"].disabled, false);
  assert.equal(views.at(-1), "tags");
  assert.equal(registry.groups.querySelector('[data-transcript-id="2"]').children[0].textContent, "New memo");

  nextGroupResponse = Promise.reject(new Error("Connection lost"));
  await registry["refresh-button"].dispatch("click");
  assert.equal(registry["refresh-button"].disabled, false);
  assert.equal(registry["list-status"].children.at(-1).textContent, "Повторить");
  await registry["list-status"].children.at(-1).dispatch("click");
  assert.equal(groupReads, 5);

  window.scrollY = 420;
  const memoButton = registry.groups.querySelector('[data-transcript-id="1"]');
  memoButton.getBoundingClientRect = () => ({top: 520 - window.scrollY});
  await memoButton.dispatch("click");
  await registry["back-button"].dispatch("click");
  assert.equal(window.scrollY, 420);
  assert.equal(groupReads, 5);
});

async function categoryHarness(timers = {setTimeout, clearTimeout}) {
  const registry = {};
  const html = fs.readFileSync(path.join(__dirname, "../miniapp/index.html"), "utf8");
  for (const [, id] of html.matchAll(/id="([^"]+)"/g)) new Element("div", registry).id = id;
  const parent = new Element("div", registry);
  parent.append(registry["detail-tags"]);
  for (const id of ["detail-screen", "settings-screen", "tag-picker-screen", "title-editor-screen"]) {
    registry[id].hidden = true;
  }
  const item = (id) => ({id, title: `Memo ${id}`, tags: []});
  const state = {
    groups: [
      {key: 1, tag: "work", items: [item(1), item(2)]},
      {key: 2, tag: "travel", items: [item(3)]},
      {key: "untagged", untagged: true, items: [item(4)]},
    ],
    groupReads: 0, failNext: false, height: 2400, viewport: 800,
    memo: {id: 1, title: "Memo 1", created_at: "2026-09-24T10:00:00+00:00",
      tags: ["work"], tag_ids: [1], text: "Text"},
    positions: new Map([[1, 520]]),
  };
  let telegramBack;
  const window = {
    location: {href: "https://example.test/apps/bot/"}, scrollY: 0,
    scrollTo(_x, y) { this.scrollY = Math.max(0, Math.min(y, state.height - state.viewport)); },
    Telegram: {WebApp: {
      initData: "signed-data", ready() {}, expand() {},
      BackButton: {onClick(callback) { telegramBack = callback; }, hide() {}, show() {}},
    }},
  };
  const fetch = async (url, options = {}) => {
    if (url.pathname.endsWith("/api/groups")) {
      state.groupReads++;
      if (state.failNext) { state.failNext = false; throw new Error("Connection lost"); }
      if (state.groupResponse) return state.groupResponse;
      return response({groups: url.searchParams.get("view") === "tags" ? state.groups.map(({items, ...group}) => ({...group, count: items.length})) : [
        {key: "2026-09-24", items: [item(1)]},
      ]});
    }
    if (url.pathname.endsWith("/api/transcripts")) {
      state.itemReads = (state.itemReads || 0) + 1;
      const key = url.searchParams.has("tag_id") ? Number(url.searchParams.get("tag_id")) : "untagged";
      if (state.itemResponse) {
        const result = state.itemResponse(key, options);
        if (result) return result;
      }
      return response({items: state.groups.find((group) => group.key === key).items});
    }
    if (url.pathname.endsWith("/api/tags")) return response({tags: [
      {id: 1, name: "work"}, {id: 2, name: "travel"},
    ]});
    if (options.method === "PUT") {
      const body = JSON.parse(options.body);
      state.memo = {...state.memo, ...body};
      if (body.tag_ids) state.memo.tags = ["travel"];
      for (const group of state.groups) {
        for (const memo of group.items) if (memo.id === 1) memo.title = state.memo.title;
      }
    }
    return response(state.memo);
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../miniapp/app.js"), "utf8"), {
    document: {
      getElementById: (id) => registry[id],
      createElement(tag) {
        const element = new Element(tag, registry);
        element.getBoundingClientRect = () => ({
          top: (state.positions.get(Number(element.dataset.transcriptId)) || 100) - window.scrollY,
        });
        return element;
      },
      createTextNode: (text) => Object.assign(new Element("text", registry), {textContent: text}),
    },
    window, fetch, URL, URLSearchParams, Intl, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout, AbortController, requestAnimationFrame: (callback) => callback(),
  });
  await flush();
  const click = async (id) => { await registry[id].dispatch("click"); await flush(); };
  const group = (key) => registry.groups.children.find((section) => section.dataset.groupKey === String(key));
  const toggle = (key) => group(key).children[0].children[0];
  const expanded = (key) => {
    const section = group(key);
    assert.equal(toggle(key).attributes["aria-expanded"], String(!section.children[1].hidden));
    return !section.children[1].hidden;
  };
  const openMemo = async () => {
    await registry.groups.querySelector('[data-transcript-id="1"]').dispatch("click");
    await flush();
  };
  return {registry, state, window, click, group, toggle, expanded, openMemo,
    back: async () => { telegramBack(); await flush(); }};
}

test("Categories collapse on entry and preserve independent expansion across refresh and settings", async () => {
  const h = await categoryHarness();
  await h.click("tags-tab");
  for (const key of [1, 2, "untagged"]) assert.equal(h.expanded(key), false);
  const reads = h.state.groupReads;
  for (const key of [1, 2, "untagged"]) await h.toggle(key).dispatch("click");
  assert.equal(h.state.groupReads, reads);
  for (const key of [1, 2, "untagged"]) assert.equal(h.expanded(key), true);
  await h.toggle(2).dispatch("click");
  assert.equal(h.expanded(2), false);
  assert.equal(h.expanded(1), true);
  h.window.scrollTo(0, 420);
  await h.click("refresh-button");
  assert.equal(h.window.scrollY, 0);
  assert.equal(h.expanded(1), true);
  assert.equal(h.expanded(2), false);
  assert.equal(h.expanded("untagged"), true);
  h.state.failNext = true;
  await h.click("refresh-button");
  await h.registry["list-status"].children.at(-1).dispatch("click");
  await flush();
  assert.equal(h.expanded(1), true);
  await h.click("settings-button");
  await h.click("settings-back-button");
  assert.equal(h.expanded(1), true);
  assert.equal(h.expanded("untagged"), true);
  h.state.groups = h.state.groups.filter((group) => group.key !== "untagged");
  await h.click("refresh-button");
  h.state.groups.push({key: "untagged", untagged: true, items: [{id: 4, title: "Memo 4", tags: []}]});
  await h.click("refresh-button");
  assert.equal(h.expanded("untagged"), false);
  await h.click("date-tab");
  assert.equal(h.registry.groups.children[0].children[1].dataset.transcriptId, "1");
  await h.click("tags-tab");
  for (const key of [1, 2, "untagged"]) assert.equal(h.expanded(key), false);
});

test("Category return restores expansion and reachable position after title edits and regrouping", async () => {
  const h = await categoryHarness();
  await h.click("tags-tab");
  await h.toggle(1).dispatch("click");
  await h.toggle("untagged").dispatch("click");
  h.window.scrollTo(0, 420);
  await h.openMemo();
  const reads = h.state.groupReads;
  await h.click("back-button");
  assert.equal(h.state.groupReads, reads);
  assert.equal(h.window.scrollY, 420);
  assert.equal(h.expanded(1), true);
  assert.equal(h.expanded("untagged"), true);
  await h.openMemo();
  await h.click("edit-detail-title");
  h.registry["title-editor-input"].value = "Changed title";
  await h.registry["title-editor-form"].dispatch("submit");
  await h.back();
  assert.equal(h.state.groupReads, reads + 1);
  assert.equal(h.window.scrollY, 420);
  assert.equal(h.expanded(1), true);
  assert.equal(h.registry.groups.querySelector('[data-transcript-id="1"]').children[0].textContent, "Changed title");
  await h.openMemo();
  await h.click("edit-detail-tags");
  const inputs = h.registry["tag-picker-list"].querySelectorAll("input");
  inputs.find((input) => input.value === "1").checked = false;
  inputs.find((input) => input.value === "2").checked = true;
  await h.registry["tag-picker-form"].dispatch("submit");
  const memo = h.state.groups[0].items.shift();
  h.state.groups[1].items.push(memo);
  h.state.height = 1400;
  h.state.positions.set(1, 1300);
  await h.back();
  assert.equal(h.expanded(2), false);
  assert.equal(h.expanded(1), true);
  assert.equal(h.expanded("untagged"), true);
  assert.equal(h.window.scrollY, 420);
  assert.equal(h.group(1).children[1].querySelector('[data-transcript-id="1"]'), undefined);
  await h.toggle(2).dispatch("click");
  await h.openMemo();
  await h.click("edit-detail-title");
  h.registry["title-editor-input"].value = "Deleted elsewhere";
  await h.registry["title-editor-form"].dispatch("submit");
  for (const group of h.state.groups) group.items = group.items.filter((item) => item.id !== 1);
  await h.back();
  assert.equal(h.window.scrollY, 420);
});


function manualTimers() {
  const callbacks = new Map();
  let next = 0;
  return {
    setTimeout(callback, delay) {
      assert.equal(delay, 15000);
      callbacks.set(++next, callback);
      return next;
    },
    clearTimeout(id) { callbacks.delete(id); },
    expire() { for (const callback of [...callbacks.values()]) callback(); },
    get size() { return callbacks.size; },
  };
}

test("Categories sort by complete membership and load only on expansion with caching", async () => {
  const h = await categoryHarness();
  h.state.groups[1].items.push({...h.state.groups[0].items[0], tags: ["work", "travel"]});
  h.state.groups.push({key: 3, tag: "empty", items: []});
  h.state.groups.push({key: 4, tag: "alpha", items: [{id: 5, title: "Memo 5", tags: []}]});
  await h.click("tags-tab");
  assert.deepEqual(h.registry.groups.children.map((section) => section.dataset.groupKey), ["2", "1", "4", "3", "untagged"]);
  assert.equal(h.state.itemReads || 0, 0);
  assert.equal(h.toggle(1).children[1].textContent, "#work · 2");
  await h.toggle(2).dispatch("click");
  assert.equal(h.state.itemReads, 1);
  const shared = h.group(2).children[1].querySelector('[data-transcript-id="1"]');
  assert.equal(shared.children[1].children[0].textContent, "#work");
  await h.toggle(2).dispatch("click");
  await h.toggle(2).dispatch("click");
  assert.equal(h.state.itemReads, 1);
  await h.toggle(1).dispatch("click");
  assert.ok(h.group(1).children[1].querySelector('[data-transcript-id="1"]'));
  await h.toggle(3).dispatch("click");
  assert.equal(h.state.itemReads, 2);
  assert.equal(h.group(3).children[1].textContent, "No memos in this category.");
});

test("Return after editing finishes when another expanded category never responds", async () => {
  const timers = manualTimers();
  const h = await categoryHarness(timers);
  await h.click("tags-tab");
  await h.toggle(1).dispatch("click");
  await h.toggle(2).dispatch("click");
  h.window.scrollTo(0, 420);
  await h.openMemo();
  await h.click("edit-detail-title");
  h.registry["title-editor-input"].value = "Updated";
  await h.registry["title-editor-form"].dispatch("submit");
  let signal;
  h.state.itemResponse = (key, options) => {
    if (key === 2) { signal = options.signal; return new Promise(() => {}); }
  };
  await h.back();
  assert.equal(h.window.scrollY, 0);
  assert.equal(timers.size, 1);
  timers.expire();
  await flush();
  assert.equal(signal.aborted, true);
  assert.equal(h.window.scrollY, 420);
  assert.equal(h.registry["refresh-button"].disabled, false);
  assert.match(h.group(2).children[1].children[0].textContent, /timed out/);
  h.state.itemResponse = null;
  await h.group(2).children[1].children.at(-1).dispatch("click");
  assert.ok(h.group(2).children[1].querySelector('[data-transcript-id="3"]'));
  assert.equal(timers.size, 0);
});

test("Category timeout covers the response body and rejects late results", async () => {
  const timers = manualTimers();
  const h = await categoryHarness(timers);
  await h.click("tags-tab");
  let finishBody;
  h.state.itemResponse = () => ({ok: true, status: 200, json: () => new Promise((resolve) => { finishBody = resolve; })});
  const loading = h.toggle(1).dispatch("click");
  await flush();
  timers.expire();
  await loading;
  assert.match(h.group(1).children[1].children[0].textContent, /timed out/);
  finishBody({items: [{id: 99, title: "Late", tags: []}]});
  await flush();
  assert.equal(h.group(1).children[1].querySelector('[data-transcript-id="99"]'), undefined);
  assert.equal(timers.size, 0);
});

test("An emptied origin stays open without automatically opening another category", async () => {
  const h = await categoryHarness();
  await h.click("tags-tab");
  await h.toggle(1).dispatch("click");
  await h.openMemo();
  await h.click("edit-detail-tags");
  const inputs = h.registry["tag-picker-list"].querySelectorAll("input");
  inputs.find((input) => input.value === "1").checked = false;
  inputs.find((input) => input.value === "2").checked = true;
  await h.registry["tag-picker-form"].dispatch("submit");
  h.state.groups[1].items.push(h.state.groups[0].items[0]);
  h.state.groups[0].items = [];
  await h.back();
  assert.equal(h.expanded(1), true);
  assert.equal(h.expanded(2), false);
  assert.equal(h.toggle(1).children[1].textContent, "#work · 0");
  assert.equal(h.group(1).children[1].textContent, "No memos in this category.");
});

test("Collapsed and outdated category responses do not reopen or populate new views", async () => {
  const h = await categoryHarness();
  await h.click("tags-tab");
  let finish;
  h.state.itemResponse = () => new Promise((resolve) => { finish = resolve; });
  const loading = h.toggle(1).dispatch("click");
  await flush();
  await h.toggle(1).dispatch("click");
  finish(response({items: h.state.groups[0].items}));
  await loading;
  assert.equal(h.expanded(1), false);
  await h.click("refresh-button");
  const stale = h.toggle(1).dispatch("click");
  await flush();
  await h.click("date-tab");
  finish(response({items: [{id: 99, title: "Stale", tags: []}]}));
  await stale;
  assert.equal(h.registry.groups.querySelector('[data-transcript-id="99"]'), undefined);
});


test("Category metadata loading times out and can be retried", async () => {
  const timers = manualTimers();
  const h = await categoryHarness(timers);
  h.state.groupResponse = new Promise(() => {});
  await h.click("tags-tab");
  assert.equal(timers.size, 1);
  timers.expire();
  await flush();
  assert.match(h.registry["list-status"].children[0].textContent, /timed out/);
  assert.equal(h.registry["refresh-button"].disabled, false);
  h.state.groupResponse = null;
  await h.registry["list-status"].children.at(-1).dispatch("click");
  assert.equal(h.registry.groups.children.length, 3);
  assert.equal(timers.size, 0);
});
