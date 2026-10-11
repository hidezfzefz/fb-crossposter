#!/usr/bin/env node
"use strict";
/**
 * Headless check of the background coordinator's scan handling (../background.js).
 *
 * Drives the real background script with stubbed browser.tabs/WebSocket APIs and
 * asserts what the auto-farm depends on:
 *
 *   1. ONE dedicated source tab is opened and then reused — never recreated per scan
 *      (recreating it throws away Facebook's loaded feed and the scroll position)
 *   2. it is made the visible tab for the scan and the tab the user was on is
 *      restored afterwards (a hidden source tab cannot scroll the feed)
 *   3. a scan asked for sooner than the minimum gap (60 s floor) is DELAYED, not
 *      dropped or run immediately — a 7-second backend interval cannot hammer the feed
 *   4. `activateSourceTabOnScan: false` leaves the user's tab alone entirely
 *
 * Run: node extension/tests/test_scan_tabs.js     (exit code 0 = all checks passed)
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const BACKGROUND_JS = process.env.BACKGROUND_JS_PATH || path.join(__dirname, "..", "background.js");
const SOURCE = fs.readFileSync(BACKGROUND_JS, "utf8");

const SOURCE_URL = "https://www.facebook.com/groups/1144780033953810";
const TARGET_URL = "https://www.facebook.com/groups/1770877160904592";
const PARK_MS = 30000;   // timers at least this long are parked instead of fired

let checks = 0;
let failures = 0;
function check(label, got, expected) {
  checks++;
  const ok = JSON.stringify(got) === JSON.stringify(expected);
  if (ok) {
    console.log("    ok   " + label + ": " + JSON.stringify(got));
  } else {
    failures++;
    console.log("    FAIL " + label + ": got " + JSON.stringify(got) + ", expected " + JSON.stringify(expected));
  }
}

function tick() { return new Promise((resolve) => setTimeout(resolve, 0)); }
async function waitFor(label, predicate, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 3000);
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await tick();
  }
  throw new Error("timed out waiting for " + label);
}

function createBackground(opts) {
  opts = opts || {};
  const store = { settings: Object.assign({
    sourceGroupUrl: SOURCE_URL,
    targetGroupUrl: TARGET_URL,
    scanMaxPosts: 3,
    minScanIntervalSeconds: 60,
    activateSourceTabOnScan: true,
  }, opts.settings || {}) };

  const tabs = new Map();
  let nextTabId = 100;
  let activeTabId = 1;
  tabs.set(1, { id: 1, url: TARGET_URL, active: true, status: "complete", windowId: 1 });

  const created = [];        // browser.tabs.create calls (args)
  const activations = [];    // tab ids that were made active, in order
  const scanMessages = [];   // auto_scan messages sent to a content script
  const sent = [];           // messages the background sent to Python over the websocket
  const logs = [];
  const parked = [];      // long timers, kept instead of fired
  let sockets = [];
  let messageHandler = null;
  let responseHandler = null;

  const browser = {
    storage: {
      local: {
        get(key) {
          if (typeof key === "string") return Promise.resolve(key in store ? { [key]: store[key] } : {});
          return Promise.resolve(Object.assign({}, store));
        },
        set(obj) { Object.assign(store, obj); return Promise.resolve(); },
      },
    },
    browserAction: { setBadgeText() {}, setBadgeBackgroundColor() {} },
    runtime: {
      onMessage: { addListener: (fn) => { messageHandler = fn; } },
    },
    tabs: {
      query: (info) => {
        const all = Array.from(tabs.values());
        if (info && info.active) return Promise.resolve(all.filter((t) => t.id === activeTabId));
        return Promise.resolve(all);
      },
      get: (id) => (tabs.has(id) ? Promise.resolve(tabs.get(id)) : Promise.reject(new Error("no tab " + id))),
      create: (args) => {
        const tab = { id: nextTabId++, url: args.url, active: !!args.active, status: "complete", windowId: 1 };
        tabs.set(tab.id, tab);
        created.push(tab);
        if (args.active) { activeTabId = tab.id; activations.push(tab.id); }
        return Promise.resolve(tab);
      },
      update: (id, props) => {
        const tab = tabs.get(id);
        if (!tab) return Promise.reject(new Error("no tab " + id));
        if (props && props.active) { activeTabId = id; activations.push(id); }
        return Promise.resolve(tab);
      },
      remove: (id) => { tabs.delete(id); return Promise.resolve(); },
      sendMessage: (id, message) => {
        if (message.action === "ping") return Promise.resolve({ ok: true });
        if (message.action === "auto_scan") {
          scanMessages.push(message);
          return Promise.resolve({ ok: true, posts: [], cursor: { depthY: 1234, atBottom: false }, inspected: 7, steps: 2 });
        }
        return Promise.resolve({ ok: true });
      },
      onUpdated: { addListener() {}, removeListener() {} },
    },
  };

  const sandbox = {
    console: { log: (m) => logs.push(String(m)), error: (m) => logs.push("ERR " + m) },
    browser,
    // Short waits resolve instantly; long ones (the scan rate limit, timeouts) are
    // parked so the test can observe them without waiting a real minute.
    setTimeout: (fn, ms) => {
      const delay = Number(ms) || 0;
      if (delay >= PARK_MS) { parked.push({ fn, delay }); return -1; }
      setImmediate(fn);
      return 0;
    },
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    queueMicrotask: (fn) => setImmediate(fn),
    WebSocket: class {
      constructor(url) {
        this.url = url;
        this.readyState = 1;
        this.sent = [];
        sockets.push(this);
      }
      send(payload) { this.sent.push(JSON.parse(payload)); sent.push(JSON.parse(payload)); }
      close() { this.readyState = 3; }
    },
  };

  vm.runInContext(SOURCE, vm.createContext(sandbox), { filename: BACKGROUND_JS });

  const postsFoundCount = () => sent.filter((m) => m.action === "posts_found").length;
  function flushParked() { const timers = parked.splice(0, parked.length); for (const t of timers) t.fn(); }

  return {
    store, created, activations, scanMessages, sent, logs,
    activeTabId: () => activeTabId,
    postsFoundCount,
    parkedDelays: () => parked.map((t) => t.delay),
    flushParked,
    async init() {
      // The background opens its websocket a tick after load (it reads settings first).
      await waitFor("background to open its websocket", () => sockets.length > 0);
      return sockets[0];
    },
    // Ask the backend-style question "please scan" and wait for the answer. Parked
    // (rate-limit) waits are released as if the missing time had passed.
    async requestScan() {
      const before = postsFoundCount();
      sockets[0].onmessage({ data: JSON.stringify({ action: "scan" }) });
      for (let i = 0; i < 50 && postsFoundCount() === before; i++) {
        await tick();
        if (postsFoundCount() === before && parked.length) flushParked();
      }
      await waitFor("posts_found answer", () => postsFoundCount() > before);
      return sent.filter((m) => m.action === "posts_found").slice(-1)[0];
    },
    // Same, but without waiting for the (possibly rate-limited) answer.
    fireScan() { sockets[0].onmessage({ data: JSON.stringify({ action: "scan" }) }); },
  };
}

function wasActivated(world, tabId) { return world.activations.includes(tabId); }

async function main() {
  console.log("\n[scan-tabs] the source tab is kept open, shown for the scan, then the user's tab returns");
  {
    const w = createBackground();
    await w.init();
    const userTab = w.activeTabId();

    await w.requestScan();
    const source = w.created[0];
    const sourceTabId = source.id;
    check("opened exactly one dedicated source tab", w.created.length, 1);
    check("that tab points at the source group", source.url, SOURCE_URL);
    check("it was opened in the background (no focus stolen at startup)", source.active, false);
    check("the scan asked the source tab to scan", w.scanMessages.length, 1);
    check("it passed the configured max posts", w.scanMessages[0].maxPosts, 3);
    check("the source tab was made visible, then the user's tab restored", w.activations, [sourceTabId, userTab]);
    check("the user's tab is active again after the scan", w.activeTabId(), userTab);

    // Second scan: same tab (Facebook keeps the loaded feed + scroll position there).
    await w.requestScan();
    check("second scan opened no new tab (reused)", w.created.length, 1);
    check("second scan really scanned again", w.scanMessages.length, 2);
    check("the same tab was shown and the user's tab restored", w.activations, [sourceTabId, userTab, sourceTabId, userTab]);
    check("the user's tab is active again", w.activeTabId(), userTab);

    // The 60 s floor: a scan asked for right away is delayed, not run immediately and
    // not dropped.
    const beforeThird = w.postsFoundCount();
    w.fireScan();
    await tick();
    await tick();
    // The scan rate-limit wait: at least 30 s but capped at 150 s (the parked 240 s
    // entries are the per-request timeouts, which are unrelated).
    const waits = w.parkedDelays().filter((d) => d >= 30000 && d <= 150000);
    check("a too-soon scan did not run immediately", w.postsFoundCount(), beforeThird);
    check("it scheduled exactly one rate-limit wait", waits.length, 1);
    check("that wait honours the 60 s floor", waits[0] >= 59000 && waits[0] <= 60000, true);
    check("it logged the delay instead", w.logs.some((l) => l.indexOf("Scan asked for too soon") !== -1), true);

    w.flushParked();
    await waitFor("the delayed scan to run", () => w.postsFoundCount() > beforeThird);
    check("once the gap passed the scan ran (the request was not dropped)", w.postsFoundCount(), beforeThird + 1);
    check("and it still reused the same source tab", w.created.length, 1);
  }

  console.log("\n[scan-tabs] `activateSourceTabOnScan: false` never touches the user's tab");
  {
    const w = createBackground({ settings: { activateSourceTabOnScan: false } });
    await w.init();
    const userTab = w.activeTabId();
    await w.requestScan();
    check("no tab was activated at all", w.activations, []);
    check("the user's tab stayed active", w.activeTabId(), userTab);
    check("the scan still happened", w.scanMessages.length, 1);
  }

  console.log("\n[scan-tabs] " + (checks - failures) + "/" + checks + " checks passed\n");
  if (failures) {
    console.log("[scan-tabs] FAILED");
    process.exit(1);
  }
  console.log("[scan-tabs] ALL SCAN-TAB CHECKS PASSED");
  return 0;
}

main().then(
  () => process.exit(failures ? 1 : 0),
  (err) => { console.error("[scan-tabs] harness error: " + ((err && err.stack) || err)); process.exit(1); }
);
