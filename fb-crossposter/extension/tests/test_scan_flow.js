#!/usr/bin/env node
"use strict";
/**
 * Headless check of the source-feed scan in ../content.js — no Firefox, no Facebook.
 *
 * It runs the REAL extension script in a stubbed page and drives its "auto_scan"
 * handler against a fake group feed (newest post first; the next page is appended
 * once you scroll near the bottom, like Facebook's infinite scroll). It asserts the
 * behaviour that was broken:
 *
 *   1. a fresh scan starts at the top and returns the newest posts
 *   2. while the rendered posts are all known it keeps scrolling DOWN and picks up
 *      the posts below — instead of re-reading the same top posts forever
 *      (the "0 new posts found, it restarts from the top every time" bug)
 *   3. it consumes the whole feed in order, never reporting a post twice
 *   4. the next scan RESUMES at the saved depth instead of restarting at the top
 *   5. at the end of the feed the cursor is marked and the next cycle starts fresh
 *   6. a stale cursor, or one saved for another group, is discarded
 *
 * Run: node extension/tests/test_scan_flow.js      (exit code 0 = all checks passed)
 * CONTENT_JS_PATH=/path/to/other-content.js points the harness at another revision
 * (used to confirm these checks really do fail on the pre-9.3 scan).
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const CONTENT_JS = process.env.CONTENT_JS_PATH || path.join(__dirname, "..", "content.js");
const SOURCE = fs.readFileSync(CONTENT_JS, "utf8");

const POST_HEIGHT = 600;    // fake post height in px
const VIEWPORT = 800;       // fake window.innerHeight
const LOAD_BATCH = 6;       // how many older posts one "page" adds
const NEAR_BOTTOM = 200;    // Facebook appends the next page this close to the bottom

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

// ---------------------------------------------------------------------------
// Fake DOM (only what content.js touches on the scan path)
// ---------------------------------------------------------------------------
function makeTextNode(text) {
  return {
    textContent: text,
    querySelectorAll: () => [],
    cloneNode: () => ({ textContent: text, querySelectorAll: () => [] }),
  };
}

function makeStubElement() {
  return {
    nodeType: 1,
    offsetHeight: 0,
    offsetWidth: 0,
    parentElement: null,
    className: "",
    textContent: "",
    style: {},
    classList: { add() {}, remove() {} },
    hasAttribute: () => false,
    getAttribute: () => null,
    setAttribute() {},
    appendChild: (child) => child,
    addEventListener() {},
    contains: () => false,
    querySelector: () => null,
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ width: 0, height: 0, top: 0, bottom: 0, left: 0, right: 0 }),
  };
}

function makePostElement(post, index, world) {
  const attrs = { role: "article" };
  return {
    nodeType: 1,
    offsetHeight: POST_HEIGHT,
    offsetWidth: 600,
    className: "",
    parentElement: world.body,
    textContent: post.text,
    style: {},
    classList: { add() {}, remove() {} },
    hasAttribute(key) { return Object.prototype.hasOwnProperty.call(attrs, key); },
    getAttribute(key) { return Object.prototype.hasOwnProperty.call(attrs, key) ? attrs[key] : null; },
    setAttribute(key, value) { attrs[key] = String(value); },
    addEventListener() {},
    appendChild(child) { return child; },
    contains(other) { return other === this; },
    querySelector: () => null,
    querySelectorAll(selector) {
      if (selector === "[data-ad-rendering-role='story_message']") return [makeTextNode(post.text)];
      return [];
    },
    // content.js skips post elements that are above the viewport — that is how a scan
    // resumes past posts it already walked in earlier cycles.
    getBoundingClientRect() {
      const top = index * POST_HEIGHT - world.scrollY();
      return { top, bottom: top + POST_HEIGHT, left: 0, right: 600, width: 600, height: POST_HEIGHT };
    },
  };
}

// ---------------------------------------------------------------------------
// Fake page + feed
// ---------------------------------------------------------------------------
function post(index) {
  return { text: "Post " + (index + 1) + " — a plausible group post with enough text to extract" };
}

function createWorld(opts) {
  const posts = [];
  for (let i = 0; i < (opts.count || 20); i++) posts.push(post(i));

  let loaded = Math.min(opts.loaded || posts.length, posts.length);
  let y = 0;
  const scrollToCalls = [];
  const scrollByCalls = [];
  const store = Object.assign({}, opts.storage || {});
  const listeners = [];
  const logs = [];
  const elements = [];

  const world = {
    body: null,
    scrollY: () => y,
    posts,
    scrollToCalls,
    scrollByCalls,
    store,
    logs,
    scrollHeight: () => loaded * POST_HEIGHT,
  };
  const maxScroll = () => Math.max(0, world.scrollHeight() - VIEWPORT);

  // Infinite scroll: another page of posts arrives only when you get near the bottom
  // of what is already rendered. Re-scrolling over ground you have covered does NOT
  // produce new posts — that is exactly why the old "nudge from the top" scan starved.
  function maybeLoadMore() {
    if (loaded < posts.length && y + VIEWPORT >= world.scrollHeight() - NEAR_BOTTOM) {
      loaded = Math.min(posts.length, loaded + LOAD_BATCH);
    }
  }
  function elementAt(index) {
    if (!elements[index]) elements[index] = makePostElement(posts[index], index, world);
    return elements[index];
  }

  const body = makeStubElement();
  world.body = body;
  body.querySelectorAll = (selector) => {
    if (selector === "div[role='article']") {
      const out = [];
      for (let i = 0; i < loaded; i++) out.push(elementAt(i));
      return out;
    }
    return [];
  };

  const documentElement = makeStubElement();
  Object.defineProperty(documentElement, "scrollHeight", { get: () => world.scrollHeight() });

  const sandbox = {
    console: { log: (m) => logs.push(String(m)), error: (m) => logs.push("ERR " + m), warn: () => {} },
    document: {
      body,
      documentElement,
      contains: () => true,
      getElementById: () => null,
      createElement: () => makeStubElement(),
      createTreeWalker: () => ({ nextNode: () => false, currentNode: null }),
      querySelector: () => null,
      querySelectorAll: () => [],
    },
    location: { href: "https://www.facebook.com/groups/1144780033953810/", pathname: "/groups/1144780033953810/", search: "" },
    browser: {
      storage: {
        local: {
          get(key) {
            if (typeof key === "string") return Promise.resolve(key in store ? { [key]: store[key] } : {});
            return Promise.resolve(Object.assign({}, store));
          },
          set(obj) { Object.assign(store, obj); return Promise.resolve(); },
        },
      },
      runtime: { onMessage: { addListener: (fn) => listeners.push(fn) } },
    },
    setTimeout: (fn) => setImmediate(fn),
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    queueMicrotask: (fn) => setImmediate(fn),
    MutationObserver: class { observe() {} disconnect() {} },
    NodeFilter: { SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2 },
    getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
    fetch: () => Promise.reject(new Error("no network in tests")),
    XMLHttpRequest: class { open() {} send() {} },
    FileReader: class {},
    DataTransfer: class { constructor() { this.items = { add() {} }; this.files = []; } },
    File: class {},
    Event: class { constructor(type) { this.type = type; } },
    navigator: { clipboard: { writeText: () => Promise.resolve() } },
  };
  sandbox.window = sandbox;
  Object.defineProperty(sandbox, "scrollY", { get: () => y });
  sandbox.innerHeight = VIEWPORT;
  sandbox.scrollTo = (x, newY) => {
    scrollToCalls.push([x, newY]);
    y = Math.max(0, Math.min(newY, maxScroll()));
    maybeLoadMore();
  };
  sandbox.scrollBy = (x, dy) => {
    scrollByCalls.push([x, dy]);
    y = Math.max(0, Math.min(y + dy, maxScroll()));
    maybeLoadMore();
  };

  vm.runInContext(SOURCE, vm.createContext(sandbox), { filename: CONTENT_JS });

  world.scan = (maxPosts) => new Promise((resolve, reject) => {
    if (!listeners.length) return reject(new Error("content.js registered no message listener"));
    const before = scrollToCalls.length;
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) { settled = true; reject(new Error("auto_scan never answered")); }
    }, 20000);
    listeners[0]({ action: "auto_scan", maxPosts }, {}, (response) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      response = response || {};
      response.anchors = scrollToCalls.slice(before).map((call) => call[1]);
      resolve(response);
    });
  });

  world.textsOf = (response) => (response.posts || []).map((p) => p.text);
  world.indexesOf = (response) => world.textsOf(response).map((text) => posts.findIndex((p) => p.text === text));
  return world;
}

function newest(indexes) { return indexes.map((i) => post(i).text); }

// Tolerant readers: a scan that does not report a cursor fails the checks below
// (depth 0 / not at the end) instead of crashing the harness.
function depthOf(scan) { return (scan.cursor && scan.cursor.depthY) || 0; }
function atBottomOf(scan) { return !!(scan.cursor && scan.cursor.atBottom); }

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
async function main() {
  console.log("\n[scan] a fresh scan starts at the top and returns the newest posts");
  {
    const w = createWorld({ count: 40, loaded: 12 });
    const s1 = await w.scan(3);
    check("scan reports ok", s1.ok, true);
    check("returns the 3 newest posts", w.textsOf(s1), newest([0, 1, 2]));
    check("anchored to the top exactly once", s1.anchors, [0]);
  }

  console.log("\n[scan] it crawls the whole feed, in order, without repeating a post");
  {
    const w = createWorld({ count: 40, loaded: 12 });
    const scans = [];
    for (let i = 0; i < 30; i++) {
      const s = await w.scan(3);
      scans.push(s);
      if (atBottomOf(s)) break;
    }
    const order = scans.reduce((acc, s) => acc.concat(w.indexesOf(s)), []);

    let resumes = 0, missedResumes = 0;
    for (let k = 1; k < scans.length; k++) {
      const saved = depthOf(scans[k - 1]);
      if (saved <= 0) continue;
      resumes++;
      // A scan that has a depth to resume from anchors there — it does NOT restart.
      if (!scans[k].anchors.includes(saved)) missedResumes++;
    }

    check("it took several cycles to walk the feed (not one restart loop)", scans.length > 5 && scans.length < 30, true);
    check("every scan had a saved depth to resume from", resumes > 3, true);
    check("scans that failed to resume at the saved depth", missedResumes, 0);
    check("posts were harvested in feed order", order, order.slice().sort((a, b) => a - b));
    check("every post came back exactly once", new Set(order).size, order.length);
    check("the whole 40-post feed was covered", order.length, 40);
    check("it ended at the end of the feed", atBottomOf(scans[scans.length - 1]), true);
    check("first scan still returned the newest posts", w.indexesOf(scans[0]), [0, 1, 2]);

    // Once the feed has been walked, repeating the scan must find nothing (the old
    // scan reported the same posts over and over instead of reaching new ones).
    const after1 = await w.scan(3);
    const after2 = await w.scan(3);
    check("a later pass reports no already-seen post", [after1.posts.length, after2.posts.length], [0, 0]);
  }

  console.log("\n[scan] each cycle continues deeper instead of restarting");
  {
    const w = createWorld({ count: 60, loaded: 12 });
    const s1 = await w.scan(3);
    const s2 = await w.scan(3);
    const s3 = await w.scan(9);
    const s4 = await w.scan(3);
    check("first scan returns the newest 3", w.textsOf(s1), newest([0, 1, 2]));
    check("second scan returns the NEXT 3", w.textsOf(s2), newest([3, 4, 5]));
    check("third scan starts where the second stopped", w.indexesOf(s3)[0] > w.indexesOf(s2)[2], true);
    check("fourth scan starts below everything the third returned",
      w.indexesOf(s4)[0] > w.indexesOf(s3)[s3.posts.length - 1], true);
    const all = [].concat(w.textsOf(s1), w.textsOf(s2), w.textsOf(s3), w.textsOf(s4));
    check("nothing was ever reported twice", new Set(all).size, all.length);
  }

  console.log("\n[scan] the end of the feed is detected and a fresh pass starts");
  {
    const w = createWorld({ count: 8, loaded: 8 });
    const all8 = await w.scan(8);
    check("8 posts found on the first pass", all8.posts.length, 8);
    const end = await w.scan(3);
    check("no new posts at the end", end.posts.length, 0);
    check("feed end flagged", atBottomOf(end), true);
    check("depth reset for the next fresh pass", depthOf(end), 0);
    check("it really scrolled to find the end", end.steps > 0, true);

    const after = await w.scan(3);
    check("fresh pass only anchors back to the top", after.anchors, [0]);
    check("fresh pass walks the feed again", after.inspected >= 8, true);
    check("still nothing new (every post is already known)", after.posts.length, 0);
  }

  console.log("\n[scan] a cursor from another group, or a stale one, is discarded");
  {
    for (const [label, cursor] of [
      ["another group", { group: "999999", depthY: 9999, atBottom: false, savedAt: Date.now() }],
      ["stale (7h old)", { group: "1144780033953810", depthY: 9999, atBottom: false, savedAt: Date.now() - 7 * 60 * 60 * 1000 }],
    ]) {
      const w = createWorld({ count: 40, loaded: 12, storage: { scanCursor: cursor } });
      const r = await w.scan(3);
      check(label + ": ignored — newest posts returned", w.textsOf(r), newest([0, 1, 2]));
      check(label + ": never jumped to the saved depth", r.anchors.includes(9999), false);
    }
  }

  console.log("\n[scan] " + (checks - failures) + "/" + checks + " checks passed\n");
  if (failures) {
    console.log("[scan] FAILED");
    process.exit(1);
  }
  console.log("[scan] ALL SCAN CHECKS PASSED");
  return 0;
}

main().then(
  () => process.exit(failures ? 1 : 0),
  (err) => { console.error("[scan] harness error: " + ((err && err.stack) || err)); process.exit(1); }
);
