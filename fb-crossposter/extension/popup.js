// popup.js — FB Post Copier v9.3
(function () {
  var MIN_SCAN_INTERVAL = 60;      // hard floor: Facebook's feed does not change every few seconds
  var MAX_SCAN_INTERVAL = 86400;
  var defaults = {
    targetGroupUrl: "https://www.facebook.com/groups/1770877160904592",
    sourceGroupUrl: "https://www.facebook.com/groups/1144780033953810",
    wsUrl: "ws://localhost:8765",
    minScanIntervalSeconds: MIN_SCAN_INTERVAL,
    activateSourceTabOnScan: true,
  };
  var invCount = 0;
  var bgPosting = false;   // reported by the background (a posting run is active)
  var clickBusy = false;   // this popup is waiting for a response right now

  browser.storage.local.get("settings").then(function(r) {
    var s = Object.assign({}, defaults, r.settings || {});
    document.getElementById("sourceUrl").value = s.sourceGroupUrl;
    document.getElementById("targetUrl").value = s.targetGroupUrl;
    document.getElementById("wsUrl").value = s.wsUrl || defaults.wsUrl;
    document.getElementById("scanInterval").value = s.minScanIntervalSeconds || defaults.minScanIntervalSeconds;
    document.getElementById("activateOnScan").checked = s.activateSourceTabOnScan !== false;
  });
  refreshInv(); refreshConn(); renderPostAllBtn();

  document.getElementById("save").addEventListener("click", function() {
    var raw = parseInt(document.getElementById("scanInterval").value, 10);
    var interval = Math.min(MAX_SCAN_INTERVAL, Math.max(MIN_SCAN_INTERVAL, raw || MIN_SCAN_INTERVAL));
    var clamped = interval !== raw;
    document.getElementById("scanInterval").value = interval;
    var s = {
      sourceGroupUrl: document.getElementById("sourceUrl").value.trim(),
      targetGroupUrl: document.getElementById("targetUrl").value.trim(),
      wsUrl: document.getElementById("wsUrl").value.trim() || defaults.wsUrl,
      minScanIntervalSeconds: interval,
      activateSourceTabOnScan: document.getElementById("activateOnScan").checked,
    };
    browser.runtime.sendMessage({ action: "save_settings", settings: s }).then(function(r) {
      showStatus(r && r.ok ? (clamped ? "✅ Saved (interval raised to " + interval + "s)" : "✅ Saved!") : "❌ Save failed");
    });
  });

  document.getElementById("resetDedup").addEventListener("click", async function() {
    var r = await browser.runtime.sendMessage({ action: "reset_dedup" });
    showStatus(r && r.ok ? "♻ Dedup history cleared" : "❌ Reset failed");
  });

  document.getElementById("postAll").addEventListener("click", async function() {
    if (clickBusy || bgPosting) return;
    clickBusy = true; renderPostAllBtn();
    try {
      var r = await browser.runtime.sendMessage({ action: "post_all_inventory" });
      if (r && r.ok) {
        var res = r.results || {};
        var msg = "✅ " + (res.success || 0) + " posted, " + (res.failed || 0) + " failed" + (res.skipped ? ", " + res.skipped + " skipped" : "");
        if (res.errors && res.errors.length) msg += " — first error: " + res.errors[0].error;
        showStatus(msg);
      } else showStatus("❌ " + (r ? r.error : "failed"));
    } finally {
      clickBusy = false;
      refreshInv();
      renderPostAllBtn();
    }
  });

  document.getElementById("clearInv").addEventListener("click", async function() {
    await browser.runtime.sendMessage({ action: "clear_inventory" }); refreshInv();
  });

  function renderPostAllBtn() {
    var btn = document.getElementById("postAll");
    if (!btn) return;
    if (bgPosting || clickBusy) { btn.disabled = true; btn.textContent = "⏳ Posting..."; }
    else { btn.disabled = false; btn.textContent = "📤 Post All (" + invCount + ")"; }
  }

  function refreshInv() {
    browser.runtime.sendMessage({ action: "get_inventory" }).then(function(r) {
      if (!r || !r.ok) return; var inv = r.inventory || [];
      invCount = inv.length;
      renderPostAllBtn();
      var el = document.getElementById("invList");
      if (inv.length === 0) { el.innerHTML = '<div class="empty">Click "➕ Add" on any post to queue it.</div>'; return; }
      el.innerHTML = "";
      for (var p of inv) { (function(post) {
        var i = document.createElement("div"); i.className = "inv-item";
        var t = document.createElement("div"); t.className = "inv-text";
        t.textContent = (post.text || "").substring(0, 50) || (post.images ? "[" + post.images.length + " image(s)]" : "(no text)");
        if (post.lastError) { t.textContent = "⚠ " + t.textContent; t.title = "Last error: " + post.lastError; }
        var b = document.createElement("button"); b.className = "inv-remove"; b.textContent = "❌";
        b.addEventListener("click", async function() { await browser.runtime.sendMessage({ action: "remove_from_inventory", id: post.id }); refreshInv(); });
        i.appendChild(t); i.appendChild(b); el.appendChild(i);
      })(p); }
    });
  }

  function refreshConn() {
    browser.runtime.sendMessage({ action: "get_status" }).then(function(r) {
      var el = document.getElementById("connStatus");
      if (r && r.connected) {
        el.className = "conn yes";
        el.textContent = r.autoMode ? "🟢 Connected — auto farm running" : "🟢 Connected — farm is stopped";
      } else {
        el.className = "conn no";
        el.textContent = "🔴 Python backend not connected (optional)";
      }
      var statsEl = document.getElementById("pyStats");
      if (r && r.connected && r.stats) {
        statsEl.textContent = "📊 posted today " + (r.stats.posted_today || 0) +
          " • total " + (r.stats.posts_total || 0) +
          " • queue " + (r.stats.new_queue || 0) +
          " • failed " + (r.stats.failed_total || 0);
        // Adaptive cadence: the farm slows down on a quiet feed and speeds back up
        // as soon as posts appear, so show what it is doing right now.
        var cadence = r.stats.scan_interval_current || 0;
        if (cadence) {
          var base = r.stats.scan_interval_seconds || 0;
          statsEl.textContent += " • scan every " + cadence + "s" + (base && cadence > base ? " (idle backoff)" : "");
        }
      } else {
        statsEl.textContent = "";
      }
      bgPosting = !!(r && r.posting);
      renderPostAllBtn();
    });
    setTimeout(refreshConn, 3000);
  }

  function showStatus(t) { var e = document.getElementById("status"); e.textContent = t; setTimeout(function() { e.textContent = ""; }, 6000); }
})();
