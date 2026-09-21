/* ============================================================================
   OmniVia Core Settings — macOS readiness (amendment UI-CORE-MAC-READINESS-001).
   Passive status model, feature-aware summary, status/recovery presentation and
   the MAC-UI fixtures. Simulated macOS state only: no browser permission APIs,
   no probes. Passive refresh never changes a result; only a harness "return"
   or an explicit check does.
   ============================================================================ */
(function () {
  "use strict";
  var U = window.CoreUI, F = window.CoreFx;
  var esc = U.esc, ico = U.ico;

  function baseMac() {
    return {
      checking: false, observedAt: null, token: 0,
      companionLogin: "enabled",            /* enabled | off | pending */
      coreStartup: "off",                   /* off | needs-approval | not-set-up | enabled | unsupported */
      notifications: "allowed",             /* not-requested | allowed | denied | limited | unknown | asking */
      promptResult: "allowed",              /* what the simulated macOS prompt answers */
      sources: { "Research": { state: "ok", at: "09:41" }, "Meeting notes": { state: "ok", at: "09:41" }, "Reading list": { state: "ok", at: "09:41" } },
      backupDest: { state: "ok", at: "08:12" }, /* ok | absent | read-only | not-checked | checking */
      settingsLaunchFails: false, lastOpened: null, launchError: null, route: null,
      shareExt: { installed: false, verified: false },
      remote: { lastTest: null, testing: false },  /* null | ok | timeout | refused */
      managed: false
    };
  }

  var FX = {
    "MAC-UI-01": { label: "01 local-only, manual, notifs off", apply: function (s, m) { s.companion.startCoreAtLogin = false; s.companion.notifyAttention = false; m.coreStartup = "off"; m.notifications = "not-requested"; } },
    "MAC-UI-02": { label: "02 passive checks pending", apply: function (s, m) { m.checking = true; m.observedAt = null; m.pendingForever = true; Object.keys(m.sources).forEach(function (k) { m.sources[k] = { state: "checking" }; }); m.backupDest = { state: "checking" }; m.notifications = "unknown"; } },
    "MAC-UI-03": { label: "03 startup requested, approval required", apply: function (s, m) { s.companion.startCoreAtLogin = true; m.coreStartup = "needs-approval"; } },
    "MAC-UI-04": { label: "04 startup registered, service stopped", apply: function (s, m) { s.companion.startCoreAtLogin = true; m.coreStartup = "enabled"; s.service.state = "stopped"; s.processing.activity = "unavailable"; } },
    "MAC-UI-05": { label: "05 notifications not yet requested", apply: function (s, m) { s.companion.notifyAttention = true; m.notifications = "not-requested"; m.promptResult = "allowed"; } },
    "MAC-UI-06": { label: "06 notifications denied", apply: function (s, m) { s.companion.notifyAttention = true; m.notifications = "denied"; } },
    "MAC-UI-07": { label: "07 alerts off, badges allowed", apply: function (s, m) { s.companion.notifyAttention = true; m.notifications = "limited"; } },
    "MAC-UI-08": { label: "08 source inaccessible, cause uncertain", apply: function (s, m) { m.sources["Research"] = { state: "attention", at: "09:38", why: "Core could not read this folder. The cause isn\u2019t known." }; } },
    "MAC-UI-09": { label: "09 companion reads, Core reader unverified", apply: function (s, m) { m.sources["Reading list"] = { state: "companion-only", at: "09:40" }; } },
    "MAC-UI-10": { label: "10 backup drive absent / read-only", apply: function (s, m) { m.backupDest = { state: "absent", at: "08:12" }; s.backup.failure = { at: "Today, 08:12", reason: "The backup destination wasn\u2019t available." }; s.backup.last = { at: "Yesterday, 08:10", dest: s.backup.opts.dest, verified: true }; } },
    "MAC-UI-10b": { label: "10b backup drive read-only", apply: function (s, m) { m.backupDest = { state: "read-only", at: "08:12" }; s.backup.failure = { at: "Today, 08:12", reason: "The destination is read-only." }; s.backup.last = { at: "Yesterday, 08:10", dest: s.backup.opts.dest, verified: true }; } },
    "MAC-UI-13": { label: "13 System Settings won\u2019t open", apply: function (s, m) { s.companion.startCoreAtLogin = true; m.coreStartup = "needs-approval"; m.settingsLaunchFails = true; } },
    "MAC-UI-14": { label: "14 remote test times out", apply: function (s, m) { s.service.target = "remote"; s.service.host = "studio-mini.local"; s.companion.startCoreSupported = false; s.processing.battery.supported = false; m.coreStartup = "unsupported"; m.remote.next = "timeout"; } },
    "MAC-UI-15": { label: "15 qualified Local Network refusal", apply: function (s, m) { s.service.target = "remote"; s.service.host = "studio-mini.local"; s.companion.startCoreSupported = false; s.processing.battery.supported = false; m.coreStartup = "unsupported"; m.remote.next = "refused"; } },
    "MAC-UI-16": { label: "16 managed / unsupported startup", apply: function (s, m) { m.coreStartup = "not-set-up"; m.managed = true; } },
    "MAC-UI-17": { label: "17 Share extension installed, unverified", apply: function (s, m) { m.shareExt = { installed: true, verified: false }; } }
  };
  /* 11 and 12 are the two harness "Return from System Settings" buttons; 18 is
     exercised by switching workspace during a folder check. */

  var current = null;
  function apply(s, id) {
    var m = baseMac();
    if (id && FX[id]) FX[id].apply(s, m);
    current = id && FX[id] ? id : null;
    s.mac = m;
    return s;
  }

  /* ---- passive refresh: re-reads, never mutates a result -------------------- */
  function refresh(reason) {
    var s = F.get(), m = s.mac; if (!m || m.pendingForever) return;
    m.checking = true; var tok = ++m.token; F.log("passive", "Refresh status (" + reason + ")"); F.emit();
    setTimeout(function () { var st = F.get(); if (!st.mac || st.mac.token !== tok) return; st.mac.checking = false; st.mac.observedAt = "just now"; F.emit(); }, 700);
  }

  /* ---- issue model ----------------------------------------------------------- */
  function issues(s) {
    var m = s.mac, c = s.companion, out = [], unknown = [];
    if (c.startCoreAtLogin && m.coreStartup === "needs-approval") out.push({ id: "startup", pane: "general", row: "g-start", t: "Start Core at login needs approval in Login Items" });
    if (c.notifyAttention) {
      if (m.notifications === "denied") out.push({ id: "notif", pane: "general", row: "g-notify", t: "Notifications are blocked in macOS" });
      else if (m.notifications === "limited") out.push({ id: "notif", pane: "general", row: "g-notify", t: "Alerts are off in macOS; only badges are allowed" });
      else if (m.notifications === "not-requested") out.push({ id: "notif", pane: "general", row: "g-notify", t: "Notifications need your permission" });
      else if (m.notifications === "unknown" || m.notifications === "checking") unknown.push("notifications");
    }
    if (F.ws()) {
      s.processing.sources.forEach(function (x) { var r = m.sources[x[0]]; if (!r) return;
        if (r.state === "attention") out.push({ id: "src:" + x[0], pane: "processing", row: "src-" + x[0], t: x[0] + " folder needs attention" });
        else if (r.state !== "ok") unknown.push(x[0]); });
      if (m.backupDest.state === "absent" || m.backupDest.state === "read-only") out.push({ id: "backup", pane: "data", row: "d-backup", t: m.backupDest.state === "absent" ? "Backup destination unavailable" : "Backup destination is read-only" });
      else if (m.backupDest.state !== "ok") unknown.push("backup destination");
    }
    return { issues: out, unknown: unknown };
  }
  function applicable(s) { return s.companion.startCoreAtLogin || s.companion.notifyAttention || !!F.ws(); }

  function summary(s) {
    var m = s.mac, r = issues(s);
    if (r.issues.length) return { kind: "attention", t: r.issues.length === 1 ? "One selected feature needs attention." : r.issues.length + " selected features need attention.", d: r.issues.length === 1 ? r.issues[0].t : null, list: r.issues };
    if (m.checking && !m.observedAt) return { kind: "checking", t: "Checking macOS status\u2026", d: "The window stays usable. Nothing is requested or started." };
    if (!applicable(s)) return { kind: "ok", t: "No additional macOS access is needed for your current setup.", d: "Optional features you haven\u2019t chosen aren\u2019t checked." };
    if (r.unknown.length) return { kind: "unknown", t: "Some access checks need verification.", d: "Not checked: " + r.unknown.join(", ") + ". A check may ask macOS for access; it does not import content." };
    return { kind: "ok", t: "No macOS issues detected for your current setup.", d: m.observedAt ? "Last checked " + m.observedAt + "." : null };
  }

  /* ---- presentation ------------------------------------------------------------ */
  var KIND = { ok: ["check-circle-2", "ok"], neutral: ["circle-dashed", "neu"], approval: ["alert-circle", "warn"], attention: ["alert-triangle", "warn"], unknown: ["circle-dashed", "neu"], stale: ["clock", "neu"], checking: ["", "neu"], off: ["circle-slash", "neu"] };
  function status(kind, label) {
    var k = KIND[kind] || KIND.unknown;
    return '<span class="cs-status ' + k[1] + '">' + (kind === "checking" ? '<span class="cs-sp" aria-hidden="true"></span>' : ico(k[0])) + esc(label) + "</span>";
  }
  /* Recovery block: lives in the row's supporting area, never on the control line. */
  function fix(o) {
    return '<div class="cs-fix' + (o.kind ? " " + o.kind : "") + '"' + (o.id ? ' id="' + esc(o.id) + '"' : "") + '><div class="cs-fix-t">' + esc(o.t) + "</div>" + (o.d ? '<div class="cs-fix-d">' + o.d + "</div>" : "") +
      (o.acts ? '<div class="cs-fix-a">' + o.acts + "</div>" : "") + (o.after || "") + "</div>";
  }
  /* What happened when System Settings was asked to open, for the given destination. */
  var ROUTES = { "login-items": ["Open Login Items", "System Settings \u2192 General \u2192 Login Items & Extensions", "Allow \u201cOmniVia Core\u201d under Allow in the background."],
    "notifications": ["Open Notifications", "System Settings \u2192 Notifications \u2192 OmniVia Core", "Turn on Allow notifications and Alerts."],
    "files": ["Open Files & Folders", "System Settings \u2192 Privacy & Security \u2192 Files & Folders", "Review folder access for \u201cOmniVia Core\u201d."],
    "local-network": ["Open Local Network", "System Settings \u2192 Privacy & Security \u2192 Local Network", "Allow \u201cOmniVia Core\u201d."],
    "sharing": ["Open Sharing Extensions", "System Settings \u2192 General \u2192 Login Items & Extensions \u2192 Sharing", "Turn on Share to Core."] };
  function sysBtn(dest, o) { o = o || {}; return U.btn(ROUTES[dest][0], o.cls || "ov-btn--bordered", { act: "sys-open", arg: dest, disabled: o.disabled }); }
  function sysNote(m, dest) {
    if (m.lastOpened !== dest) return "";
    var r = ROUTES[dest];
    if (m.launchError) return '<div class="cs-inline-err">' + ico("alert-circle") + "<span>System Settings could not be opened. Open it yourself and go to <b>" + esc(r[1]) + "</b>. " + esc(r[2]) + "</span></div>" + '<div class="cs-fix-a">' + U.btn("Try again", "ov-btn--bordered", { act: "sys-open", arg: dest }) + "</div>";
    return U.note("System Settings opened. If it didn\u2019t land on the right page: <b>" + esc(r[1]) + "</b>. " + esc(r[2]) + " Status re-reads when you come back.", "external-link");
  }

  window.CoreMac = { apply: apply, fixtures: FX, current: function () { return current; }, refresh: refresh, issues: issues, summary: summary, status: status, fix: fix, sysBtn: sysBtn, sysNote: sysNote, ROUTES: ROUTES };
})();
