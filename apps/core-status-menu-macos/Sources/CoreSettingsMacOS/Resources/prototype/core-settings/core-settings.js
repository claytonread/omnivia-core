/* ============================================================================
   OmniVia Core Settings — window shell: source list, footer status, pane
   routing, persistence, close, theme and the development harness.
   ============================================================================ */
(function () {
  "use strict";
  var U = window.CoreUI, F = window.CoreFx, P = window.CorePanes, A = window.CoreActions;
  var esc = U.esc, ico = U.ico;
  var KEY = "ov-core-settings:pane", TKEY = "ov-core-settings:scenario", MKEY = "ov-core-settings:mac";

  var NAV = [
    { id: "general", icon: "sliders-horizontal", label: "General" },
    { id: "data", icon: "hard-drive", label: "Data" },
    { id: "processing", icon: "refresh-cw", label: "Processing" },
    { id: "access", icon: "shield", label: "Access" },
    { id: "maintenance", icon: "wrench", label: "Maintenance" }
  ];
  var current = null, scroll = {};

  function renderNav() {
    document.getElementById("sw-nav").innerHTML = NAV.map(function (it) {
      return '<button class="sw-i' + (it.id === current ? " sel" : "") + '" data-pane="' + it.id + '"' + (it.id === current ? ' aria-current="page"' : "") + ">" + ico(it.icon) + '<span class="nm">' + esc(it.label) + "</span></button>";
    }).join("");
  }

  /* One status line, honest to the service state; not repeated elsewhere. */
  function renderFoot(s) {
    var sv = s.service, dot, txt;
    if (sv.state === "running") { dot = "ok"; txt = "Core running"; }
    else if (sv.state === "stopped") { dot = "stop"; txt = "Core stopped"; }
    else if (sv.state === "unreachable") { dot = "lost"; txt = "Core unreachable \u00b7 last seen " + sv.lastSeen; }
    else { dot = "wait"; txt = sv.state === "restarting" ? "Core restarting\u2026" : "Core starting\u2026"; }
    document.getElementById("cs-foot").innerHTML = '<span class="ai-dot ' + dot + '" aria-hidden="true"></span><span>' + esc(txt) + " \u00b7 " + esc(sv.host) + "</span>";
  }

  function render() {
    var s = F.get(), host = document.getElementById("sw-main");
    var focus = document.activeElement, inMain = focus && host.contains(focus);
    var fk = inMain && focus.getAttribute("data-act") ? [focus.getAttribute("data-act"), focus.getAttribute("data-arg")] : null;
    var fid = inMain && !fk && focus.id ? focus.id : null;
    var wasScrolled = host.scrollTop;
    host.innerHTML = '<div class="sw-pane">' + P[current](s) + "</div>";
    renderNav(); renderFoot(s); renderDev(s); U.icons();
    host.scrollTop = wasScrolled;
    if (fk) { var again = host.querySelector('[data-act="' + fk[0] + '"]' + (fk[1] ? '[data-arg="' + fk[1] + '"]' : "")); if (again) again.focus(); }
    else if (fid) { var el = document.getElementById(fid); if (el) { if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "-1"); el.focus(); } }
  }

  function show(id) {
    if (current) scroll[current] = document.getElementById("sw-main").scrollTop;
    var changed = current !== id;
    current = NAV.some(function (n) { return n.id === id; }) ? id : "general";
    try { localStorage.setItem(KEY, current); } catch (e) {}
    P.V.app = null;
    render();
    document.getElementById("sw-main").scrollTop = scroll[current] || 0;
    var lbl = NAV.filter(function (n) { return n.id === current; })[0].label;
    document.title = "OmniVia Core \u2014 " + lbl;
    U.live(lbl);
    if (changed && window.CoreMac) window.CoreMac.refresh("pane opened");
  }
  /* Review → owning pane, row focused and briefly highlighted; disclosures opened as needed. */
  function focusRow(pane, rowId) {
    if (pane === "processing") P.V.sources = true;
    if (pane !== current) show(pane); else render();
    var el = document.getElementById(rowId); if (!el) return;
    var host = document.getElementById("sw-main");
    host.scrollTop = Math.max(0, el.offsetTop - 60);
    el.classList.add("focus"); setTimeout(function () { el.classList.remove("focus"); }, 1600);
    var f = el.querySelector("button,select") || (el.nextElementSibling && el.nextElementSibling.classList.contains("sw-row-sub") ? el.nextElementSibling.querySelector("[data-act]") : null);
    if (f) f.focus(); else { el.setAttribute("tabindex", "-1"); el.focus(); }
  }
  function openDisc(id) { var d = document.querySelector('details[data-disc="' + id + '"]'); if (d && !d.open) d.open = true; }
  function nav(id) { if (id === current) return; A.guard(function () { show(id); }); }

  function applyTheme() {
    var a = F.get().companion.appearance, t = a;
    if (a === "system") t = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", t);
  }

  function closeWin() {
    A.guard(function () {
      document.body.classList.add("closed");
      F.log("local", "Window closed \u2014 Core keeps running");
      renderDev(F.get());
      document.getElementById("cs-reopen").focus();
    });
  }
  function reopen() { document.body.classList.remove("closed"); render(); document.querySelector("#sw-nav .sw-i.sel").focus(); if (window.CoreMac) window.CoreMac.refresh("window opened"); }

  /* Simulated return from System Settings: passive re-read; the result changes
     only when the harness says the user actually changed something. */
  function returnFromSettings(changed) {
    var s = F.get(), m = s.mac, d = m.lastOpened;
    if (changed && d) {
      if (d === "login-items") m.coreStartup = "enabled";
      else if (d === "notifications") m.notifications = "allowed";
      else if (d === "files") Object.keys(m.sources).forEach(function (k) { if (m.sources[k].state === "attention") m.sources[k] = { state: "ok", at: "just now" }; });
      else if (d === "local-network") { m.remote.next = "ok"; m.remote.lastTest = null; }
      else if (d === "sharing") m.shareExt.verified = true;
      F.log("local", "Returned from System Settings \u2014 new native status read");
    } else F.log("local", "Returned from System Settings \u2014 no change observed");
    m.lastOpened = null; m.launchError = false;
    window.CoreMac.refresh("returned from System Settings");
  }

  /* ---- development harness: outside the window, mono, dashed ------------- */
  function renderDev(s) {
    var sc = F.scenario(), mf = F.macId(), M = window.CoreMac;
    var el = document.getElementById("cs-dev");
    el.innerHTML = '<div class="r"><b>Prototype harness</b> <span class="tag">Prototype \u00b7 simulated macOS state</span> all names, sizes, timestamps, versions and macOS results are synthetic \u00b7 scenario:' +
      Object.keys(F.scenarios).map(function (k) { return '<button data-scn="' + k + '"' + (k === sc ? ' class="on"' : "") + ">" + esc(F.scenarios[k].label) + "</button>"; }).join("") + "</div>" +
      '<div class="r"><b>macOS fixture:</b><button data-mac=""' + (!mf ? ' class="on"' : "") + ">none</button>" +
      Object.keys(M.fixtures).map(function (k) { return '<button data-mac="' + k + '"' + (k === mf ? ' class="on"' : "") + " title=\"" + k + "\">" + esc(M.fixtures[k].label) + "</button>"; }).join("") + "</div>" +
      '<div class="r"><b>Return from System Settings:</b><button data-ret="0"' + (s.mac.lastOpened ? "" : " disabled") + '>11 without a change</button><button data-ret="1"' + (s.mac.lastOpened ? "" : " disabled") + '>12 after a confirmed change</button>' +
      '<b style="margin-left:8px">Prompt answers:</b><button data-flag="mac.promptResult"' + (s.mac.promptResult === "denied" ? ' class="on"' : "") + ">deny notifications</button>" +
      '<button data-flag="mac.settingsLaunchFails"' + (s.mac.settingsLaunchFails ? ' class="on"' : "") + ">System Settings fails to open</button></div>" +
      '<div class="r"><b>Next request:</b>' +
      '<button data-flag="access.refuseNext"' + (s.access.refuseNext ? ' class="on"' : "") + ">refuse access change</button>" +
      '<button data-flag="backup.failNext"' + (s.backup.failNext ? ' class="on"' : "") + ">backup fails</button>" +
      '<button data-flag="maintenance.failNext"' + (s.maintenance.failNext ? ' class="on"' : "") + ">rebuild fails</button>" +
      '<button data-flag="companion.appearance">toggle theme</button></div>' +
      '<div class="lg">' + (s.log.length ? s.log.slice(0, 12).map(function (l) { return '<div class="' + l.kind + '">' + l.t + "  " + esc(l.kind.padEnd(8)) + " " + esc(l.msg) + "</div>"; }).join("") : "<div>Requests to Core appear here.</div>") + "</div>";
  }

  function wire() {
    document.addEventListener("click", function (e) {
      var t = e.target;
      var n = t.closest("[data-pane]"); if (n) { nav(n.getAttribute("data-pane")); return; }
      var sc = t.closest("[data-scn]"); if (sc) { resetView(); try { localStorage.setItem(TKEY, sc.getAttribute("data-scn")); } catch (err) {} F.load(sc.getAttribute("data-scn"), F.macId()); applyTheme(); window.CoreMac.refresh("scenario"); return; }
      var mc = t.closest("[data-mac]"); if (mc) { resetView(); var v = mc.getAttribute("data-mac") || null; try { localStorage.setItem(MKEY, v || ""); } catch (err) {} F.load(F.scenario(), v); applyTheme(); if (v !== "MAC-UI-02") window.CoreMac.refresh("fixture"); return; }
      var rt = t.closest("[data-ret]"); if (rt && !rt.disabled) { returnFromSettings(rt.getAttribute("data-ret") === "1"); return; }
      var fl = t.closest("[data-flag]"); if (fl) {
        var k = fl.getAttribute("data-flag").split("."), s = F.get();
        if (k[0] === "companion") { s.companion.appearance = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark"; applyTheme(); }
        else if (k[1] === "promptResult") s.mac.promptResult = s.mac.promptResult === "denied" ? "allowed" : "denied";
        else s[k[0]][k[1]] = !s[k[0]][k[1]];
        F.emit(); return;
      }
      if (t.closest("#cs-close")) { closeWin(); return; }
      if (t.closest("#cs-reopen")) { reopen(); return; }
      var a = t.closest("[data-act]");
      if (a && a.tagName !== "SELECT") { A.dispatch(a.getAttribute("data-act"), a.getAttribute("data-arg"), a); }
    });
    document.addEventListener("change", function (e) {
      var a = e.target.closest && e.target.closest("select[data-act]");
      if (a) A.dispatch(a.getAttribute("data-act"), a.value, a);
    });
    document.getElementById("sw-nav").addEventListener("keydown", function (e) {
      var keys = ["ArrowDown", "ArrowUp", "Home", "End"]; if (keys.indexOf(e.key) < 0) return;
      var items = [].slice.call(document.querySelectorAll("#sw-nav .sw-i")); e.preventDefault();
      var i = items.indexOf(document.activeElement), n;
      if (e.key === "Home") n = 0; else if (e.key === "End") n = items.length - 1;
      else if (e.key === "ArrowDown") n = i < 0 ? 0 : Math.min(i + 1, items.length - 1); else n = i <= 0 ? 0 : i - 1;
      if (n === i) return;
      var pane = items[n].getAttribute("data-pane"); nav(pane);
      var again = document.querySelector('#sw-nav .sw-i[data-pane="' + pane + '"]'); if (again) again.focus();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || U.isSheetOpen() || document.body.classList.contains("closed")) return;
      closeWin();
    });
    if (window.matchMedia) window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
    window.addEventListener("message", function (e) { var d = e.data; if (d && d.type === "ov-theme" && d.theme) document.documentElement.setAttribute("data-theme", d.theme); });

    F.on(function () { if (current) render(); });
    var scn = "normal", mfx = null; try { scn = localStorage.getItem(TKEY) || "normal"; mfx = localStorage.getItem(MKEY) || null; } catch (e) {}
    F.load(scn, mfx); applyTheme();
    var boot = "general"; try { boot = localStorage.getItem(KEY) || "general"; } catch (e) {}
    show(boot);
    if (mfx !== "MAC-UI-02") window.CoreMac.refresh("window opened");
    if (window.parent !== window) { window.parent.postMessage({ type: "ov-owned-ready" }, "*"); window.parent.postMessage({ type: "ov-owned-title", title: "OmniVia Core \u2014 Settings" }, "*"); }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire); else wire();
  window.CoreShell = { show: show, applyTheme: applyTheme, render: render, focusRow: focusRow, openDisc: openDisc };
  function resetView() { P.V.app = null; P.V.sources = false; P.V.backupDraft = null; P.V.appErr = {}; }
})();
