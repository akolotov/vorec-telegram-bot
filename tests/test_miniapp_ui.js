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
  setAttribute() {}
}

function response(body) {
  return {ok: true, status: 200, json: async () => body};
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

test("Tag picker blocks navigation during save and handles failed saves", async () => {
  const registry = {};
  const ids = [
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
    requestAnimationFrame: (callback) => callback(),
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
      if (failGeneration) throw new Error("Offline");
      if (deferGeneration) {
        return new Promise((resolve) => {
          finishGeneration = () => resolve(response({title: "Late AI title"}));
        });
      }
      return response({title: "AI suggestion"});
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
    requestAnimationFrame: (callback) => callback(),
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
  await registry["title-editor-generate"].dispatch("click");
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
      return response({groups: [{tag: "work", items}]});
    }
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
    requestAnimationFrame: (callback) => callback(),
  });
  await flush();
  await registry["tags-tab"].dispatch("click");
  await flush();
  window.scrollY = 420;
  let finishRefresh;
  nextGroupResponse = new Promise((resolve) => { finishRefresh = resolve; });
  const refreshing = registry["refresh-button"].dispatch("click");
  assert.equal(window.scrollY, 0);
  assert.equal(registry["refresh-button"].disabled, true);
  await registry["refresh-button"].dispatch("click");
  assert.equal(groupReads, 3);
  finishRefresh(response({groups: [{tag: "work", items: [
    {id: 2, title: "New memo", tags: ["work"]}, ...items,
  ]}]}));
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
