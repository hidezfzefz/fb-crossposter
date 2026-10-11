// background.js — FB Post Copier v9.3
// Coordinator: manual Copy/Add buttons, inventory, and the bridge to the optional
// Python backend (auto-farm + dashboard). Everything manual works WITHOUT Python.
//
// v9.3 — scanning works like a human browsing instead of a restart every cycle:
//   * ONE dedicated source tab is kept open for the farm's lifetime (closing it
//     throws away Facebook's loaded feed AND the scroll position)
//   * that tab is made VISIBLE for the scan (Firefox throttles background tabs and
//     Facebook's infinite scroll does not run in them), then the user's tab is restored
//   * scans are rate limited: at least minScanIntervalSeconds (never below 60) apart,
//     so a 7-second backend interval can no longer hammer the feed
//
// v9.1 — Post All hardening (the "posts one thing / posts twice" bugs):
//   * posting is single-flight — two Post All runs can never overlap
//   * inventory mutations are serialized (no lost updates resurrecting items)
//   * every item is re-checked against the live inventory and the dedup store
//     right before it is posted
//   * waitForTabLoaded no longer waits 30s when the page was already complete
//
// Hard-won rules kept here:
//   * target tabs are opened ACTIVE (visible) — Firefox throttles background tabs,
//     which breaks the 5ms-per-character typing in the composer.
//   * posting is SEQUENTIAL — parallel visible tabs would demote each other to
//     background tabs and the typing would fail.
//   * every auto_scan command gets a posts_found answer (even on error) so the
//     backend never waits forever.

