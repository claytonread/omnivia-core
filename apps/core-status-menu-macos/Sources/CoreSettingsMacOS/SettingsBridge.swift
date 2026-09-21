// ============================================================================
// OmniVia Core Settings — the native/web adapter (web-host amendment v1.0).
//
// The settings bundle (the shared web UI) is presentation only. This bridge is
// the entire native surface it can reach:
//
//   native → page: `statusProjection` JSON via CoreNativeBridge.applyStatus
//   page → native: typed action requests from the frozen allowlist; opaque
//                  arguments only; every request is rate-limited and anything
//                  outside the allowlist is logged and ignored. Page script
//                  cannot execute a native action, acquire a permission, open
//                  an arbitrary URL, or pass a path, URL or shell fragment
//                  (MR-035, §11.1, §13.1).
// ============================================================================

import Foundation
import UserNotifications
import WebKit

/// The frozen allowlist of page-requestable actions (amendment v1.0).
enum BridgeAction: String, CaseIterable {
    case refreshStatus
    case openLoginItems
    case openNotifications
    case requestNotifications
}

struct SettingsStatusProjection: Encodable {
    struct Summary: Encodable {
        let kind: String
        let headline: String
        let detail: String?
    }

    struct Check: Encodable {
        let checkId: String
        let statusWord: String
        let tone: String
        let allowedActions: [String]
    }

    let type = "statusProjection"
    let schemaVersion = 1
    let generation: Int
    let observedAt: String?
    let summary: Summary
    let checks: [Check]
}

@MainActor
final class SettingsBridge: NSObject, WKScriptMessageHandler {
    private weak var coordinator: ReadinessCoordinator?
    private var navigator: SettingsNavigator?
    private weak var notificationCenter: UNUserNotificationCenter?
    private var lastActionAt: [String: Date] = [:]
    /// Deliver status through an injected bridge script; nil until installed.
    private weak var webView: WKWebView?

    func install(in webView: WKWebView, coordinator: ReadinessCoordinator,
                 navigator: SettingsNavigator, notificationCenter: UNUserNotificationCenter?) {
        self.webView = webView
        self.coordinator = coordinator
        self.navigator = navigator
        self.notificationCenter = notificationCenter
        let script = WKUserScript(source: Self.bridgeScript, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        webView.configuration.userContentController.addUserScript(script)
        webView.configuration.userContentController.add(self, name: "native")
    }

    /// Project the current readiness snapshot into the page.
    func push(snapshot: ReadinessSnapshot, summary: ReadinessSummary, preferences: ReadinessPreferences) {
        let checks = snapshot.checks.map { check -> SettingsStatusProjection.Check in
            let (word, tone) = statusPresentation(for: check, preferences: preferences)
            return SettingsStatusProjection.Check(
                checkId: checkIdString(check.checkID),
                statusWord: word,
                tone: tone,
                allowedActions: check.allowedActions.map(actionName)
            )
        }
        let projection = SettingsStatusProjection(
            generation: snapshot.generation,
            observedAt: snapshot.observedAt.map { ISO8601DateFormatter().string(from: $0) },
            summary: .init(kind: summaryKind(summary.kind), headline: summary.headline, detail: summary.detail),
            checks: checks
        )
        guard let data = try? JSONEncoder().encode(projection),
              let json = String(data: data, encoding: .utf8) else { return }
        webView?.evaluateJavaScript(
            "window.CoreNativeBridge && CoreNativeBridge.applyStatus(\(json));",
            completionHandler: nil
        )
    }

    // MARK: - WKScriptMessageHandler (page → native, allowlisted)

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        guard message.name == "native",
              let body = message.body as? [String: Any],
              let rawAction = body["action"] as? String,
              let action = BridgeAction(rawValue: rawAction) else {
            // Anything outside the allowlist is ignored, never improvised.
            return
        }
        // Rate-limit duplicate dispatches from event bursts (§8.2 coalescing
        // spirit): at most one of each action per 300 ms.
        if let last = lastActionAt[rawAction], Date().timeIntervalSince(last) < 0.3 { return }
        lastActionAt[rawAction] = Date()

        switch action {
        case .refreshStatus:
            coordinator?.scheduleRefresh(.explicitRefresh)
        case .openLoginItems:
            Task { @MainActor [navigator] in
                _ = await navigator?.open(.loginItems)
            }
        case .openNotifications:
            Task { @MainActor [navigator] in
                _ = await navigator?.open(.notifications)
            }
        case .requestNotifications:
            requestNotificationsExplicitly()
        }
    }

    /// Explicit notification request (§9.2): only from a trusted interaction,
    /// only when undetermined, followed by a passive re-read — the request
    /// result itself is never treated as a permission grant.
    private func requestNotificationsExplicitly() {
        guard let center = notificationCenter else { return }
        Task { @MainActor [weak self] in
            let settings = await center.notificationSettings()
            if settings.authorizationStatus == .notDetermined {
                _ = try? await center.requestAuthorization(options: [.alert, .sound])
            }
            self?.coordinator?.scheduleRefresh(.explicitRefresh)
        }
    }

    // MARK: - Presentation mapping (§15 wording)

