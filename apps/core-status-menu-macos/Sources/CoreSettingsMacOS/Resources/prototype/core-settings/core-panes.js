/* ============================================================================
   OmniVia Core Settings — the five panes. Pure rendering from CoreFx state +
   a little view state; every control dispatches through data-act.
   ============================================================================ */
(function () {
  "use strict";
  var U = window.CoreUI, F = window.CoreFx;
  var esc = U.esc, ico = U.ico, row = U.row, rows = U.rows, group = U.group;

  /* view-only state (never service state) */
  var V = { app: null, sources: false, backupDraft: null, appErr: {}, logs: null };

  function live() { return F.live(); }
  function remote() { return F.get().service.target === "remote"; }
  function lastKnown(s) { return s.service.state === "unreachable" ? " " + U.pill("Last known") : ""; }
  function scopeLabel(s) { return s.service.target === "remote" ? "Core on " + s.service.host : "Core on this Mac"; }

  /* Service-backed controls say why they are unavailable instead of going grey silently. */
  function unavailable(s) {
    if (s.service.state === "stopped") return U.note("Core is stopped. These values were read the last time it ran; start Core from Maintenance to change them.", "power");
    if (s.service.state === "unreachable") return U.note("Core isn\u2019t reachable right now. Showing the last known values from " + esc(s.service.lastSeen) + ". Nothing has been changed.", "cloud-off", true);
    return "";
  }

  function noWorkspace(s, what) {
    var many = s.workspaces.length > 0;
    return '<div class="sw-stub">' + ico("hard-drive") + "<h2>No workspace selected</h2><p>" + esc(what) +
      (many ? " Choose one of the workspaces this Mac is authorised for." : " Open OmniVia to create or authorise a workspace, then return here.") + "</p>" +
      (many ? '<div style="margin-top:14px">' + U.btn("Choose workspace\u2026", "ov-btn--primary", { act: "ws-choose" }) + "</div>" : "") + "</div>";
  }

  /* ---- General -------------------------------------------------------------- */
  function general(s) {
    var c = s.companion, m = s.mac, M = window.CoreMac, chk = m.checking && !m.observedAt;
    /* Open menu bar at login: registration state, off is neutral */
    var menuSt = chk ? M.status("checking", "Checking\u2026") : m.companionLogin === "pending" ? M.status("checking", "Waiting for macOS to acknowledge") : c.openAtLogin ? M.status("ok", "Registered as a login item") : M.status("off", "Off \u00b7 Optional");
    /* Start Core at login: off | needs approval | not set up | enabled */
    var start = "";
    if (c.startCoreSupported && m.coreStartup !== "unsupported") {
      var st, sub = "", dis = false;
      if (m.coreStartup === "not-set-up") { st = M.status("neutral", "Not set up"); dis = true; sub = m.managed ? "Background startup is managed by your organisation. Nothing to approve here." : "The Core background component isn\u2019t installed on this Mac, so there is nothing to approve. This is not a permission refusal."; }
      else if (chk) st = M.status("checking", "Checking\u2026");
      else if (!c.startCoreAtLogin) st = M.status("off", "Off \u00b7 Optional");
      else if (m.coreStartup === "needs-approval") st = M.status("approval", "Needs approval");
      else if (m.coreStartup === "pending") st = M.status("checking", "Waiting for macOS to acknowledge");
      else st = M.status("ok", "Enabled");
      var fix = (c.startCoreAtLogin && m.coreStartup === "needs-approval") ? M.fix({ id: "g-start-fix", kind: "warn", t: "macOS hasn\u2019t approved Core to start at login", d: "Allow \u201cOmniVia Core\u201d in Login Items. Until then Core starts only when you start it. Whether Core is running now is shown in the sidebar, not here.", acts: M.sysBtn("login-items"), after: M.sysNote(m, "login-items") }) : "";
      start = row({ id: "g-start", tid: "g-start-t", t: "Start Core at login", d: "Starts the Core service itself, separately from the menu bar item." + (sub ? " " + sub : "") + "<br>" + st, c: U.toggle("gen-start", c.startCoreAtLogin && m.coreStartup !== "not-set-up", { by: "g-start-t", disabled: dis }) }) + (fix ? '<div class="sw-row-sub">' + fix + "</div>" : "");
    }
    /* Notifications: preference vs macOS authorisation */
    var n = m.notifications, nSt, nFix = "";
    if (chk || n === "checking") nSt = M.status("checking", "Checking macOS\u2026");
    else if (n === "asking") nSt = M.status("checking", "Asking macOS\u2026");
    else if (!c.notifyAttention) nSt = M.status("off", "Off \u00b7 Optional");
    else if (n === "allowed") nSt = M.status("ok", "Allowed in macOS");
    else if (n === "unknown") nSt = M.status("unknown", "macOS status not checked");
    else if (n === "not-requested") { nSt = M.status("approval", "Needs your permission"); nFix = M.fix({ id: "g-notify-fix", t: "macOS hasn\u2019t been asked yet", d: "Allowing lets OmniVia Core alert you when something needs attention. macOS will ask once; you can change it later in System Settings.", acts: U.btn("Allow notifications", "ov-btn--primary", { act: "notif-allow" }) }); }
    else if (n === "denied") { nSt = M.status("attention", "Blocked in macOS"); nFix = M.fix({ id: "g-notify-fix", kind: "warn", t: "Notifications are turned off for OmniVia Core in macOS", d: "Your preference is on, but macOS won\u2019t show alerts. Turn them on in System Settings; Core won\u2019t ask again on its own.", acts: M.sysBtn("notifications"), after: M.sysNote(m, "notifications") }); }
    else if (n === "limited") { nSt = M.status("attention", "Alerts off \u00b7 badges allowed"); nFix = M.fix({ id: "g-notify-fix", kind: "warn", t: "Only badges are allowed", d: "macOS allows OmniVia Core to show a badge but not alerts, so attention items appear quietly. Turn on Alerts to be notified.", acts: M.sysBtn("notifications"), after: M.sysNote(m, "notifications") }); }
    return U.head("General", "Choose how Core behaves on this Mac.") +
      group(null, rows(
        row({ id: "g-menu", tid: "g-menu-t", t: "Open menu bar at login", d: "Keep Core status and settings available when you sign in.<br>" + menuSt, c: U.toggle("gen-menu", c.openAtLogin, { by: "g-menu-t", disabled: m.companionLogin === "pending" }) }) + start +
        row({ id: "g-notify", tid: "g-notify-t", t: "Notify me when Core needs attention", d: "Only for problems you can act on. Routine activity stays quiet.<br>" + nSt, c: U.toggle("gen-notify", c.notifyAttention, { by: "g-notify-t" }) }) + (nFix ? '<div class="sw-row-sub">' + nFix + "</div>" : "") +
        row({ tid: "g-app", t: "Appearance", d: "Applies to this window and the menu bar item.", c: U.seg("gen-appearance", [["system", "System"], ["light", "Light"], ["dark", "Dark"]], c.appearance, { by: "g-app" }) })
      )) + macSummary(s) +
      U.note("Closing this window does not stop Core.", "info");
  }

  /* One lightweight summary; independent from the sidebar's service status. */
  function macSummary(s) {
    var M = window.CoreMac, m = s.mac, sum = M.summary(s);
    var icon = { ok: "check-circle-2", attention: "alert-triangle", unknown: "circle-dashed", checking: "" }[sum.kind];
    var body = '<div class="cs-sum ' + sum.kind + '">' + (sum.kind === "checking" ? '<span class="cs-sp" aria-hidden="true"></span>' : ico(icon)) + '<div style="flex:1;min-width:0"><div class="sw-row-t">' + esc(sum.t) + "</div>" + (sum.d ? '<div class="sw-row-d">' + esc(sum.d) + "</div>" : "") +
      (sum.list && sum.list.length > 1 ? '<div class="cs-sum-l">' + sum.list.map(function (i) { return '<button data-act="mac-review" data-arg="' + esc(i.id) + '">' + ico("alert-triangle") + esc(i.t) + '<span class="go">Review \u203a</span></button>'; }).join("") + "</div>" : "") + "</div></div>";
    var ctl = (sum.list && sum.list.length === 1 ? U.btn("Review", "ov-btn--bordered", { act: "mac-review", arg: sum.list[0].id }) : "") +
      (m.checking ? U.pending("Checking\u2026") : U.btn("Refresh status", "ov-btn--plain", { act: "mac-refresh", label: "Refresh macOS status (passive)" }));
    return group("macOS access", rows('<div class="sw-row"><div class="sw-row-l">' + body + '</div><div class="sw-row-c">' + ctl + "</div></div>"),
      m.observedAt && !m.checking ? U.note("Last checked " + esc(m.observedAt) + ". Refreshing re-reads status only; it never asks macOS for access or starts anything.", "info") : "");
  }

  /* Unimplemented features render honest stubs, never simulated controls
     (SPEC-CORE-MAC-READINESS-001 MT-007): the native projection declares
     which features exist in this build. */
  function featureInstalled(s, name) {
    return !!(s.features && s.features[name]);
  }
  function featureStub(head, what) {
    return U.head(head, "") + '<div class="sw-stub">' + ico("circle-dashed") +
      "<h2>Not installed</h2><p>" + esc(what) + "</p></div>";
  }

  /* ---- Data ----------------------------------------------------------------- */
  function wsContext(s, ws) {
    var pick = s.workspaces.length > 1
      ? U.select("ws-select", s.workspaces.map(function (w) { return [w.id, w.name]; }), ws.id, { label: "Workspace" })
      : '<span class="cs-val">' + esc(ws.name) + "</span>";
    return group(null, rows(row({ t: "Workspace", d: "Managed here: " + esc(scopeLabel(s)) + ". Other applications keep their own workspace bindings.", c: pick })));
  }

  function data(s) {
    if (!featureInstalled(s, "backup")) return featureStub("Data", "No backup feature is installed in this build. When it exists, its storage and backup controls appear here.");
    var ws = F.ws();
    var h = U.head("Data", "See where your workspace is stored and keep it backed up.");
    if (!ws) return h + noWorkspace(s, "Storage and backups belong to a workspace.");
    var b = s.backup, isLive = live();
    var pathCtl = U.path(ws.path) + U.btn("Copy", "ov-btn--plain", { act: "path-copy", arg: ws.path, label: "Copy storage location" }) +
      (!remote() ? U.btn("Show in Finder", "ov-btn--bordered", { act: "path-finder", arg: ws.path, disabled: !isLive }) : "");
    var storage = group("Storage", rows(
      row({ t: "Location", c: '<span class="cs-val">' + esc(s.service.host) + "</span>" }) +
      row({ t: "Storage location", d: "Reported by Core. Shown for reference; it can\u2019t be edited here.", c: pathCtl }) +
      row({ t: "Storage used", c: '<span class="cs-val">' + esc(ws.total) + "</span>" + lastKnown(s) })
    ), U.disc("storage-details", "Storage details", rows(ws.breakdown.map(function (p) { return row({ t: esc(p[0]), c: '<span class="cs-val">' + esc(p[1]) + "</span>" }); }).join(""))));

    var lastTxt = b.last ? esc(b.last.at) + " \u00b7 " + (b.last.verified ? "Verified" : "Not yet verified") : "No backup yet";
    var ctl;
    if (b.busy === "backing") ctl = U.pending("Backing up\u2026");
    else if (b.busy === "verifying") ctl = U.pending("Verifying\u2026");
    else if (b.failure) ctl = U.btn("Try again", "ov-btn--primary", { act: "backup-now", disabled: !isLive });
    else ctl = U.btn("Back up now", "ov-btn--primary", { act: "backup-now", disabled: !isLive });
    var draft = V.backupDraft || b.opts;
    var dirty = !!V.backupDraft && JSON.stringify(V.backupDraft) !== JSON.stringify(b.opts);
    var opts = U.disc("backup-options", "Options", rows(
      row({ t: "Destination", c: U.select("bk-opt-dest", [["Backups (external) \u203a OmniVia", "Backups (external) \u203a OmniVia"], ["iCloud Drive \u203a OmniVia", "iCloud Drive \u203a OmniVia"]], draft.dest, { label: "Backup destination", disabled: !isLive }) }) +
      row({ t: "Schedule", c: U.select("bk-opt-schedule", [["daily", "Daily"], ["weekly", "Weekly"], ["manual", "Only when I choose"]], draft.schedule, { label: "Backup schedule", disabled: !isLive }) }) +
      row({ t: "Keep backups for", c: U.select("bk-opt-retention", [["7", "7 days"], ["30", "30 days"], ["90", "90 days"]], draft.retention, { label: "Backup retention", disabled: !isLive }) }) +
      row({ t: "Destination access", d: backupDestStatus(s), c: s.mac.backupDest.state === "checking" ? U.pending("Testing\u2026") : U.btn("Test backup access", "ov-btn--bordered", { act: "bk-test", disabled: !isLive }) })
    ) + (dirty ? '<div class="cs-save">' + U.btn("Discard", "ov-btn--plain", { act: "bk-discard" }) + U.btn("Save changes", "ov-btn--primary", { act: "bk-save" }) + "</div>" : ""));
    var backups = group("Backups", rows(
      row({ t: "Last successful backup", d: b.last ? "To " + esc(b.last.dest) : "Back up this workspace to keep a copy outside Core.", c: '<span class="cs-val">' + lastTxt + "</span>" }) +
      row({ id: "d-backup", t: b.failure ? "Backup failed" : "Back up", d: b.failure ? esc(b.failure.at) + " \u2014 " + esc(b.failure.reason) + " Your previous backup is unchanged." : "Creates a new backup at the destination, then verifies it.", c: ctl,
        err: (b.lastError || null) }) + backupDestFix(s)
    ), opts);
    return h + wsContext(s, ws) + storage + backups + unavailable(s);
  }

  /* Destination access, distinct from whether a backup succeeded. */
  function backupDestStatus(s) {
    var M = window.CoreMac, d = s.mac.backupDest;
    if (d.state === "checking") return M.status("checking", "Checking\u2026");
    if (d.state === "ok") return M.status("ok", "Read and write access checked " + (d.at || ""));
    if (d.state === "absent") return M.status("attention", "Destination unavailable \u00b7 last seen " + d.at);
    if (d.state === "read-only") return M.status("attention", "Read only \u00b7 can\u2019t write new backups");
    return M.status("unknown", "Not checked") + " " + '<span class="sw-row-d" style="display:inline">Testing may create and remove a small temporary file at the destination.</span>';
  }
  function backupDestFix(s) {
    var M = window.CoreMac, m = s.mac, d = m.backupDest;
    if (d.state !== "absent" && d.state !== "read-only") return "";
    var absent = d.state === "absent";
    return '<div class="sw-row-sub">' + M.fix({ id: "d-backup-fix", kind: "warn", t: absent ? "Backup destination unavailable" : "Backup destination is read-only",
      d: absent ? "Core could not reach the selected drive. If it\u2019s unplugged, reconnect it and test again. Your last successful backup remains available in the history." : "Core can read earlier backups there but can\u2019t write new ones. Choose a writable destination or change the drive\u2019s permissions. Your last successful backup is unchanged.",
      acts: U.btn("Choose destination again", "ov-btn--bordered", { act: "bk-rechoose" }) + U.btn("Test backup access", "ov-btn--plain", { act: "bk-test" }) }) + "</div>";
  }

  /* ---- Processing ----------------------------------------------------------- */
  function activityText(p, s) {
    if (s.service.state === "stopped") return ["Core is stopped", "Search and processing are unavailable until it starts."];
    if (s.service.state === "unreachable") return ["Processing state unknown", "Core isn\u2019t reachable. Last known: up to date at " + s.service.lastSeen + "."];
    if (p.pausePending) return ["Finishing the current item", "Processing will pause after it. Search remains available."];
    if (p.resumePending) return ["Resuming\u2026", ""];
    if (p.activity === "paused") return ["Processing paused" + (p.waiting ? " \u00b7 " + p.waiting + " items waiting" : ""), "Search remains available while background processing is paused."];
    if (p.activity === "waiting") return [p.waiting + " items waiting", "Processing in the background."];
    return ["Up to date", "All approved sources are processed."];
  }
  function processing(s) {
    if (!featureInstalled(s, "sources")) return featureStub("Processing", "No source feature is installed in this build. When it exists, source and processing controls appear here.");
    var ws = F.ws(), p = s.processing, isLive = live();
    var h = U.head("Processing", "Control how Core processes the information you have added.");
    if (!ws) return h + noWorkspace(s, "Processing applies to the sources of a workspace.");
    var files = p.sources.reduce(function (n, x) { return n + parseInt(x[2].replace(/,/g, ""), 10); }, 0).toLocaleString();
    var srcList = V.sources ? rows(p.sources.map(function (x) { return sourceRow(s, x); }).join("")) +
      '<div style="padding:0 0 12px">' + U.note("Sources are approved in OmniVia. Processing a source does not make its content trusted knowledge. A folder check asks macOS for access if needed and does not import or index anything.", "info") + "</div>" : "";
    var main = group(null, rows(
      row({ tid: "p-auto", t: "Process changes automatically", d: "Process updates from sources you have allowed.", c: U.toggle("proc-auto", p.auto, { by: "p-auto", disabled: !isLive }) }) +
      row({ t: "Sources", d: p.sources.length + " approved sources \u00b7 " + files + " files \u00b7 Workspace: " + esc(ws.name), c: U.btn(V.sources ? "Hide sources" : "Manage sources", "ov-btn--bordered", { act: "proc-sources" }) }) +
      (V.sources ? '<div class="sw-row-sub">' + srcList + "</div>" : "") +
      (p.profilesSupported ? row({ tid: "p-res", t: "Resource use", d: "How much of this Mac Core may use while processing.", c: U.seg("proc-profile", [["low", "Low impact"], ["balanced", "Balanced"], ["high", "High throughput"]], p.profile, { by: "p-res", disabled: !isLive }) }) : "") +
      (p.battery.supported ? row({ tid: "p-batt", t: "Pause heavy processing on battery", d: "Light updates continue; extraction and indexing wait for power.", c: U.toggle("proc-battery", p.battery.pause, { by: "p-batt", disabled: !isLive }) }) : "")
    ));
    var a = activityText(p, s), ctl = "";
    if (isLive) {
      if (p.pausePending || p.resumePending) ctl = U.pending(p.pausePending ? "Pausing\u2026" : "Resuming\u2026");
      else if (p.activity === "paused") ctl = U.btn("Resume processing", "ov-btn--bordered", { act: "proc-resume" });
      else ctl = U.btn("Pause processing", "ov-btn--bordered", { act: "proc-pause" });
    }
    var act = group("Activity", rows(row({ t: esc(a[0]), d: esc(a[1]), c: ctl })),
      U.disc("proc-details", "Processing details", rows(p.deps.map(function (d) { return row({ t: esc(d[0]), c: '<span class="cs-val">' + esc(d[1]) + "</span>" }); }).join("")) +
        U.note("Everything runs on " + esc(s.service.host) + ". No workspace content is sent to an external service.", "shield-check")));
    return h + main + act + unavailable(s);
  }

  function sourceRow(s, x) {
    var M = window.CoreMac, m = s.mac, r = m.sources[x[0]] || { state: "not-checked" }, st, ctl = "", fix = "";
    var chk = (m.checking && !m.observedAt);
    if (r.state === "checking" || chk) { st = M.status("checking", "Checking\u2026"); }
    else if (r.state === "ok") { st = M.status("ok", "Access checked " + r.at); }
    else if (r.state === "companion-only") { st = M.status("unknown", "Companion can read it \u00b7 Core\u2019s reader not verified"); ctl = U.btn("Check folder access", "ov-btn--bordered", { act: "src-check", arg: x[0] }); }
    else if (r.state === "attention") { st = M.status("attention", "Needs attention \u00b7 last read " + r.at);
      fix = M.fix({ id: "src-" + x[0] + "-fix", kind: "warn", t: x[0] + " folder needs attention", d: esc(r.why || "Core could not access this source.") + " Choose the folder again first; if that doesn\u2019t help, review its macOS access settings. Processing of other sources continues.",
        acts: U.btn("Choose folder again", "ov-btn--primary", { act: "src-rechoose", arg: x[0] }) + M.sysBtn("files") + U.btn("Check folder access", "ov-btn--plain", { act: "src-check", arg: x[0] }), after: M.sysNote(m, "files") }); }
    else { st = M.status("unknown", "Access not checked"); ctl = U.btn("Check folder access", "ov-btn--bordered", { act: "src-check", arg: x[0] }); }
    return row({ id: "src-" + x[0], t: esc(x[0]) + ' <span class="cs-val" style="font-weight:400;color:var(--ov-text-tertiary)">' + esc(x[2]) + "</span>", d: '<span class="cs-val mono" style="text-align:left">' + esc(x[1]) + "</span><br>" + st, c: ctl }) + (fix ? '<div class="sw-row-sub">' + fix + "</div>" : "");
  }

  /* ---- Access --------------------------------------------------------------- */
  var LEVELS = [["read", "Read only"], ["contribute", "Read and contribute"]];
  function levelName(l) { return l === "contribute" ? "Read and contribute" : "Read only"; }
  function access(s) {
    if (!featureInstalled(s, "connections")) return featureStub("Access", "No connection feature is installed in this build. When it exists, connection recovery appears here.");
    var ws = F.ws(), A = s.access, isLive = live(), can = A.canManage && isLive;
    var h = U.head("Access", "Choose which applications can use this workspace.", U.btn("Add application\u2026", "ov-btn--bordered", { act: "acc-add", disabled: !can || !ws, icon: "plus" }));
    if (!ws) return h + noWorkspace(s, "Access is granted per workspace.");
    var list = A.apps.map(function (a) {
      var openD = V.app === a.id;
      var ctl = a.pending ? U.pending(a.pending) : U.select("acc-level", LEVELS, a.level, { arg: a.id, label: "Access level for " + a.name, disabled: !can });
      var det = "";
      if (openD) {
        var conn = a.connected === null ? "Unknown \u2014 Core unreachable" : a.connected ? "Connected now" : "Not connected";
        det = '<div class="cs-app-d">' + U.dl([["Connection", U.health(conn, a.connected === null ? "neu" : a.connected ? "ok" : "off")], ["Last activity", esc(a.lastActivity)], ["Application type", esc(a.host)], ["Workspace", esc(ws.name)], ["Access", esc(levelName(a.level)) + '<span class="q">' + (a.level === "contribute" ? "Can also submit evidence and proposed memory. It cannot approve its own contributions as trusted knowledge." : "Can search and retrieve information it is allowed to access.") + "</span>"]]) +
          '<div class="cs-app-acts">' + (a.level === "contribute" ? U.btn("Remove contribution permission", "ov-btn--bordered", { act: "acc-demote", arg: a.id, disabled: !can }) : "") +
          U.btn("Revoke access\u2026", "ov-btn--bordered ov-btn--danger-text", { act: "acc-revoke", arg: a.id, disabled: !can }) + "</div></div>";
      }
      return '<div class="cs-app">' + row({ t: esc(a.name), d: esc(a.host) + " \u00b7 " + esc(levelName(a.level)), err: V.appErr[a.id] || null,
        c: ctl + U.btn(openD ? "Hide details" : "Details", "ov-btn--plain", { act: "acc-details", arg: a.id }) }) + det + "</div>";
    }).join("");
    var restricted = !A.canManage ? U.note("You can view access for <b>" + esc(ws.name) + "</b> but not change it. Access is managed by the workspace owner in OmniVia; ask them to grant you management of this workspace.", "shield", true) : "";
    var conn = U.disc("conn-details", "Connection details", rows(
      row({ t: "Transport", c: '<span class="cs-val">' + esc(remote() ? "Local network (" + s.service.host + ")" : A.transport.kind) + "</span>" }) +
      row({ t: "Reachable from", c: '<span class="cs-val">' + (remote() ? "This network" : "This Mac only") + "</span>" }) +
      (remote() ? remoteRow(s) : row({ t: "Socket", c: U.path(A.transport.path) + U.btn("Copy", "ov-btn--plain", { act: "path-copy", arg: A.transport.path, label: "Copy socket path" }) }))
    ) + (remote() ? "" : U.note("Connections are local to this Mac. Core is not exposed to other devices or the internet.", "shield-check")));
    return h + group("Connected applications", rows(list) + U.note("Configured access is not a live connection. New applications start as Read only.", "info"), restricted) + group(null, "", conn) + unavailable(s);
  }

  /* Remote-only: connection evidence, kept apart from workspace permissions and sign-in. */
  function remoteRow(s) {
    var M = window.CoreMac, m = s.mac, r = m.remote, st, fix = "";
    if (r.testing) st = M.status("checking", "Testing connection\u2026");
    else if (r.lastTest === "ok") st = M.status("ok", "Connected \u00b7 tested just now");
    else if (r.lastTest === "timeout") { st = M.status("attention", "Could not connect"); fix = M.fix({ kind: "warn", t: "Could not connect to " + s.service.host, d: "The connection timed out. This is a network problem, not necessarily a permission one: check the Mac is awake and on the same network, then test again.", acts: U.btn("Test connection", "ov-btn--bordered", { act: "remote-test" }) }); }
    else if (r.lastTest === "refused") { st = M.status("attention", "Local Network access refused"); fix = M.fix({ kind: "warn", t: "macOS is blocking local network access for OmniVia Core", d: "macOS reported that Local Network access is not allowed for this app. Allow it, then test again. Signing in to " + esc(s.service.host) + " is a separate step and isn\u2019t affected.", acts: M.sysBtn("local-network") + U.btn("Test connection", "ov-btn--plain", { act: "remote-test" }), after: M.sysNote(m, "local-network") }); }
    else st = M.status("unknown", "Not tested");
    return row({ t: "Connection", d: st + (r.lastTest ? "" : "<br>Testing opens one connection to " + esc(s.service.host) + " and nothing else."), c: r.testing || fix ? "" : U.btn("Test connection", "ov-btn--bordered", { act: "remote-test" }) }) + (fix ? '<div class="sw-row-sub">' + fix + "</div>" : "");
  }

  /* ---- Maintenance ---------------------------------------------------------- */
  function maintenance(s) {
    var sv = s.service, m = s.maintenance, isLocal = sv.target === "local";
    var stateTxt = { running: ["Running", "ok"], stopped: ["Stopped", "off"], unreachable: ["Unreachable", "attn"], restarting: ["Restarting\u2026", "test"], starting: ["Starting\u2026", "test"] }[sv.state] || ["Unknown", "neu"];
    var life = "";
    if (isLocal) {
      if (sv.state === "running") life = U.btn("Restart Core\u2026", "ov-btn--bordered", { act: "core-restart" });
      else if (sv.state === "stopped") life = sv.canStart ? U.btn("Start Core", "ov-btn--primary", { act: "core-start" }) : "";
      else if (sv.state === "restarting" || sv.state === "starting") life = U.pending(stateTxt[0]);
      else life = U.btn("Retry connection", "ov-btn--bordered", { act: "core-retry" });
    }
    var core = group("Core", rows(
      row({ t: "Service", d: isLocal ? "Runs on this Mac. Closing this window does not stop it." : "Runs on " + esc(sv.host) + ". Start and stop it there.", c: U.health(stateTxt[0], stateTxt[1]) + life }) +
      row({ t: "Core version", c: '<span class="cs-val mono">' + esc(sv.version) + "</span>" }) +
      row({ t: "Companion version", c: '<span class="cs-val mono">' + esc(sv.companion) + "</span>" }) +
      row({ t: "Updates", d: sv.updates === "installer" ? "Installed with the OmniVia installer." + (sv.lastChecked ? " Up to date \u00b7 checked " + esc(sv.lastChecked) + "." : "") : "Managed by your installation. No updater is available here.",
        c: sv.updates === "installer" ? (sv.checking ? U.pending("Checking\u2026") : U.btn("Check for updates", "ov-btn--bordered", { act: "core-updates" })) : "" })
    ));
    var diag = group("Diagnostics", rows(
      row({ t: "Logs", d: "Recent Core and companion events on this Mac. Workspace content is never logged.", c: U.btn("View logs", "ov-btn--bordered", { act: "diag-logs" }) }) +
      row({ t: "Diagnostics bundle", d: m.exportResult ? esc(m.exportResult) : "Collect a bundle to share with support. You review what it includes first.", c: U.btn("Export diagnostics\u2026", "ov-btn--bordered", { act: "diag-export" }) }) +
      (s.mac.shareExt.installed ? row({ t: "Share to Core extension", d: "Installed. " + (s.mac.shareExt.verified ? "Seen in a supported Share menu." : "Not yet seen in a supported Share menu \u2014 it may need to be turned on.") + "<br>" + window.CoreMac.status(s.mac.shareExt.verified ? "ok" : "unknown", s.mac.shareExt.verified ? "Verified" : "Installed \u00b7 not verified"), c: s.mac.shareExt.verified ? "" : window.CoreMac.sysBtn("sharing") }) + (window.CoreMac.sysNote(s.mac, "sharing") ? '<div class="sw-row-sub">' + window.CoreMac.sysNote(s.mac, "sharing") + "</div>" : "") : "")
    ), U.disc("mac-checks", "macOS check details", macChecks(s)));
    var rb = m.rebuild, rbCtl, rbD = "Rebuilds the search and embedding indexes from workspace records. Records themselves are not changed or deleted.";
    if (rb && rb.state === "running") { rbCtl = U.pending("Rebuilding\u2026 " + rb.pct + "%"); rbD += '<div class="cs-progress" role="progressbar" aria-valuenow="' + rb.pct + '" aria-valuemin="0" aria-valuemax="100"><i style="width:' + rb.pct + '%"></i></div>'; }
    else if (rb && rb.state === "failed") { rbCtl = U.btn("Try again", "ov-btn--primary", { act: "maint-rebuild-go" }); rbD = "The rebuild stopped at " + rb.pct + "%. The previous indexes are still in use; search keeps working."; }
    else { rbCtl = U.btn("Rebuild search indexes\u2026", "ov-btn--bordered", { act: "maint-rebuild", disabled: !live() }); if (m.lastRebuild) rbD += " Last rebuilt " + esc(m.lastRebuild) + "."; }
    var opts = group(null, "", U.disc("maint-options", "Maintenance options", rows(row({ t: "Search indexes", d: rbD, c: rbCtl }))));
    var notInstalled = group("Service & diagnostics", rows(
      row({ t: "Not available in this build", d: "Service controls, versions, diagnostics export and index rebuild arrive with the qualified Core service integration. Service status lives in the menu bar." })
    ));
    return U.head("Maintenance", "Check Core health and resolve problems.") + notInstalled + diag + U.disc("mac-checks", "macOS check details", macChecks(s));
  }

  function macChecks(s) {
    var m = s.mac, c = s.companion, when = m.observedAt || "not this session", cur = m.observedAt ? "Current" : "Last observed";
    var L = [
      ["Companion login item", "Companion", "Registration state (SMAppService)", c.openAtLogin ? "Registered" : "Not registered"],
      ["Core background startup", "Core installer", "Login item status", { off: "Not chosen", "needs-approval": "Requires approval", "not-set-up": "Component not installed", enabled: "Enabled", unsupported: "Not applicable (remote Core)" }[m.coreStartup] || m.coreStartup],
      ["Notifications", "Companion", "Notification authorisation", { allowed: "Authorised", denied: "Denied", limited: "Provisional: badges only", "not-requested": "Not determined", unknown: "Not read", checking: "Reading\u2026", asking: "Prompt in progress" }[m.notifications] || m.notifications]
    ];
    if (F.ws() && featureInstalled(s, "sources")) {
      s.processing.sources.forEach(function (x) { var r = m.sources[x[0]] || { state: "not-checked" }; L.push(["Source: " + x[0], "Core reader", "Last read of bookmarked folder", { ok: "Readable at " + r.at, attention: "Read failed at " + r.at, "companion-only": "Companion readable; Core unverified", "not-checked": "Not checked", checking: "Checking\u2026" }[r.state]]); });
    }
    if (F.ws() && featureInstalled(s, "backup")) {
      L.push(["Backup destination", "Core backup", "Read and write test", { ok: "Read/write at " + m.backupDest.at, absent: "Unreachable since " + m.backupDest.at, "read-only": "Read only", "not-checked": "Not checked", checking: "Testing\u2026" }[m.backupDest.state]]);
    }
    if (m.shareExt.installed) L.push(["Share to Core", "Share extension", "Installed; appearance in Share menu", m.shareExt.verified ? "Verified" : "Installed, not verified"]);
    return '<div class="cs-mono-tbl">' + U.dl(L.map(function (r) { return [r[0], esc(r[3]) + '<span class="q">' + esc(r[1]) + " \u00b7 " + esc(r[2]) + " \u00b7 " + esc(cur) + ": " + esc(when) + "</span>"]; })) + "</div>" +
      U.note("Paths, bookmark data and credentials are not shown here and are excluded from diagnostics exports. Local Network and Firewall appear only for a local-network connection.", "info");
  }

  window.CorePanes = { general: general, data: data, processing: processing, access: access, maintenance: maintenance, V: V, LEVELS: LEVELS, levelName: levelName };
})();