(function () {
  "use strict";
  function log(m) { console.log("[FB Copier BG] " + m); }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  var DEFAULT_WS_URL = "ws://localhost:8765";
  var MIN_SCAN_GAP_SECONDS = 60;   // never scan the feed faster than this
  var SCAN_GAP_MAX_WAIT_MS = 150000; // stay inside the backend's 180s scan timeout
  var ws = null;
  var connected = false;
  var connecting = false;
  var scanBusy = false;
  var pythonStats = null;   // counters pushed by the Python backend
  var sourceTabId = null;   // the dedicated source-group tab (kept open on purpose)
  var lastScanStartedAt = 0;

  var defaults = {
    targetGroupUrl: "https://www.facebook.com/groups/1770877160904592",
    sourceGroupUrl: "https://www.facebook.com/groups/1144780033953810",
    scanMaxPosts: 12,
    maxPostsPerDay: 20,
    minSecondsBetweenPosts: 10,
    batchSize: 3,
    wsUrl: "ws://localhost:8765",
    pythonAutoMode: false,
    // Minimum gap between two source-feed scans (seconds, hard floor 60) and
    // whether the source tab is shown while scanning.
    minScanIntervalSeconds: 60,
    activateSourceTabOnScan: true,
  };

  function getSettings() {
    return browser.storage.local.get("settings").then(function (r) {
      return Object.assign({}, defaults, r.settings || {});
    });
  }
  function saveSettings(s) { return browser.storage.local.set({ settings: s }); }

  // ============================================================
  // SINGLE-FLIGHT QUEUES (prevent overlapping runs → no double posts)
  // ============================================================
  var inventoryQueue = Promise.resolve();
  // All inventory read-modify-write operations go through here.
  function inventoryLock(fn) {
    var result = inventoryQueue.then(function () { return fn(); });
    inventoryQueue = result.then(function () {}, function () {});
    return result;
  }

  var postQueue = Promise.resolve();
  var postQueueDepth = 0;
  var postAllRunning = false;
  // Every posting attempt (manual Copy, Post All, auto-farm) is serialized.
  function withPostQueue(fn) {
    postQueueDepth++;
    var result = postQueue.then(function () { return fn(); });
    function release() { postQueueDepth = Math.max(0, postQueueDepth - 1); }
    postQueue = result.then(release, release);
    return result;
  }

  // ============================================================
  // DEDUP (local, best effort — the Python DB hash is authoritative for auto-farm)
  // ============================================================
  // Content keys: a stable prefix of the normalized text + first image URL.
  // Two lengths are kept so dedup still matches entries written by older versions.
  function contentKeys(data) {
    var text = String(data.text || "")
      .toLowerCase()
      .replace(/\bsee more\b|\bshow more\b/g, "")
      .replace(/\s+/g, " ")
      .trim();
    var img = (data.images && data.images[0]) ? String(data.images[0]).split("?")[0] : "";
    if (!text && !img) return [];
    var keys = [(text.substring(0, 120) + "|" + img).substring(0, 220)];
    if (text.length > 120) keys.push((text.substring(0, 200) + "|" + img).substring(0, 300)); // legacy length
    return keys;
  }
  function contentKey(data) { var k = contentKeys(data); return k[0] || ""; }
  // Read every key out of a stored dedup entry (all historical shapes).
  function entryKeys(entry) {
    if (!entry) return [];
    if (typeof entry === "string") return [entry];
    if (Array.isArray(entry.keys)) return entry.keys;
    if (entry.key) return [entry.key];
    return [];
  }
  // Read every key out of an inventory item (old items only have `key`).
  function itemKeys(item) {
    if (!item) return [];
    if (Array.isArray(item.keys) && item.keys.length) return item.keys;
    if (item.key) return [item.key];
    return contentKeys(item);
  }

  async function getPostedEntries() {
    var r = await browser.storage.local.get("postedPermalinks");
    return r.postedPermalinks || [];
  }
  async function isDuplicateKeys(candidateKeys) {
    if (!candidateKeys || !candidateKeys.length) return false;
    var stored = await getPostedEntries();
    for (var entry of stored) {
      var keys = entryKeys(entry);
      for (var k of keys) { if (candidateKeys.indexOf(k) !== -1) return true; }
    }
    return false;
  }
  async function isDuplicate(data) { return await isDuplicateKeys(itemKeys(data)); }
  async function markAsPosted(data) {
    var keys = itemKeys(data);
    if (!keys.length) return;
    var stored = await getPostedEntries();
    if (await isDuplicateKeys(keys)) return;
    stored.push({ keys: keys, time: Date.now() });
    if (stored.length > 500) stored = stored.slice(-500);
    await browser.storage.local.set({ postedPermalinks: stored });
  }
  async function resetLocalDedup() {
    await browser.storage.local.set({ postedPermalinks: [], scanSeenKeys: [] });
    try {
      var tabs = await browser.tabs.query({ url: "*://*.facebook.com/*" });
      for (var t of tabs) {
        try { await browser.tabs.sendMessage(t.id, { action: "clear_scan_keys" }); } catch (e) {}
      }
    } catch (e) {}
    log("Local dedup history cleared");
  }

  // ============================================================
  // INVENTORY
  // ============================================================
  async function getInventory() { var r = await browser.storage.local.get("inventory"); return r.inventory || []; }
  async function saveInventory(inv) { await browser.storage.local.set({ inventory: inv }); updateBadge(inv.length); }

  function updateInventoryItem(id, patch) {
    return inventoryLock(async function () {
      var inv = await getInventory();
      for (var i = 0; i < inv.length; i++) { if (inv[i].id === id) { Object.assign(inv[i], patch); break; } }
      await saveInventory(inv);
    });
  }
  function addToInventory(postData) {
    return inventoryLock(async function () {
      var inv = await getInventory();
      var keys = contentKeys(postData);
      postData.keys = keys;
      postData.key = keys[0] || "";
      if (keys.length) {
        var exists = inv.some(function (p) {
          var pk = itemKeys(p);
          return pk.some(function (k) { return keys.indexOf(k) !== -1; });
        });
        if (exists) return { ok: true, alreadyExists: true, count: inv.length };
      } else if (postData.permalink && inv.some(function (p) { return p.permalink === postData.permalink; })) {
        return { ok: true, alreadyExists: true, count: inv.length };
      }
      postData.id = "post_" + Date.now() + "_" + Math.random().toString(36).substr(2, 9);
      inv.push(postData);
      await saveInventory(inv);
      return { ok: true, alreadyExists: false, count: inv.length };
    });
  }
  function removeFromInventory(id) {
    return inventoryLock(async function () {
      var inv = await getInventory();
      inv = inv.filter(function (p) { return p.id !== id; });
      await saveInventory(inv);
    });
  }
  function clearInventory() { return inventoryLock(function () { return saveInventory([]); }); }
  function updateBadge(count) {
    if (count > 0) { browser.browserAction.setBadgeText({ text: String(count) }); browser.browserAction.setBadgeBackgroundColor({ color: "#2563eb" }); }
    else browser.browserAction.setBadgeText({ text: "" });
  }
  getInventory().then(function (inv) { updateBadge(inv.length); });

  // ============================================================
  // HELPERS
  // ============================================================
  function withTimeout(promise, ms, label) {
    return new Promise(function (resolve, reject) {
      var done = false;
      var t = setTimeout(function () {
        if (!done) { done = true; reject(new Error(label + " timed out after " + Math.round(ms / 1000) + "s")); }
      }, ms);
      promise.then(
        function (v) { if (!done) { done = true; clearTimeout(t); resolve(v); } },
        function (e) { if (!done) { done = true; clearTimeout(t); reject(e); } }
      );
    });
  }

  // Firefox doesn't always inject content.js into newly created tabs — verify with a
  // ping and inject manually until the content script answers.
  async function ensureContentScript(tabId) {
    for (var i = 0; i < 6; i++) {
      try { var pong = await browser.tabs.sendMessage(tabId, { action: "ping" }); if (pong && pong.ok) return true; } catch (e) {}
      try { await browser.tabs.executeScript(tabId, { file: "content.js" }); } catch (e) { log("inject retry: " + e.message); }
      await sleep(1200);
    }
    return false;
  }

  function waitForTabLoaded(tabId) {
    return new Promise(function (resolve) {
      var settled = false;
      function finish() {
        if (settled) return;
        settled = true;
        browser.tabs.onUpdated.removeListener(listener);
        resolve();
      }
      var listener = function (updatedTabId, changeInfo) {
        if (updatedTabId === tabId && changeInfo.status === "complete") finish();
      };
      browser.tabs.onUpdated.addListener(listener);
      // The tab may ALREADY be complete (cached page) — without this check the
      // listener would never fire and every post would stall 30s.
      browser.tabs.get(tabId).then(function (t) { if (t && t.status === "complete") finish(); }, function () {});
      setTimeout(finish, 30000);
    });
  }
  function groupIdFromUrl(url) {
    var part = String(url || "").split("/groups/")[1] || "";
    return part.split("/")[0].split("?")[0];
  }

  // ============================================================
  // POST ONE POST (never needs Python)
  // ============================================================
  function postSinglePost(data) {
    return withPostQueue(function () { return postSinglePostNow(data); });
  }

  async function postSinglePostNow(data) {
    var settings = await getSettings();
    if (!settings.targetGroupUrl) return { ok: false, error: "No target group URL configured" };
    // Central duplicate guard — also protects the auto-farm path.
    if (await isDuplicate(data)) return { ok: false, error: "Duplicate — this post was already copied", duplicate: true };
    var tab = null;
    try {
      log("Posting: " + (data.text || "").substring(0, 40));
      // ACTIVE tab: Firefox throttles background tabs (setTimeout min 1000ms → typing fails).
      tab = await browser.tabs.create({ url: settings.targetGroupUrl, active: true });
      await waitForTabLoaded(tab.id);
      await sleep(3000);

      var ready = await ensureContentScript(tab.id);
      if (!ready) return { ok: false, error: "content script did not load in the target tab" };

      var response = await withTimeout(
        browser.tabs.sendMessage(tab.id, { action: "do_post", data: data }),
        240000, "do_post"
      );
      if (response && response.ok) await markAsPosted(data);
      return response || { ok: false, error: "No response from the target tab" };
    } catch (e) {
      return { ok: false, error: e.message };
    } finally {
      if (tab) { await sleep(500); try { await browser.tabs.remove(tab.id); } catch (e) {} }
    }
  }

  // ============================================================
  // MANUAL COPY (direct post — duplicate-protected)
  // ============================================================
  async function handleCopyPost(data) {
    if (await isDuplicate(data)) return { ok: false, error: "Already copied this post (duplicate)", duplicate: true };
    log("Manual copy: " + (data.text || "").substring(0, 40));
    return await postSinglePost(data);
  }

  // ============================================================
  // POST ALL FROM INVENTORY (sequential — see throttle note at the top)
  //  * single-flight: a second run is refused, never executed in parallel
  //  * works from a snapshot of IDs, re-checked against the live inventory
  //    and the dedup store before every single post
  // ============================================================
  async function postAllFromInventory() {
    if (postAllRunning) return { ok: false, error: "A posting run is already in progress — wait for it to finish" };
    postAllRunning = true;
    try {
      var inv = await getInventory();
      if (inv.length === 0) return { ok: false, error: "Inventory is empty" };
      var settings = await getSettings();
      var gap = Math.max(3, settings.minSecondsBetweenPosts || 3);
      var success = 0, failed = 0, skipped = 0, errors = [];
      var ids = inv.map(function (p) { return p.id; });

      for (var i = 0; i < ids.length; i++) {
        // Re-read the live inventory: the item may have been removed meanwhile.
        var current = await getInventory();
        var post = null;
        for (var j = 0; j < current.length; j++) { if (current[j].id === ids[i]) { post = current[j]; break; } }
        if (!post) { skipped++; continue; }

        if (await isDuplicate(post)) {   // already posted via another path
          await removeFromInventory(post.id);
          skipped++;
          continue;
        }

        log("Posting inventory item " + (i + 1) + "/" + ids.length);
        var result = await postSinglePost(post);
        if (result && result.ok) { success++; await removeFromInventory(post.id); }
        else {
          failed++;
          var err = (result && result.error) || "unknown error";
          errors.push({ text: (post.text || "").substring(0, 40), error: err });
          // Keep failures in the inventory for review. If the status is unknown
          // (composer never closed), verify on Facebook before retrying —
          // retrying a post that actually went through would duplicate it.
          await updateInventoryItem(post.id, { lastError: err, attempts: (post.attempts || 0) + 1 });
        }
        if (i < ids.length - 1) await sleep(gap * 1000);
      }
      return { ok: true, results: { success: success, failed: failed, skipped: skipped, errors: errors } };
    } finally {
      postAllRunning = false;
    }
  }

  // ============================================================
  // AUTO SCAN (Python backend drives this; also used by dashboard "Scan Now")
  // ============================================================
  // The source tab is reused, never closed: Facebook keeps the loaded feed and the
  // content script keeps its scroll cursor in that page. Recreating it every cycle
  // is what made every scan restart from the newest posts.
  async function findOrCreateSourceTab(url) {
    var groupId = groupIdFromUrl(url);
    if (sourceTabId !== null) {
      try {
        var known = await browser.tabs.get(sourceTabId);
        if (known && (!groupId || (known.url && known.url.indexOf(groupId) !== -1))) return known;
      } catch (e) { log("source tab is gone: " + e.message); }
      sourceTabId = null;
    }
    // Adopt an already-open tab on the same group (the user may be browsing it).
    var tabs = await browser.tabs.query({});
    if (groupId) {
      for (var t of tabs) {
        if (t.url && t.url.indexOf("facebook.com") !== -1 && t.url.indexOf(groupId) !== -1) { sourceTabId = t.id; return t; }
      }
    }
    // No tab for this group: open a dedicated one instead of hijacking an unrelated tab.
    var tab = await browser.tabs.create({ url: url, active: false });
    await waitForTabLoaded(tab.id);
    await sleep(3000);
    sourceTabId = tab.id;
    return tab;
  }

  // Firefox only runs Facebook's infinite scroll (and full-speed timers) in a tab that
  // is actually visible, so the source tab is activated for the scan and the tab the
  // user was on is restored afterwards.
  async function activateSourceTab(sourceTab) {
    try {
      var active = await browser.tabs.query({ active: true, currentWindow: true });
      var previous = (active && active[0]) || null;
      if (!previous || previous.id === sourceTab.id) return null;
      await browser.tabs.update(sourceTab.id, { active: true });
      await sleep(800);   // give the page a moment to be treated as visible
      return previous.id;
    } catch (e) { log("could not activate the source tab: " + e.message); return null; }
  }
  // Only hand focus back if the source tab is still the one that has it: a posting run
  // may have opened its own active target tab meanwhile, and stealing focus from a
  // composer mid-typing is exactly how posts get mangled.
  async function restoreTab(previousTabId, sourceTabIdToCheck) {
    if (previousTabId === null) return;
    try {
      var active = await browser.tabs.query({ active: true, currentWindow: true });
      if (!active || !active[0] || active[0].id !== sourceTabIdToCheck) return;
      await browser.tabs.get(previousTabId);
      await browser.tabs.update(previousTabId, { active: true });
    } catch (e) { log("could not restore the previous tab: " + e.message); }
  }

  async function doScan() {
    if (scanBusy) return { ok: false, busy: true, posts: [], error: "a scan is already running" };
    scanBusy = true;
    try {
      var settings = await getSettings();
      if (!settings.sourceGroupUrl) return { ok: false, posts: [], error: "No source group URL configured" };

      // Rate limit. Facebook's feed does not change every few seconds, so a scan
      // requested too early is DELAYED (not dropped) until the minimum gap has
      // passed — the backend still receives its posts_found answer.
      var gapMs = Math.max(MIN_SCAN_GAP_SECONDS, parseInt(settings.minScanIntervalSeconds, 10) || MIN_SCAN_GAP_SECONDS) * 1000;
      var waitMs = lastScanStartedAt ? gapMs - (Date.now() - lastScanStartedAt) : 0;
      if (waitMs > 0) {
        waitMs = Math.min(waitMs, SCAN_GAP_MAX_WAIT_MS);
        log("Scan asked for too soon — waiting " + Math.round(waitMs / 1000) + "s (minimum gap " + Math.round(gapMs / 1000) + "s)");
        await sleep(waitMs);
      }
      lastScanStartedAt = Date.now();

      var sourceTab = await findOrCreateSourceTab(settings.sourceGroupUrl);
      if (!sourceTab) return { ok: false, posts: [], error: "could not open the source group tab" };
      // Don't grab focus while a posting run is going on (see the throttle note above).
      var mayActivate = settings.activateSourceTabOnScan !== false && postQueueDepth === 0 && !postAllRunning;
      var restoreTabId = mayActivate ? await activateSourceTab(sourceTab) : null;
      try {
        var ready = await ensureContentScript(sourceTab.id);
        if (!ready) return { ok: false, posts: [], error: "content script did not load in the source tab" };
        var result = await withTimeout(
          browser.tabs.sendMessage(sourceTab.id, { action: "auto_scan", maxPosts: settings.scanMaxPosts || 12 }),
          240000, "auto_scan"
        );
        if (result && result.cursor) {
          log("Scan cursor is now at " + Math.round(result.cursor.depthY) + "px" + (result.cursor.atBottom ? " (end of the feed — next cycle restarts from the top)" : ""));
        }
        return result || { ok: false, posts: [], error: "No scan result" };
      } finally {
        await restoreTab(restoreTabId, sourceTab.id);
      }
    } catch (e) {
      return { ok: false, posts: [], error: e.message };
    } finally {
      scanBusy = false;
    }
  }

  // ============================================================
  // WEBSOCKET BRIDGE TO PYTHON (optional)
  // ============================================================
  function wsSend(obj) {
    if (ws && ws.readyState === 1) { try { ws.send(JSON.stringify(obj)); } catch (e) { log("ws send failed: " + e.message); } }
  }

  async function applyPythonSettings(s) {
    var cur = await getSettings();
    if (s.source_group_url) cur.sourceGroupUrl = s.source_group_url;
    if (s.target_group_url) cur.targetGroupUrl = s.target_group_url;
    if (s.batch_size) cur.batchSize = parseInt(s.batch_size, 10) || cur.batchSize;
    if (s.max_posts_per_day) cur.maxPostsPerDay = parseInt(s.max_posts_per_day, 10) || cur.maxPostsPerDay;
    if (s.cooldown_seconds) cur.minSecondsBetweenPosts = parseInt(s.cooldown_seconds, 10) || cur.minSecondsBetweenPosts;
    // The dashboard's scan interval is also the extension's minimum scan gap.
    var interval = parseInt(s.scan_interval_seconds, 10);
    if (interval > 0) cur.minScanIntervalSeconds = Math.max(MIN_SCAN_GAP_SECONDS, interval);
    cur.pythonAutoMode = String(s.auto_mode || "false").toLowerCase() === "true";
    await saveSettings(cur);
  }

  async function connect() {
    if (connecting) return;
    connecting = true;
    var settings = await getSettings();
    var url = settings.wsUrl || DEFAULT_WS_URL;
    log("Connecting to Python backend at " + url + " ...");
    try { ws = new WebSocket(url); }
    catch (e) { connecting = false; setTimeout(connect, 5000); return; }

    ws.onopen = async function () {
      connecting = false; connected = true;
      log("Connected to Python backend");
      var s = await getSettings();
      wsSend({
        action: "hello",
        settings: {
          sourceGroupUrl: s.sourceGroupUrl,
          targetGroupUrl: s.targetGroupUrl,
          batchSize: s.batchSize,
          maxPostsPerDay: s.maxPostsPerDay,
          minSecondsBetweenPosts: s.minSecondsBetweenPosts,
        },
      });
    };

    ws.onmessage = async function (event) {
      var msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      log("<- Python: " + msg.action);
      try {
        if (msg.action === "settings" && msg.settings) {
          await applyPythonSettings(msg.settings);
        } else if (msg.action === "scan") {
          var result = await doScan();
          // ALWAYS answer, even on error — the backend waits for this event.
          wsSend({
            action: "posts_found",
            posts: (result && result.posts) || [],
            error: (result && !result.ok) ? (result.error || "") : "",
          });
        } else if (msg.action === "do_post") {
          if (postAllRunning) {
            // A manual Post All run is in progress — tell the backend to retry
            // this post on the next cycle instead of stacking up behind it.
            wsSend({
              action: "post_result",
              id: msg.id || "",
              success: false,
              busy: true,
              error: "Manual Post All in progress",
            });
          } else {
            var pr;
            try { pr = await postSinglePost(msg.post); } catch (e) { pr = { ok: false, error: e.message }; }
            wsSend({
              action: "post_result",
              id: msg.id || "",
              success: !!(pr && pr.ok),
              error: (pr && pr.ok) ? "" : ((pr && pr.error) || "unknown error"),
            });
          }
        } else if (msg.action === "stats") {
          // Python pushes counters after every scan/post cycle and on connect.
          pythonStats = msg.stats || null;
        } else if (msg.action === "reset_dedup") {
          await resetLocalDedup();
        }
      } catch (e) {
        log("message handling failed: " + e.message);
      }
    };

    ws.onclose = function () {
      connected = false; connecting = false; pythonStats = null;
      log("Disconnected from Python backend. Reconnecting in 5s...");
      setTimeout(connect, 5000);
    };

    ws.onerror = function () { log("WebSocket error (Python backend not running?)"); };
  }

  // ============================================================
  // MESSAGE HANDLER (popup + content scripts)
  // ============================================================
  browser.runtime.onMessage.addListener(function (message, sender, sendResponse) {
    if (message.action === "copy_post") {
      handleCopyPost(message.data).then(sendResponse).catch(function (e) { sendResponse({ ok: false, error: e.message }); });
      return true;
    }
    if (message.action === "add_to_inventory") {
      addToInventory(message.data).then(sendResponse).catch(function (e) { sendResponse({ ok: false, error: e.message }); });
      return true;
    }
    if (message.action === "get_inventory") { getInventory().then(function (inv) { sendResponse({ ok: true, inventory: inv }); }); return true; }
    if (message.action === "remove_from_inventory") { removeFromInventory(message.id).then(function () { sendResponse({ ok: true }); }); return true; }
    if (message.action === "clear_inventory") { clearInventory().then(function () { sendResponse({ ok: true }); }); return true; }
    if (message.action === "post_all_inventory") {
      postAllFromInventory().then(sendResponse).catch(function (e) { sendResponse({ ok: false, error: e.message }); });
      return true;
    }
    if (message.action === "get_settings") { getSettings().then(function (s) { sendResponse({ ok: true, settings: s }); }); return true; }
    if (message.action === "save_settings") {
      (async function () {
        var cur = await getSettings();
        var merged = Object.assign(cur, message.settings || {});
        var urlChanged = !!(message.settings && message.settings.wsUrl && message.settings.wsUrl !== cur.wsUrl);
        await saveSettings(merged);
        if (urlChanged) {
          // Reconnect to the new backend URL (onclose schedules the reconnect).
          try { if (ws) ws.close(); } catch (e) {}
        } else {
          // Keep the Python dashboard in sync with popup edits (if connected).
          var push = { source_group_url: merged.sourceGroupUrl, target_group_url: merged.targetGroupUrl };
          if (merged.minScanIntervalSeconds) push.scan_interval_seconds = merged.minScanIntervalSeconds;
          wsSend({ action: "settings_update", settings: push });
        }
        sendResponse({ ok: true });
      })();
      return true;
    }
    if (message.action === "get_status") {
      (async function () {
        var s = await getSettings();
        sendResponse({
          ok: true,
          connected: connected,
          autoMode: !!s.pythonAutoMode,
          posting: postQueueDepth > 0 || postAllRunning,
          stats: pythonStats,
        });
      })();
      return true;
    }
    if (message.action === "reset_dedup") {
      (async function () {
        await resetLocalDedup();
        if (connected) wsSend({ action: "reset_dedup" });  // also clear the server DB
        sendResponse({ ok: true });
      })();
      return true;
    }
  });

  // Start connecting to Python (optional — manual mode works without it)
  connect();
  log("FB Post Copier v9.3 loaded (manual mode works without Python)");
})();