    private func statusPresentation(
        for check: ReadinessCheck,
        preferences: ReadinessPreferences
    ) -> (String, String) {
        switch check.checkID {
        case .coreBackground:
            let row = ReadinessReducer.startupRow(check: check, serviceRunning: nil)
            switch row.registration {
            case "Enabled": return ("Enabled", "ok")
            case "Needs approval": return ("Needs approval", "warn")
            case "Not set up": return ("Off · Optional", "neu")
            default: return ("Not checked", "checking")
            }
        case .notificationsDelivery:
            switch check.observedState {
            case .allowed: return ("Allowed", "ok")
            case .limitedDelivery: return ("Limited — alerts are off", "warn")
            case .denied: return ("Blocked in macOS", "warn")
            case .notDetermined: return ("Not set up", "neu")
            case .unrecognized: return ("Not checked", "neu")
            default: return ("Not checked", "checking")
            }
        default:
            return ("Not checked", "neu")
        }
    }

    private func checkIdString(_ id: ReadinessCheckID) -> String {
        switch id {
        case .companionLogin: return "companion.login"
        case .coreBackground: return "core.background"
        case .notificationsDelivery: return "notifications.delivery"
        case .shareExtension: return "capture.share-extension"
        case .sourceRead(let ref): return "source.read:\(ref)"
        case .backupWrite(let ref): return "backup.write:\(ref)"
        case .connectionLocalNetwork(let ref): return "connection.local-network:\(ref)"
        }
    }

    private func actionName(_ action: ReadinessAction) -> String {
        switch action {
        case .requestNotifications: return "requestNotifications"
        case .openLoginItems: return "openLoginItems"
        case .openNotifications: return "openNotifications"
        case .openSystemSettings, .refreshStatus: return "refreshStatus"
        case .testSourceAccess, .testBackupAccess, .testConnection, .reselectSource:
            return "unsupported"
        }
    }

    private func summaryKind(_ kind: ReadinessSummaryKind) -> String {
        switch kind {
        case .ok: return "ok"
        case .noAdditionalAccess: return "noAdditionalAccess"
        case .attention: return "attention"
        case .needsVerification: return "needsVerification"
        case .checking: return "checking"
        }
    }

    // MARK: - Injected page-side half

    /// Injected page-side half. Defines `window.CoreNativeBridge` (status in,
    /// typed actions out) and maps real projections onto the page's state
    /// model. The page's own CSS and components render the data unchanged —
    /// the prototype's simulated `mac` fixture handlers for bridge-owned acts
    /// are intercepted so the simulated and real flows never mix.
    static let bridgeScript = """
    (function () {
      "use strict";
      var latest = null;
      var listeners = [];
      window.CoreNativeBridge = {
        applyStatus: function (projection) {
          latest = projection;
          mapIntoState(projection);
          listeners.forEach(function (cb) { try { cb(projection); } catch (e) {} });
        },
        latest: function () { return latest; },
        onStatus: function (cb) { listeners.push(cb); }
      };

      /* Map the native status projection onto the page's mac state model.
         The page keeps rendering through its own components — only the data
         source changes. */
      var STARTUP = { "Enabled": "enabled", "Needs approval": "needs-approval", "Off · Optional": "off" };
      var NOTIFY = { "Allowed": "allowed", "Blocked in macOS": "denied",
                     "Limited — alerts are off": "limited", "Not set up": "not-requested",
                     "Not checked": "unknown" };
      function mapIntoState(projection) {
        if (!window.CoreFx || !window.CoreMac) return;
        var s = window.CoreFx.get();
        if (!s || !s.mac) return;
        var byId = {};
        (projection.checks || []).forEach(function (c) { byId[c.checkId] = c; });
        var bg = byId["core.background"], nt = byId["notifications.delivery"];
        if (bg) s.mac.coreStartup = STARTUP[bg.statusWord] || "not-set-up";
        if (nt) s.mac.notifications = NOTIFY[nt.statusWord] || "unknown";
        s.mac.checking = !!projection.summary && projection.summary.kind === "checking";
        s.mac.observedAt = projection.observedAt ? "just now" : null;
        s.mac.pendingForever = false;
        window.CoreFx.emit();
      }

      /* Typed action channel. Bridge-owned acts are intercepted before the
         page's simulated dispatcher sees them; everything else is untouched. */
      var HANDLED = { "mac-refresh": "refreshStatus", "notif-allow": "requestNotifications" };
      var DESTINATIONS = { "login-items": "openLoginItems", "notifications": "openNotifications" };
      document.addEventListener("click", function (event) {
        var el = event.target && event.target.closest ? event.target.closest("[data-act]") : null;
        if (!el) return;
        var act = el.getAttribute("data-act");
        if (act === "sys-open") {
          var mapped = DESTINATIONS[el.getAttribute("data-arg")];
          if (mapped) {
            event.preventDefault();
            event.stopImmediatePropagation();
            window.webkit.messageHandlers.native.postMessage({ action: mapped });
          }
          return;
        }
        var action = HANDLED[act];
        if (action) {
          event.preventDefault();
          event.stopImmediatePropagation();
          window.webkit.messageHandlers.native.postMessage({ action: action });
        }
      }, true);
    })();
    """
}
