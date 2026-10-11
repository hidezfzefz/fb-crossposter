// content.js — FB Post Copier v9.1
// Runs on every facebook.com page.
//   * Manual mode: adds "+ Copy" / "+ Add" buttons to posts.
//   * Automation: answers "auto_scan" (incremental, cursor-style) and "do_post"
//     (composer flow) messages from the background script / Python backend.
//
// CRITICAL fixes kept from earlier iterations (do not remove):
//   * double-injection guard — otherwise onMessage registers twice → same post posted twice
//   * emojis are <img alt="🚀"> tags — replace with alt text during extraction
//   * only "scontent" URLs are real photos (static.xx.fbcdn.net = emojis/icons)
//   * isEmojiOrIcon scans with document.body (a bare postEl reference crashed image extraction)
//   * click "See more" before extracting hidden content
(function () {
  "use strict";
  if (window.__fbCopierLoaded) { console.log("[FB Copier] Already loaded, skipping"); return; }
  window.__fbCopierLoaded = true;

  var COPIER_BTN_CLASS = "fb-copier-btn";
  var COPIER_POST_CLASS = "fb-copier-post";
  var PROCESSED_ATTR = "data-fb-copier-processed";
  var MAX_SEEN_KEYS = 1000;   // local cursor memory (bounded)
  var MAX_WALK = 45;          // post elements inspected per scan
  var settings = { includeImages: true, includeLink: true };

  // ------------------------------------------------------------------
  // State
  // ------------------------------------------------------------------
  var seenKeys = new Set();   // posts already sent to the backend from this browser
  var scanRunning = false;    // re-entrancy guard (two scans must never overlap)
  var composerModal = null;   // the modal we opened, so completion checks are reliable
  var postingInThisTab = false; // re-entrancy guard (one do_post at a time per tab)

  function log(m) { console.log("[FB Copier] " + m); }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function loadSettings() {
    try {
      browser.storage.local.get("settings").then(function (r) {
        if (r.settings) settings = Object.assign({}, settings, r.settings);
      });
    } catch (e) {}
  }
  function loadSeenKeys() {
    try {
      browser.storage.local.get("scanSeenKeys").then(function (r) {
        if (r.scanSeenKeys) { for (var k of r.scanSeenKeys) seenKeys.add(k); }
      });
    } catch (e) {}
  }
  function saveSeenKeys() {
    try {
      var arr = Array.from(seenKeys);
      if (arr.length > MAX_SEEN_KEYS) arr = arr.slice(arr.length - MAX_SEEN_KEYS);
      seenKeys = new Set(arr);
      browser.storage.local.set({ scanSeenKeys: arr });
    } catch (e) {}
  }
  // Local content key — same idea as the backend hash (normalized text + first image URL).
  // Only used to avoid re-sending posts we already handled; the Python hash is authoritative.
  function contentKey(pd) {
    var text = String(pd.text || "").toLowerCase().replace(/\bsee more\b|\bshow more\b/g, "").replace(/\s+/g, " ").trim();
    var img = (pd.images && pd.images[0]) ? String(pd.images[0]).split("?")[0] : "";
    if (!text && !img) return "";
    return (text.substring(0, 120) + "|" + img).substring(0, 220);
  }
  loadSettings();
  loadSeenKeys();

  // ------------------------------------------------------------------
  // On-page status toast
  // ------------------------------------------------------------------
  function showStatus(text, color) {
    color = color || "#16a34a";
    var o = document.getElementById("fb-copier-status");
    if (!o) {
      o = document.createElement("div");
      o.id = "fb-copier-status";
      o.style.cssText = "position:fixed;top:20px;right:20px;z-index:999999;padding:16px 24px;border-radius:12px;font-size:16px;font-weight:600;color:white;max-width:400px;box-shadow:0 4px 20px rgba(0,0,0,0.3);font-family:sans-serif;transition:opacity 0.3s;";
      document.body.appendChild(o);
    }
    o.style.background = color; o.textContent = text; o.style.opacity = "1";
  }
  function hideStatus() { var o = document.getElementById("fb-copier-status"); if (o) o.style.opacity = "0"; }

  // ------------------------------------------------------------------
  // Add buttons to posts
  // ------------------------------------------------------------------
  var observer = new MutationObserver(function (mutations) {
    for (var m of mutations) { for (var n of m.addedNodes) { if (n.nodeType === 1) addCopyButtonsToPosts(n); } }
  });
  observer.observe(document.body, { childList: true, subtree: true });
  setTimeout(function () { addCopyButtonsToPosts(document.body); }, 500);
  setTimeout(function () { addCopyButtonsToPosts(document.body); }, 2000);

  function addCopyButtonsToPosts(root) {
    var posts = findPosts(root);
    for (var p of posts) { if (!p.hasAttribute(PROCESSED_ATTR)) addCopyButtonToPost(p); }
  }

  function findPosts(root) {
    var allArticles = root.querySelectorAll ? Array.from(root.querySelectorAll("div[role='article']")) : [];
    var topArticles = allArticles.filter(function (el) {
      if (el.getAttribute("aria-label") === "Comment") return false;
      if (el.offsetHeight < 100) return false;
      var parent = el.parentElement;
      while (parent && parent !== document.body) { if (parent.getAttribute && parent.getAttribute("role") === "article") return false; parent = parent.parentElement; }
      return true;
    });
    var storyMsgs = root.querySelectorAll ? Array.from(root.querySelectorAll("[data-ad-rendering-role='story_message']")) : [];
    for (var el of storyMsgs) {
      var node = el;
      for (var i = 0; i < 8 && node; i++) {
        node = node.parentElement; if (!node) break;
        if (node.getAttribute && node.getAttribute("role") === "article" && node.offsetHeight > 150) { if (!topArticles.includes(node)) topArticles.push(node); break; }
        if (i === 7 && node.offsetHeight > 150 && !topArticles.includes(node)) topArticles.push(node);
      }
    }
    var final = [];
    for (var a of topArticles) { var isChild = false; for (var b of topArticles) { if (a !== b && b.contains(a)) { isChild = true; break; } } if (!isChild) final.push(a); }
    return final;
  }

  function addCopyButtonToPost(postEl) {
    postEl.setAttribute(PROCESSED_ATTR, "true"); postEl.classList.add(COPIER_POST_CLASS);
    var c = document.createElement("div"); c.className = "fb-copier-btn-container";
    var cb = document.createElement("button"); cb.className = COPIER_BTN_CLASS; cb.textContent = "📋 Copy";
    cb.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); handleCopyClick(postEl, cb); });
    var ab = document.createElement("button"); ab.className = COPIER_BTN_CLASS + " add-btn"; ab.textContent = "➕ Add";
    ab.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); handleAddClick(postEl, ab); });
    c.appendChild(cb); c.appendChild(ab); postEl.appendChild(c);
  }

  // ------------------------------------------------------------------
  // Manual buttons
  // ------------------------------------------------------------------
  async function handleAddClick(postEl, btn) {
    btn.textContent = "⏳"; btn.style.background = "#ca8a04"; showStatus("📋 Extracting...", "#ca8a04");
    try {
      await clickSeeMore(postEl);
      var pd = extractPostData(postEl);
      if (!pd.text && pd.images.length === 0 && !pd.link) throw new Error("No content");
      if (pd.images.length > 0) { showStatus("📥 Images...", "#ca8a04"); pd.imagesBase64 = await downloadImagesAsBase64(pd.images); } else pd.imagesBase64 = [];
      var r = await browser.runtime.sendMessage({ action: "add_to_inventory", data: pd });
      if (r && r.ok) { if (r.alreadyExists) { btn.textContent = "✅ Exists"; } else { btn.textContent = "✅ Added (" + r.count + ")"; showStatus("✅ Added! (" + r.count + ")", "#16a34a"); } }
      else throw new Error(r ? r.error : "No response");
      setTimeout(function () { btn.textContent = "➕ Add"; btn.style.background = "#2563eb"; hideStatus(); }, 2000);
    } catch (e) {
      btn.textContent = "❌"; btn.style.background = "#dc2626"; showStatus("❌ " + e.message, "#dc2626");
      setTimeout(function () { btn.textContent = "➕ Add"; btn.style.background = "#2563eb"; hideStatus(); }, 4000);
    }
  }

  async function handleCopyClick(postEl, btn) {
    btn.className = COPIER_BTN_CLASS + " copying"; btn.textContent = "⏳"; showStatus("📋 Extracting...", "#ca8a04");
    try {
      await clickSeeMore(postEl);
      var pd = extractPostData(postEl);
      if (!pd.text && pd.images.length === 0 && !pd.link) throw new Error("No content");
      if (pd.images.length > 0) { showStatus("📥 Images...", "#ca8a04"); pd.imagesBase64 = await downloadImagesAsBase64(pd.images); } else pd.imagesBase64 = [];
      showStatus("🌐 Posting...", "#2563eb"); btn.textContent = "⏳";
      var r = await browser.runtime.sendMessage({ action: "copy_post", data: pd });
      if (r && r.ok) { btn.textContent = "✅"; showStatus("✅ Posted!", "#16a34a"); }
      else throw new Error(r ? r.error : "No response");
      setTimeout(function () { btn.className = COPIER_BTN_CLASS; btn.textContent = "📋 Copy"; hideStatus(); }, 3000);
    } catch (e) {
      btn.className = COPIER_BTN_CLASS + " error"; btn.textContent = "❌"; showStatus("❌ " + e.message, "#dc2626");
      setTimeout(function () { btn.className = COPIER_BTN_CLASS; btn.textContent = "📋 Copy"; hideStatus(); }, 5000);
    }
  }

  async function clickSeeMore(postEl) {
    var btns = postEl.querySelectorAll("div[role='button'], span[role='button'], a[role='button']");
    for (var b of btns) { var t = (b.textContent || "").trim().toLowerCase(); if ((t.includes("see more") || t.includes("show more")) && t.length < 30) { b.click(); await sleep(800); } }
  }

  // ------------------------------------------------------------------
  // Extraction (with emoji support)
  // ------------------------------------------------------------------
  function extractPostData(postEl) {
    return {
      text: extractTextWithEmojis(postEl),
      images: settings.includeImages ? extractImages(postEl) : [],
      link: settings.includeLink ? extractLink(postEl) : "",
      permalink: extractPermalink(postEl),
      author: extractAuthor(postEl),
      imagesBase64: [],
    };
  }

  function extractTextWithEmojis(postEl) {
    var sels = ["[data-ad-rendering-role='story_message']", "[data-ad-comet-preview='message']", "div[data-ad-preview='message']", "div[data-testid='post_message']", "div[data-contents='true']"];
    for (var s of sels) { var els = postEl.querySelectorAll(s); for (var el of els) { var t = getTextWithEmojis(el); if (isValidPostText(t) && t.length > 5) return t; } }
    var best = ""; var auto = postEl.querySelectorAll("div[dir='auto'], span[dir='auto']");
    for (var el of auto) { var t = getTextWithEmojis(el); if (isValidPostText(t) && t.length > best.length) best = t; }
    if (best.length > 10) return best;
    var skip = ["BUTTON", "A", "TIME", "ABBR", "SVG"]; var col = "";
    var w = document.createTreeWalker(postEl, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        var p = n.parentElement;
        while (p && p !== postEl) { if (skip.includes(p.tagName)) return NodeFilter.FILTER_REJECT; if (p.getAttribute && p.getAttribute("role") === "button") return NodeFilter.FILTER_REJECT; p = p.parentElement; }
        var t = (n.textContent || "").trim(); if (t.length < 2) return NodeFilter.FILTER_REJECT; if (isValidPostText(t)) return NodeFilter.FILTER_ACCEPT; return NodeFilter.FILTER_REJECT;
      },
    });
    while (w.nextNode()) col += (w.currentNode.textContent || "").trim() + " ";
    var emojiImgs = postEl.querySelectorAll("img[alt]");
    for (var img of emojiImgs) { var alt = img.getAttribute("alt") || ""; if (alt.length <= 4 && alt.length > 0) col += alt + " "; }
    col = col.trim(); return col.length > 10 ? col.substring(0, 5000) : "";
  }

  // Emojis are <img alt="🚀"> — replace them with their text before reading textContent.
  function getTextWithEmojis(el) {
    var clone = el.cloneNode(true);
    var imgs = clone.querySelectorAll("img");
    for (var img of imgs) { var alt = img.getAttribute("alt") || ""; if (alt.length <= 10) { img.parentNode.replaceChild(document.createTextNode(alt), img); } }
    return (clone.textContent || "").trim();
  }

  function isValidPostText(t) {
    if (!t || t.length < 2) return false;
    if (/^(Like|Comment|Share|Reply|More|Send|Post|Edit|See more|Show more|Hide|Report|Not now|Cancel|Close|Continue|OK|Done|Public|Friends|Members|Join|Leave|Follow|Unfollow|Liked|Shared|Commented|Reacted|Posted)$/i.test(t)) return false;
    if (/^(\d+[mhdy]\s?)+$/.test(t)) return false; if (/^just now$/i.test(t)) return false; if (t.length > 10000) return false; return true;
  }

  // URL-only emoji/icon test (usable for <img>, <source srcset> and background-image).
  function isEmojiSrc(src) {
    if (!src) return true;
    if (src.includes("static.xx.fbcdn")) return true;
    if (src.includes("/images/emoji.php")) return true;
    if (src.includes("/images/assets/")) return true;
    if (src.includes("emoji")) return true;
    if (src.includes("icon")) return true;
    if (src.includes("sticker")) return true;
    if (src.includes("/images/")) return true;
    return false;
  }

  function isEmojiOrIcon(img, src) {
    if (isEmojiSrc(src)) return true;
    var alt = (img.getAttribute("alt") || "").toLowerCase(); if (alt.includes("emoji")) return true;
    var cn = img.className || ""; if (cn.includes("emoji") || cn.includes("icon")) return true;
    var w = img.offsetWidth, h = img.offsetHeight; if (w > 0 && h > 0 && w < 80 && h < 80) return true;
    // NOTE: scan from document.body — postEl is NOT in scope here (old crash).
    var p = img.parentElement;
    while (p && p !== document.body) {
      var pc = p.className || ""; var pa = (p.getAttribute("aria-label") || "").toLowerCase();
      if (pc.includes("reaction") || pa.includes("reaction")) return true;
      if (pa.includes("write a comment")) return true; if (pa.includes("like")) return true;
      p = p.parentElement;
    }
    return false;
  }

  function extractImages(postEl) {
    var images = [], seen = new Set();
    function push(src) {
      if (!src || !src.includes("scontent")) return;
      if (isEmojiSrc(src)) return;
      if (seen.has(src)) return;
      seen.add(src); images.push(src);
    }

    var imgs = postEl.querySelectorAll("img");
    for (var img of imgs) {
      var src = img.getAttribute("src") || img.getAttribute("data-src") || img.getAttribute("data-orig-src") || "";
      if (!src || seen.has(src) || !src.includes("scontent")) continue;
      if (isEmojiOrIcon(img, src)) continue;
      var p = img.parentElement, isPP = false;
      while (p && p !== document.body) { var pc = p.className || ""; if (pc.includes("profile") || pc.includes("avatar")) { isPP = true; break; } p = p.parentElement; }
      if (isPP) continue;
      push(src);
    }

    // Lazy-loaded <picture><source srcset="...">
    var sources = postEl.querySelectorAll("source[srcset]");
    for (var s of sources) {
      var set = s.getAttribute("srcset") || "";
      var first = set.split(",")[0].trim().split(" ")[0];
      push(first);
    }

    // Background-image photos
    var bgs = postEl.querySelectorAll("div[style*='background-image'], span[style*='background-image']");
    for (var d of bgs) {
      var m = /url\(["']?(.*?)["']?\)/.exec(d.getAttribute("style") || "");
      if (m && m[1]) push(m[1]);
    }

    // Fallback: photo links
    if (images.length === 0) {
      var pl = postEl.querySelectorAll("a[href*='photo.php'], a[href*='/photo/'], a[href*='/photos/']");
      for (var link of pl) {
        var img = link.querySelector("img");
        if (img) {
          var src = img.getAttribute("src") || img.getAttribute("data-src") || "";
          if (src && !seen.has(src) && src.includes("scontent") && !isEmojiOrIcon(img, src)) { seen.add(src); images.push(src); }
        }
      }
    }
    return images;
  }

  function extractLink(postEl) {
    var anchors = postEl.querySelectorAll("a[href]");
    for (var a of anchors) {
      var href = a.getAttribute("href") || "";
      if (href.includes("l.php")) { var m = href.match(/[?&]u=([^&]+)/); if (m) href = decodeURIComponent(m[1]); }
      if (!href || href.startsWith("/") || href.includes("facebook.com")) continue;
      if (href.startsWith("http") && !href.includes("akamai") && !href.includes("fbcdn")) return href;
    }
    return "";
  }
  function extractPermalink(postEl) {
    var anchors = postEl.querySelectorAll("a[href]");
    for (var a of anchors) {
      var href = a.getAttribute("href") || "";
      if (href.includes("/permalink/") || href.includes("/posts/") || href.includes("story_fbid=")) return href.startsWith("/") ? "https://www.facebook.com" + href : href;
    }
    return "";
  }
  function extractAuthor(postEl) {
    var pe = postEl.querySelector("[data-ad-rendering-role='profile_name']");
    if (pe) { var t = (pe.textContent || "").trim(); if (t && t.length < 50) return t; }
    var links = postEl.querySelectorAll("a[role='link'], a[href*='/user/']");
    for (var a of links) {
      var t = (a.textContent || "").trim();
      if (t && t.length > 1 && t.length < 50 && !t.includes("http")) {
        if (!t.match(/^(Like|Comment|Share|Reply|More|Send|Follow|Join|Leave|Admin|Author)$/i)) return t;
      }
    }
    return "";
  }

  // ------------------------------------------------------------------
  // Image download as base64 (same-origin credentials; CORS-safe)
  // ------------------------------------------------------------------
  async function downloadImagesAsBase64(urls) {
    var results = [];
    for (var i = 0; i < urls.length && i < 10; i++) {
      try {
        var blob;
        try { var r = await fetch(urls[i], { credentials: "include" }); if (!r.ok) throw new Error("HTTP " + r.status); blob = await r.blob(); }
        catch (fe) { blob = await downloadViaXHR(urls[i]); }
        if (!blob || blob.size < 100) throw new Error("Too small");
        var b64 = await blobToBase64(blob);
        results.push({ data: b64, type: blob.type || "image/jpeg", name: "image_" + i + (blob.type.includes("png") ? ".png" : ".jpg") });
      } catch (e) { log("Download failed: " + e.message); }
    }
    return results;
  }
  function downloadViaXHR(url) {
    return new Promise(function (resolve, reject) {
      var x = new XMLHttpRequest();
      x.open("GET", url, true); x.responseType = "blob"; x.withCredentials = true;
      x.onload = function () { if (x.status === 200) resolve(x.response); else reject(new Error("XHR " + x.status)); };
      x.onerror = function () { reject(new Error("XHR error")); };
      x.timeout = 15000; x.ontimeout = function () { reject(new Error("XHR timeout")); };
      x.send();
    });
  }
  function blobToBase64(blob) {
    return new Promise(function (resolve, reject) {
      var r = new FileReader();
      r.onloadend = function () { resolve(r.result); };
      r.onerror = reject;
      r.readAsDataURL(blob);
    });
  }

  // ------------------------------------------------------------------
  // Message handler (background / Python bridge)
  // ------------------------------------------------------------------
  browser.runtime.onMessage.addListener(function (message, sender, sendResponse) {
    if (message.action === "do_post") {
      doPost(message.data).then(sendResponse).catch(function (e) { sendResponse({ ok: false, error: e.message }); });
      return true;
    }
    if (message.action === "ping") { sendResponse({ ok: true, url: window.location.href }); return false; }
    if (message.action === "auto_scan") {
      autoScan(message.maxPosts || 12).then(sendResponse).catch(function (e) { sendResponse({ ok: false, posts: [], error: e.message }); });
      return true;
    }
    if (message.action === "clear_scan_keys") {
      seenKeys = new Set();
      try { browser.storage.local.set({ scanSeenKeys: [] }); } catch (e) {}
      log("Scan cursor cleared");
      sendResponse({ ok: true });
      return false;
    }
  });

  // ------------------------------------------------------------------
  // Incremental auto scan (cursor-style)
  //  - re-anchors to the top so we always see the newest posts
  //  - skips posts already sent (local cursor + backend hash dedup)
  //  - only downloads images for NEW posts
  //  - never runs twice at the same time
  // ------------------------------------------------------------------
  async function autoScan(maxPosts) {
    if (scanRunning) { log("Scan skipped: another scan is already running"); return { ok: false, busy: true, posts: [], error: "scan already running" }; }
    scanRunning = true;
    try {
      log("=== AUTO SCAN (max " + maxPosts + ") ===");
      // 1. Re-anchor to the newest posts (the previous cycle scrolled down).
      window.scrollTo(0, 0);
      await sleep(1500);
      // 2. Nudge the feed so recent posts (and their lazy images) are rendered.
      for (var i = 0; i < 3; i++) { window.scrollBy(0, 900); await sleep(1500); }

      var posts = findPosts(document.body);
      log("Found " + posts.length + " post elements");
      var results = [];
      var walked = 0;
      for (var j = 0; j < posts.length; j++) {
        if (results.length >= maxPosts || walked >= MAX_WALK) break;
        walked++;
        try {
          await clickSeeMore(posts[j]);
          var pd = extractPostData(posts[j]);
          if (!pd.text && pd.images.length === 0 && !pd.link) continue;
          var key = contentKey(pd);
          if (key && seenKeys.has(key)) continue;   // already handled — don't re-download/re-send
          if (pd.images.length > 0) pd.imagesBase64 = await downloadImagesAsBase64(pd.images); else pd.imagesBase64 = [];
          if (key) seenKeys.add(key);
          results.push(pd);
          log("New post " + results.length + ": " + (pd.text || "").substring(0, 40).replace(/\n/g, " "));
        } catch (e) { log("Post " + walked + " failed: " + e.message); }
      }
      saveSeenKeys();
      log("Scan done: " + results.length + " new post(s), " + walked + " inspected");
      return { ok: true, posts: results };
    } finally {
      scanRunning = false;
    }
  }

  // ------------------------------------------------------------------
  // Posting (composer flow in the target group tab)
  // ------------------------------------------------------------------
  async function doPost(data) {
    if (postingInThisTab) return { ok: false, error: "A post is already being submitted in this tab" };
    postingInThisTab = true;
    try {
      log("doPost: " + (data.text || "").substring(0, 40));
      composerModal = null;
      if (!await openComposer()) return { ok: false, error: "Could not open composer" };
      await sleep(1200);
      var tb = await findComposerTextBox();
      if (!tb) return { ok: false, error: "Could not find composer text box" };
      var body = composeText(data);
      if (body) { await typeText(tb, body); await sleep(500); }
      var imageCount = (data.imagesBase64 && data.imagesBase64.length) || 0;
      if (imageCount > 0) {
        var uploaded = await uploadImagesFromBase64(data.imagesBase64);
        if (!uploaded) return { ok: false, error: "Image upload failed" };
        await sleep(3000);
      }
      if (!await clickPostButton()) return { ok: false, error: "Could not find the Post button" };
      return await waitForComposerClose();
    } finally {
      postingInThisTab = false;
    }
  }

  function findVisibleModal() {
    var ds = document.querySelectorAll("div[role='dialog']");
    var best = null, ba = 0;
    for (var m of ds) {
      var r = m.getBoundingClientRect();
      if (r.width < 100 || r.height < 100) continue;
      var s = window.getComputedStyle(m);
      if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") continue;
      var a = r.width * r.height;
      if (a > ba) { ba = a; best = m; }
    }
    return best;
  }
  function isActuallyVisible(el) {
    if (!el) return false;
    var r = el.getBoundingClientRect();
    if (r.width < 50 || r.height < 20) return false;
    var s = window.getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && s.opacity !== "0";
  }

  async function openComposer() {
    var cs = document.querySelectorAll("div[role='button'], div[role='textbox']");
    for (var el of cs) {
      var t = (el.textContent || "").trim();
      if (t.includes("Create a Post") || t.includes("Create Post") || t.includes("Write something") || t.includes("Create a public post")) {
        el.click();
        for (var i = 0; i < 30; i++) { if (findVisibleModal()) return true; await sleep(200); }
        return true;
      }
    }
    return false;
  }

  async function findComposerTextBox() {
    for (var i = 0; i < 25; i++) {
      var m = findVisibleModal();
      if (!m) { await sleep(200); continue; }
      var all = m.querySelectorAll("div[contenteditable='true'], div[role='textbox'], div[data-lexical-editor='true']");
      var best = null, ba = 0;
      for (var b of all) {
        var al = (b.getAttribute("aria-label") || "").toLowerCase();
        if (al.includes("comment")) continue;
        if (!isActuallyVisible(b)) continue;
        var r = b.getBoundingClientRect();
        var a = r.width * r.height;
        if (a > ba) { ba = a; best = b; }
      }
      if (best) { composerModal = m; return best; }
      await sleep(200);
    }
    return null;
  }

  function composeText(data) {
    var p = [];
    if (data.text) p.push(data.text);
    if (data.link && (!data.text || !data.text.includes(data.link))) p.push(data.link);
    return p.join("\n\n");
  }

  async function typeText(tb, text) {
    tb.focus(); tb.click(); await sleep(100);
    for (var ch of text) { document.execCommand("insertText", false, ch); await sleep(5); }
    await sleep(200);
    var cur = (tb.textContent || "").trim();
    if (cur.length > text.length * 0.5) return;
    tb.focus(); tb.innerHTML = "";
    var lines = text.split("\n");
    for (var l of lines) { var p = document.createElement("div"); p.textContent = l; p.setAttribute("dir", "auto"); tb.appendChild(p); }
    tb.dispatchEvent(new Event("input", { bubbles: true }));
    await sleep(200);
    cur = (tb.textContent || "").trim();
    if (cur.length > text.length * 0.5) return;
    tb.focus();
    try { await navigator.clipboard.writeText(text); await sleep(50); document.execCommand("paste"); await sleep(200); } catch (e) {}
  }

  async function uploadImagesFromBase64(imgs) {
    var m = findVisibleModal();
    if (!m) return false;
    var fi = m.querySelector("input[type='file'][accept*='image']") || m.querySelector("input[type='file'][accept*='photo']") || m.querySelector("input[type='file'][multiple]") || m.querySelector("input[type='file']") || document.querySelector("input[type='file']");
    if (!fi) return false;
    var files = [];
    for (var img of imgs) {
      try { var blob = await (await fetch(img.data)).blob(); files.push(new File([blob], img.name || "image.jpg", { type: img.type || "image/jpeg" })); } catch (e) {}
    }
    if (files.length === 0) return false;
    var dt = new DataTransfer();
    for (var f of files) dt.items.add(f);
    fi.files = dt.files;
    fi.dispatchEvent(new Event("change", { bubbles: true }));
    fi.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  }

  async function clickPostButton() {
    var m = findVisibleModal();
    if (!m) return false;
    var btns = m.querySelectorAll("div[role='button'], button");
    for (var b of btns) {
      var t = (b.textContent || "").trim();
      if (t === "Post" || t === "Share") {
        var d = b.getAttribute("aria-disabled") || b.getAttribute("disabled");
        if (d && d !== "false") {
          for (var i = 0; i < 15; i++) {
            await sleep(1000);
            d = b.getAttribute("aria-disabled") || b.getAttribute("disabled");
            if (!d || d === "false") break;
          }
        }
        b.click();
        return true;
      }
    }
    return false;
  }

  // Wait until OUR composer modal closes. Success = closed without an error alert.
  async function waitForComposerClose() {
    for (var i = 0; i < 30; i++) {
      await sleep(1000);
      var err = findComposerError();
      if (err) return { ok: false, error: err };
      var m = composerModal;
      if (!m || !document.contains(m) || m.getBoundingClientRect().width < 100) return { ok: true };
    }
    return { ok: false, error: "Composer stayed open for 30s — status unknown, verify on Facebook before retrying" };
  }

  function findComposerError() {
    var nodes = document.querySelectorAll("div[role='alert'], span[role='alert']");
    for (var n of nodes) {
      var t = (n.textContent || "").trim();
      if (t.length > 0 && t.length < 200 && /something went wrong|couldn't be posted|could not be posted|can't be posted|unable to post|try again later|post failed/i.test(t)) return t;
    }
    return "";
  }

  log("FB Post Copier v9.1 loaded (incremental scan + post verification)");
})();
