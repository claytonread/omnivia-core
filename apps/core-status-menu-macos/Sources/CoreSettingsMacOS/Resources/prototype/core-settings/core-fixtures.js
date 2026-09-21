/* ============================================================================
   OmniVia Core Settings — fixture state and scenarios.
   Every value below is SYNTHETIC. Service, processing, backup and connection
   states are modelled separately (no single healthy boolean). The window only
   reads this model and requests changes through CoreFx.request(); nothing here
   touches login items, files, services or the network.
   ============================================================================ */
(function () {
  "use strict";

  var WS_PATH = "/Users/sample/Library/Application Support/OmniVia/Workspaces/Studio";

  function base() {
    return {
      service: { state: "running", target: "local", host: "This Mac", version: "0.9.3 (fixture)", companion: "0.9.3 (fixture)", canStart: true, lastSeen: "09:41", updates: "installer", lastChecked: null, checking: false, restarting: false },
      companion: { openAtLogin: true, startCoreAtLogin: false, startCoreSupported: true, startCoreNeedsApproval: false, notifyAttention: true, appearance: "system" },
      workspaces: [{ id: "studio", name: "Studio", path: WS_PATH, total: "2.4 GB",
        breakdown: [["Source content", "1.6 GB"], ["Records", "310 MB"], ["Indexes", "420 MB"], ["Temporary data", "38 MB"]] }],
      selected: "studio",
      backup: { last: { at: "Today, 08:12", dest: "Backups (external) › OmniVia", verified: true }, failure: null, busy: null,
        opts: { dest: "Backups (external) › OmniVia", schedule: "daily", retention: "30" }, optsSupported: true },
      processing: { auto: true, profile: "balanced", profilesSupported: true, battery: { supported: true, pause: true },
        activity: "up-to-date", waiting: 0, pausePending: false, resumePending: false,
        sources: [["Research", "~/Documents/Research", "1,204 files"], ["Meeting notes", "~/Notes/Meetings", "312 files"], ["Reading list", "~/Documents/Reading", "88 files"]],
        deps: [["Search index", "Runs locally"], ["Text extraction", "Runs locally"], ["Semantic embeddings", "Runs locally"]] },
      access: { canManage: true, refuseNext: false, transport: { kind: "Local socket only", path: "~/Library/Application Support/OmniVia/core.sock", remote: false },
        apps: [
          { id: "research", name: "Research assistant", host: "MCP client", level: "read", connected: true, lastActivity: "3 min ago", pending: null, error: null },
          { id: "dev", name: "Development assistant", host: "MCP client", level: "contribute", connected: false, lastActivity: "Yesterday, 17:05", pending: null, error: null }
        ] },
      maintenance: { rebuild: null, lastRebuild: null, exportResult: null },
      log: []
    };
  }

  var SCENARIOS = {
    normal: { label: "Normal", apply: function (s) { return s; } },
    stopped: { label: "Core stopped", apply: function (s) { s.service.state = "stopped"; s.processing.activity = "unavailable"; return s; } },
    "no-workspace": { label: "No workspace", apply: function (s) { s.selected = null; return s; } },
    paused: { label: "Processing paused", apply: function (s) { s.processing.activity = "paused"; s.processing.waiting = 5; return s; } },
    "backup-failed": { label: "Backup failed", apply: function (s) { s.backup.failure = { at: "Today, 08:12", reason: "The backup destination wasn\u2019t available." }; s.backup.last = { at: "Yesterday, 08:10", dest: "Backups (external) \u203a OmniVia", verified: true }; return s; } },
    lost: { label: "Connection lost", apply: function (s) { s.service.state = "unreachable"; s.processing.activity = "unknown"; s.access.apps.forEach(function (a) { a.connected = null; }); return s; } },
    restricted: { label: "Access restricted", apply: function (s) { s.access.canManage = false; return s; } },
    remote: { label: "Remote Core", apply: function (s) { s.service.target = "remote"; s.service.host = "studio-mini.local"; s.companion.startCoreSupported = false; s.processing.battery.supported = false; return s; } },
    "two-ws": { label: "Two workspaces", apply: function (s) { s.workspaces.push({ id: "archive", name: "Archive", path: "/Volumes/Vault/OmniVia/Archive", total: "18.9 GB", breakdown: [["Source content", "17.1 GB"], ["Records", "640 MB"], ["Indexes", "1.1 GB"], ["Temporary data", "12 MB"]] }); return s; } }
  };

  var state = null, scenario = "normal", macId = null, listeners = [];

  function load(name, mac) {
    scenario = SCENARIOS[name] ? name : "normal";
    macId = mac || null;
    state = SCENARIOS[scenario].apply(base());
    if (window.CoreMac) window.CoreMac.apply(state, macId);
    emit();
  }
  function emit() { listeners.forEach(function (f) { f(state); }); }
  function on(f) { listeners.push(f); }
  function log(kind, msg) { state.log.unshift({ t: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }), kind: kind, msg: msg }); if (state.log.length > 40) state.log.pop(); }

  /* A "request" is the only way the UI changes service-backed state. It
     resolves after a short delay so pending states are visible, and it can be
     refused so rejected changes restore the prior value. */
  function request(name, mutate, ms, refuse) {
    log("request", name);
    return new Promise(function (res, rej) {
      setTimeout(function () {
        if (refuse) { log("refused", name + " \u2014 " + refuse); rej(new Error(refuse)); emit(); return; }
        if (mutate) mutate(state);
        log("ok", name);
        emit(); res(state);
      }, ms == null ? 500 : ms);
    });
  }
  /* Companion preferences are local to this Mac and apply immediately. */
  function local(name, mutate) { mutate(state); log("local", name); emit(); }

  function serviceLive() { return state.service.state === "running"; }
  function ws() { for (var i = 0; i < state.workspaces.length; i++) if (state.workspaces[i].id === state.selected) return state.workspaces[i]; return null; }

  window.CoreFx = { load: load, on: on, get: function () { return state; }, request: request, local: local, log: log, emit: emit,
    scenarios: SCENARIOS, scenario: function () { return scenario; }, macId: function () { return macId; }, live: serviceLive, ws: ws };
})();
