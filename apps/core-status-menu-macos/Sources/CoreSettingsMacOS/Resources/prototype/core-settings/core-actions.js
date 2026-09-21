/* ============================================================================
   OmniVia Core Settings — actions. Every visible control lands here; every
   service-backed change goes through CoreFx.request() so pending, refused and
   acknowledged states are real, not decorative.
   ============================================================================ */
(function () {
  "use strict";
  var U = window.CoreUI, F = window.CoreFx, P = window.CorePanes, V = P.V;
  var esc = U.esc;
  function S() { return F.get(); }
  function saved() { U.toast("Saved", "ok", "check"); }
  function app(id) { return S().access.apps.filter(function (a) { return a.id === id; })[0]; }
  function now() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
  function fail(e) { U.toast(e && e.message ? e.message : "Core declined the change.", "warn", "alert-circle"); }
  function cancelFoot(label, act, cls) { return U.btn("Cancel", "ov-btn--plain", { act: "sheet-cancel" }) + U.btn(label, cls || "ov-btn--primary", { act: act }); }

  var ACT = {
    /* ---- General: companion preferences, local to this Mac ---------------- */
    "gen-menu": function () {
      var s = S(); s.mac.companionLogin = "pending"; F.emit();
      F.request("Login item: " + (s.companion.openAtLogin ? "unregister" : "register") + " companion", function (st) { st.companion.openAtLogin = !st.companion.openAtLogin; st.mac.companionLogin = st.companion.openAtLogin ? "enabled" : "off"; }, 700)
        .then(saved).catch(function (e) { S().mac.companionLogin = S().companion.openAtLogin ? "enabled" : "off"; F.emit(); fail(e); });
    },
    "gen-start": function () {
      var s = S(), m = s.mac;
      if (m.coreStartup === "not-set-up" || m.coreStartup === "unsupported") return;
      if (s.companion.startCoreAtLogin) {
        /* Turning off can affect a running background component: say so first. */
        U.sheet({ title: "Stop starting Core at login?", sub: "Core on this Mac", body: "<p style=\"margin:0;font-size:12.5px;line-height:1.5\">Core will no longer start when you sign in. Depending on how it was installed, macOS may also stop the background component now; connected applications would lose their connection until you start Core again.</p>", foot: cancelFoot("Turn off", "gen-start-off") });
        return;
      }
      m.coreStartup = "pending"; s.companion.startCoreAtLogin = true; F.emit();
      F.request("Register Core login item", function (st) { st.mac.coreStartup = "needs-approval"; }, 800)
        .then(function () { U.toast("Approve OmniVia Core in Login Items to finish", "warn", "alert-circle"); })
        .catch(function (e) { var st = S(); st.companion.startCoreAtLogin = false; st.mac.coreStartup = "off"; F.emit(); fail(e); });
    },
    "gen-start-off": function () { U.closeSheet(); var s = S(); s.mac.coreStartup = "pending"; F.emit(); F.request("Unregister Core login item", function (st) { st.companion.startCoreAtLogin = false; st.mac.coreStartup = "off"; }, 700).then(saved).catch(fail); },
    "gen-notify": function () { F.local("Notify when Core needs attention", function (s) { s.companion.notifyAttention = !s.companion.notifyAttention; }); saved(); },
    /* User-initiated only. The simulated prompt answers from the fixture; never re-asked after a denial. */
    "notif-allow": function () {
      var s = S(); if (s.mac.notifications !== "not-requested") return;
      s.mac.notifications = "asking"; F.emit();
      F.request("Request notification authorisation (macOS prompt)", function (st) { st.mac.notifications = st.mac.promptResult || "allowed"; }, 1400)
        .then(function () { var n = S().mac.notifications; U.toast(n === "allowed" ? "Notifications allowed" : "Notifications were not allowed. You can change this in System Settings.", n === "allowed" ? "ok" : "warn", n === "allowed" ? "check" : "alert-circle"); });
    },
    /* Opening System Settings never grants anything. Launch can fail; the result is unchanged either way. */
    "sys-open": function (dest) {
      var s = S(), m = s.mac; m.lastOpened = dest; m.launchError = false;
      if (m.settingsLaunchFails) { m.launchError = true; F.log("refused", "Open System Settings (" + dest + ") \u2014 launch failed"); F.emit(); return; }
      F.log("request", "Open System Settings \u2192 " + window.CoreMac.ROUTES[dest][1]); F.emit();
      U.toast("Opened System Settings", null, "external-link");
    },
    "mac-refresh": function () { window.CoreMac.refresh("Refresh status"); },
    "mac-review": function (id) {
      var r = window.CoreMac.issues(S()).issues.filter(function (i) { return i.id === id; })[0]; if (!r) return;
      window.CoreShell.focusRow(r.pane, r.row);
    },
    "gen-appearance": function (v) { F.local("Appearance: " + v, function (s) { s.companion.appearance = v; }); window.CoreShell.applyTheme(); },

    /* ---- Data ---------------------------------------------------------------- */
    "ws-select": function (v) {
      /* Results belong to a workspace: a pending or old check never carries over. */
      F.local("Manage workspace: " + v, function (s) { s.selected = v; s.mac.token++; s.processing.sources.forEach(function (x) { s.mac.sources[x[0]] = { state: "not-checked" }; }); s.mac.backupDest = { state: "not-checked" }; });
      V.app = null; V.sources = false; U.toast("Managing " + F.ws().name + ". Other applications keep their bindings.", null, "hard-drive");
      window.CoreMac.refresh("workspace changed");
    },
    "ws-choose": function () {
      var s = S();
      U.sheet({ title: "Choose a workspace to manage", sub: "Only workspaces this Mac is already authorised for are listed. Choosing one here does not change what other applications use.",
        body: s.workspaces.map(function (w, i) { return '<label class="cs-radio"><input type="radio" name="ws" value="' + esc(w.id) + '"' + (i === 0 ? " checked" : "") + '><span><span class="t">' + esc(w.name) + '</span><div class="d">' + esc(w.path) + " \u00b7 " + esc(w.total) + "</div></span></label>"; }).join(""),
        foot: cancelFoot("Manage workspace", "ws-choose-go") });
    },
    "ws-choose-go": function () { var r = document.querySelector('#cs-sheet input[name="ws"]:checked'); U.closeSheet(); if (r) ACT["ws-select"](r.value); },
    "path-copy": function (p) { if (navigator.clipboard) navigator.clipboard.writeText(p).catch(function () {}); U.toast("Path copied", "ok", "check"); },
    "path-finder": function (p) { F.log("request", "Reveal in Finder: " + p); F.emit(); U.toast("Asked Finder to show the storage folder", null, "external-link"); },
    "backup-now": function () {
      var s = S(); if (s.backup.busy) return;
      s.backup.busy = "backing"; F.emit();
      F.request("Back up workspace " + F.ws().name, function (st) { st.backup.busy = "verifying"; }, 1600, s.backup.failNext ? "The backup destination wasn\u2019t available." : null)
        .then(function () { return F.request("Verify backup", function (st) { st.backup.busy = null; st.backup.failure = null; st.backup.last = { at: "Today, " + now(), dest: st.backup.opts.dest, verified: true }; }, 900); })
        .then(function () { U.toast("Backup complete and verified", "ok", "check"); })
        .catch(function (e) { var st = S(); st.backup.busy = null; st.backup.failNext = false; st.backup.failure = { at: "Today, " + now(), reason: e.message }; F.emit(); fail(e); });
    },
    "bk-opt-dest": function (v) { draft().dest = v; F.emit(); },
    "bk-opt-schedule": function (v) { draft().schedule = v; F.emit(); },
    "bk-opt-retention": function (v) { draft().retention = v; F.emit(); },
    "bk-discard": function () { V.backupDraft = null; F.emit(); },
    "bk-save": function () { var d = V.backupDraft; F.request("Save backup options", function (s) { s.backup.opts = d; s.mac.backupDest = { state: "not-checked" }; }, 500).then(function () { V.backupDraft = null; F.emit(); saved(); }).catch(fail); },
    "bk-rechoose": function () { window.CoreShell.openDisc("backup-options"); var sel = document.querySelector('select[data-act="bk-opt-dest"]'); if (sel) sel.focus(); },
    "bk-test": function () {
      U.sheet({ title: "Test backup access?", sub: esc(S().backup.opts.dest),
        body: "<p style=\"margin:0;font-size:12.5px;line-height:1.5\">Core checks that it can read the destination and write to it by creating and removing one small temporary file. It does not create a backup or change existing ones.</p>",
        foot: cancelFoot("Test access", "bk-test-go") });
    },
    "bk-test-go": function () {
      U.closeSheet(); var s = S(), prev = s.mac.backupDest, tok = ++s.mac.token; s.mac.backupDest = { state: "checking" }; F.emit();
      var outcome = prev.state === "absent" ? "absent" : prev.state === "read-only" ? "read-only" : "ok";
      F.request("Test backup destination access", function (st) { if (st.mac.token !== tok) return; st.mac.backupDest = { state: outcome, at: outcome === "ok" ? "just now" : prev.at || "just now" }; }, 1300)
        .then(function () { U.toast(outcome === "ok" ? "Backup destination: read and write access checked" : outcome === "absent" ? "Destination still unavailable" : "Destination is read-only", outcome === "ok" ? "ok" : "warn", outcome === "ok" ? "check" : "alert-circle"); });
    },

    /* ---- Processing ---------------------------------------------------------- */
    "proc-auto": function () { F.request("Process changes automatically", function (s) { s.processing.auto = !s.processing.auto; }, 400).then(saved).catch(fail); },
    "proc-profile": function (v) { F.request("Resource use: " + v, function (s) { s.processing.profile = v; }, 400).then(saved).catch(fail); },
    "proc-battery": function () { F.request("Pause heavy processing on battery", function (s) { s.processing.battery.pause = !s.processing.battery.pause; }, 400).then(saved).catch(fail); },
    "proc-sources": function () { V.sources = !V.sources; F.emit(); },
    "src-check": function (name) {
      U.sheet({ title: "Check access to \u201c" + name + "\u201d?", sub: "Core reads the folder listing once to confirm it can access the source.",
        body: "<p style=\"margin:0;font-size:12.5px;line-height:1.5\">macOS may ask whether OmniVia Core can access this folder. Nothing is imported or indexed by this check, and processing settings don\u2019t change.</p>",
        foot: cancelFoot("Check folder access", "src-check-go") });
      document.querySelector("#cs-sheet [data-act=src-check-go]").setAttribute("data-arg", name);
    },
    "src-check-go": function (name) {
      U.closeSheet(); var s = S(), prev = s.mac.sources[name] || {}, tok = ++s.mac.token; s.mac.sources[name] = { state: "checking" }; F.emit();
      var outcome = prev.state === "attention" ? "attention" : "ok";
      F.request("Check folder access: " + name, function (st) { if (st.mac.token !== tok) return; st.mac.sources[name] = outcome === "ok" ? { state: "ok", at: "just now" } : { state: "attention", at: "just now", why: prev.why }; }, 1500)
        .then(function () { if (S().mac.token !== tok) return; U.toast(outcome === "ok" ? name + ": access checked just now" : name + ": Core still can\u2019t read this folder", outcome === "ok" ? "ok" : "warn", outcome === "ok" ? "check" : "alert-circle"); });
    },
    "src-rechoose": function (name) {
      /* A cancelled picker keeps the prior source. The simulated picker re-selects the same folder. */
      U.sheet({ title: "Choose folder again", sub: "macOS opens a folder picker. Choosing the folder renews Core\u2019s access to it; cancelling keeps the current source unchanged.",
        body: '<div class="ai-code">' + esc(S().processing.sources.filter(function (x) { return x[0] === name; })[0][1]) + "</div>" + U.note("The picker is simulated in this prototype.", "info"),
        foot: cancelFoot("Choose this folder", "src-rechoose-go") });
      document.querySelector("#cs-sheet [data-act=src-rechoose-go]").setAttribute("data-arg", name);
    },
    "src-rechoose-go": function (name) {
      U.closeSheet(); var s = S(); s.mac.sources[name] = { state: "checking" }; F.emit();
      F.request("Renew folder bookmark: " + name, function (st) { st.mac.sources[name] = { state: "ok", at: "just now" }; }, 1200).then(function () { U.toast(name + ": access checked just now", "ok", "check"); });
    },
    "remote-test": function () {
      var s = S(); s.mac.remote.testing = true; F.emit();
      var next = s.mac.remote.next || "ok";
      F.request("Test connection to " + s.service.host, function (st) { st.mac.remote.testing = false; st.mac.remote.lastTest = next; }, 1800)
        .then(function () { U.toast(next === "ok" ? "Connected to " + S().service.host : next === "timeout" ? "Could not connect" : "Local Network access refused by macOS", next === "ok" ? "ok" : "warn", next === "ok" ? "check" : "alert-circle"); });
    },
    "proc-pause": function () { S().processing.pausePending = true; F.emit(); F.request("Pause processing", function (s) { s.processing.pausePending = false; s.processing.activity = "paused"; s.processing.waiting = 3; }, 1400).then(function () { U.toast("Processing paused. Search remains available.", null, "info"); }); },
    "proc-resume": function () { S().processing.resumePending = true; F.emit(); F.request("Resume processing", function (s) { s.processing.resumePending = false; s.processing.activity = s.processing.waiting ? "waiting" : "up-to-date"; }, 900).then(function () { setTimeout(function () { var s = S(); if (s.processing.activity === "waiting") { s.processing.activity = "up-to-date"; s.processing.waiting = 0; F.emit(); } }, 3000); }); },

    /* ---- Access -------------------------------------------------------------- */
    "acc-details": function (id) { V.app = V.app === id ? null : id; F.emit(); },
    "acc-level": function (v, el) {
      var id = el.getAttribute("data-arg"), a = app(id);
      if (!a || v === a.level) return;
      if (v === "contribute") ACT["acc-promote"](id); else ACT["acc-demote"](id);
    },
    "acc-promote": function (id) {
      var a = app(id);
      U.sheet({ title: "Allow " + a.name + " to contribute?", sub: "Workspace: " + esc(F.ws().name),
        body: '<div class="cs-check in">' + U.ico("check") + "<span>Keeps searching and retrieving what it can already access.</span></div>" +
          '<div class="cs-check in">' + U.ico("check") + "<span>Can submit evidence and proposed memory for review.</span></div>" +
          '<div class="cs-check out">' + U.ico("x") + "<span>Cannot approve its own contributions as trusted knowledge.<span class=\"q\">Approval stays with you in OmniVia.</span></span></div>" +
          U.note("Core records this authorisation. The change applies once Core acknowledges it.", "info"),
        foot: cancelFoot("Allow contributions", "acc-promote-go"), onCancel: function () { F.emit(); } });
      document.querySelector("#cs-sheet [data-act=acc-promote-go]").setAttribute("data-arg", id);
    },
    "acc-promote-go": function (id) {
      U.closeSheet(); var a = app(id); a.pending = "Authorising\u2026"; delete V.appErr[id]; F.emit();
      F.request("Grant contribute: " + a.name, function (s) { app(id).level = "contribute"; }, 900, S().access.refuseNext ? "You don\u2019t manage access for this workspace." : null)
        .then(function () { app(id).pending = null; F.emit(); saved(); })
        .catch(function (e) { var st = S(); st.access.refuseNext = false; a.pending = null; V.appErr[id] = "Core declined the change: " + e.message + " Ask the workspace owner in OmniVia to grant it. Access stays Read only."; F.emit(); });
    },
    "acc-demote": function (id) {
      var a = app(id);
      U.sheet({ title: "Remove contribution permission?", sub: esc(a.name) + " returns to Read only.",
        body: "<p style=\"margin:0;font-size:12.5px;line-height:1.5\">It keeps retrieving what it is allowed to access, but can no longer submit evidence or proposed memory. Anything it already submitted stays in the workspace and keeps its current review state.</p>",
        foot: cancelFoot("Remove permission", "acc-demote-go"), onCancel: function () { F.emit(); } });
      document.querySelector("#cs-sheet [data-act=acc-demote-go]").setAttribute("data-arg", id);
    },
    "acc-demote-go": function (id) {
      U.closeSheet(); var a = app(id); a.pending = "Updating\u2026"; F.emit();
      F.request("Reduce to read only: " + a.name, function () { app(id).level = "read"; }, 700).then(function () { app(id).pending = null; F.emit(); saved(); }).catch(function (e) { a.pending = null; F.emit(); fail(e); });
    },
    "acc-revoke": function (id) {
      var a = app(id);
      U.sheet({ title: "Revoke access for " + a.name + "?", sub: "Workspace: " + esc(F.ws().name),
        body: "<p style=\"margin:0;font-size:12.5px;line-height:1.5\">This connection loses its authority to use the workspace. Information it already submitted is not deleted, and work Core has already accepted is not cancelled. You can add the application again later.</p>",
        foot: cancelFoot("Revoke access", "acc-revoke-go", "ov-btn--danger") });
      document.querySelector("#cs-sheet [data-act=acc-revoke-go]").setAttribute("data-arg", id);
    },
    "acc-revoke-go": function (id) {
      U.closeSheet(); var a = app(id); a.pending = "Revoking\u2026"; F.emit();
      F.request("Revoke access: " + a.name, function (s) { s.access.apps = s.access.apps.filter(function (x) { return x.id !== id; }); }, 800).then(function () { V.app = null; F.emit(); U.toast("Access revoked for " + a.name, "ok", "check"); }).catch(function (e) { a.pending = null; F.emit(); fail(e); });
    },
    "acc-add": function () {
      U.sheet({ title: "Add application", sub: "Grant an application access to <b>" + esc(F.ws().name) + "</b>. You\u2019ll get connection instructions after it is added.",
        body: '<div class="ai-field"><label for="add-name">Application name</label><div class="ov-field"><input id="add-name" type="text" placeholder="e.g. Writing assistant" autofocus></div><div class="ai-help">A name you will recognise in this list.</div></div>' +
          '<div class="ai-field"><label for="add-host">Application type</label><select id="add-host" class="ai-select"><option>MCP client</option><option>Command-line tool</option></select></div>' +
          '<div class="ai-field"><label>Access</label><label class="cs-radio"><input type="radio" name="lvl" value="read" checked><span><span class="t">Read only</span><div class="d">Can search and retrieve information it is allowed to access.</div></span></label>' +
          '<label class="cs-radio"><input type="radio" name="lvl" value="contribute"><span><span class="t">Read and contribute</span><div class="d">Can also submit evidence and proposed memory. It cannot approve its own contributions as trusted knowledge.</div></span></label></div>',
        foot: cancelFoot("Add application", "acc-add-go") });
    },
    "acc-add-go": function () {
      var name = (document.getElementById("add-name").value || "").trim(), host = document.getElementById("add-host").value, lvl = document.querySelector('#cs-sheet input[name="lvl"]:checked').value;
      if (!name) { document.getElementById("add-name").focus(); document.getElementById("add-name").parentNode.setAttribute("aria-invalid", "true"); return; }
      var id = "app-" + Date.now();
      U.sheetUpdate(U.pending("Registering " + name + "\u2026"), "");
      F.request("Add application: " + name + " (" + lvl + ")", function (s) { s.access.apps.push({ id: id, name: name, host: host, level: lvl, connected: false, lastActivity: "Never", pending: null, error: null }); }, 900)
        .then(function () {
          var t = S().access.transport;
          U.sheetUpdate('<p style="margin:0 0 10px;font-size:12.5px;line-height:1.5"><b>' + esc(name) + "</b> was added with " + P.levelName(lvl) + " access. In its settings, point it at Core on this Mac:</p>" +
            '<div class="ai-code">' + esc(t.path) + "</div>" + U.note("Connections are local to this Mac. The application appears as Connected once it has used this address.", "info"),
            U.btn("Copy address", "ov-btn--bordered", { act: "path-copy", arg: t.path }) + U.btn("Done", "ov-btn--primary", { act: "sheet-cancel" }));
          V.app = id;
        }).catch(function (e) { U.closeSheet(); fail(e); });
    },

    /* ---- Maintenance --------------------------------------------------------- */
    "core-restart": function () {
      var n = S().access.apps.filter(function (a) { return a.connected; }).length;
      U.sheet({ title: "Restart Core?", sub: "Core on this Mac",
        body: "<p style=\"margin:0;font-size:12.5px;line-height:1.5\">" + (n ? n + " connected application" + (n > 1 ? "s" : "") + " will lose the connection for a few seconds and reconnect on their own. " : "") + "Background processing resumes where it left off. Nothing in the workspace is changed.</p>",
        foot: cancelFoot("Restart Core", "core-restart-go") });
    },
    "core-restart-go": function () {
      U.closeSheet(); S().service.state = "restarting"; F.emit();
      F.request("Restart Core", function (s) { s.service.state = "running"; }, 1800).then(function () { U.toast("Core restarted", "ok", "check"); });
    },
    "core-start": function () {
      S().service.state = "starting"; F.emit();
      F.request("Start Core", function (s) { s.service.state = "running"; s.processing.activity = "up-to-date"; }, 1500).then(function () { U.toast("Core is running", "ok", "check"); });
    },
    "core-retry": function () { F.request("Reconnect to Core", null, 1200, "Core is still unreachable.").catch(fail); },
    "core-updates": function () { S().service.checking = true; F.emit(); F.request("Check for updates (installer)", function (s) { s.service.checking = false; s.service.lastChecked = "just now"; }, 1300).then(function () { U.toast("Core " + S().service.version.split(" ")[0] + " is up to date", "ok", "check"); }); },
    "diag-logs": function () {
      var lines = ["09:41:02  core      service ready (pid 4821)", "09:41:03  index     opened workspace \u201cStudio\u201d \u00b7 4,210 records", "09:41:09  access    research-assistant connected (read)", "09:44:17  process   3 changed files queued from \u201cResearch\u201d", "09:44:31  process   queue drained \u00b7 up to date", "09:52:40  backup    verified \u201cToday, 08:12\u201d snapshot", "10:03:12  companion settings window opened"];
      U.sheet({ title: "Logs", sub: "Recent events from Core and the companion on this Mac. Paths and workspace content are not logged.", wide: true,
        body: '<div class="cs-log">' + esc(lines.join("\n")) + "</div>", foot: U.btn("Copy", "ov-btn--bordered", { act: "path-copy", arg: lines.join("\n") }) + U.btn("Close", "ov-btn--primary", { act: "sheet-cancel" }) });
    },
    "diag-export": function () {
      U.sheet({ title: "Export diagnostics", sub: "Review what the bundle includes before it is created.",
        body: '<div class="ai-sec-h">Included</div>' +
          ['Core and companion versions', 'Service state and recent logs (last 24 hours)', 'Index health and processing queue summary', 'Connected application names and access levels'].map(function (t) { return '<div class="cs-check in">' + U.ico("check") + "<span>" + t + "</span></div>"; }).join("") +
          '<div class="ai-sec-h" style="margin-top:14px">Not included</div>' +
          [['Workspace content and records', ''], ['Credentials, tokens and socket paths', ''], ['File names from your sources', 'Logs are exported as recorded; they are not otherwise rewritten.']].map(function (t) { return '<div class="cs-check out">' + U.ico("x") + "<span>" + t[0] + (t[1] ? '<span class="q">' + t[1] + "</span>" : "") + "</span></div>"; }).join(""),
        foot: cancelFoot("Export", "diag-export-go") });
    },
    "diag-export-go": function () {
      U.sheetUpdate(U.pending("Collecting diagnostics\u2026"), "");
      F.request("Export diagnostics bundle", function (s) { s.maintenance.exportResult = "Last exported today at " + now() + " \u00b7 1.2 MB"; }, 1700).then(function () {
        U.sheetUpdate('<p style="margin:0;font-size:12.5px;line-height:1.5"><b>omnivia-core-diagnostics-2026-09-21.zip</b> \u00b7 1.2 MB<br>Saved to Downloads.</p>',
          U.btn("Show in Finder", "ov-btn--bordered", { act: "path-finder", arg: "~/Downloads/omnivia-core-diagnostics-2026-09-21.zip" }) + U.btn("Done", "ov-btn--primary", { act: "sheet-cancel" }));
      }).catch(function (e) { U.closeSheet(); fail(e); });
    },
    "maint-rebuild": function () {
      U.sheet({ title: "Rebuild search indexes?", sub: "Workspace: " + esc(F.ws() ? F.ws().name : ""),
        body: "<p style=\"margin:0;font-size:12.5px;line-height:1.5\">Core rebuilds the search and embedding indexes from workspace records. Records are not deleted or changed. Search keeps using the current indexes until the rebuild finishes; results may be briefly incomplete. This usually takes a few minutes.</p>",
        foot: cancelFoot("Rebuild indexes", "maint-rebuild-go") });
    },
    "maint-rebuild-go": function () {
      U.closeSheet(); var s = S(); s.maintenance.rebuild = { state: "running", pct: 0 }; F.log("request", "Rebuild search indexes"); F.emit();
      var t = setInterval(function () {
        var st = S(), r = st.maintenance.rebuild; if (!r || r.state !== "running") { clearInterval(t); return; }
        r.pct = Math.min(100, r.pct + 9);
        if (st.maintenance.failNext && r.pct >= 45) { r.state = "failed"; st.maintenance.failNext = false; F.log("refused", "Rebuild stopped at " + r.pct + "%"); U.toast("Index rebuild stopped. Previous indexes remain in use.", "warn", "alert-circle"); clearInterval(t); }
        else if (r.pct >= 100) { st.maintenance.rebuild = null; st.maintenance.lastRebuild = "today at " + now(); F.log("ok", "Rebuild search indexes"); U.toast("Indexes rebuilt \u00b7 4,210 records", "ok", "check"); clearInterval(t); }
        F.emit();
      }, 300);
    },
    "sheet-cancel": function () { var el = document.getElementById("cs-sheet"); if (el && el._onCancel) el._onCancel(); U.closeSheet(); }
  };

  function draft() { if (!V.backupDraft) V.backupDraft = JSON.parse(JSON.stringify(S().backup.opts)); return V.backupDraft; }

  /* Unsaved multi-field edits are handled before navigation or close. */
  function guard(cb) {
    var dirty = V.backupDraft && JSON.stringify(V.backupDraft) !== JSON.stringify(S().backup.opts);
    if (!dirty) { cb(); return; }
    U.sheet({ title: "Save backup options?", sub: "You changed backup options but haven\u2019t saved them.",
      body: "", foot: U.btn("Discard", "ov-btn--plain", { act: "guard-discard" }) + U.btn("Keep editing", "ov-btn--bordered", { act: "sheet-cancel" }) + U.btn("Save changes", "ov-btn--primary", { act: "guard-save" }) });
    ACT["guard-discard"] = function () { U.closeSheet(); V.backupDraft = null; F.emit(); cb(); };
    ACT["guard-save"] = function () { U.closeSheet(); var d = V.backupDraft; F.request("Save backup options", function (s) { s.backup.opts = d; }, 500).then(function () { V.backupDraft = null; F.emit(); saved(); cb(); }).catch(fail); };
  }

  function dispatch(name, arg, el) { var f = ACT[name]; if (f) f(arg, el); else { F.log("refused", "No handler: " + name); F.emit(); } }

  window.CoreActions = { dispatch: dispatch, guard: guard };
})();
