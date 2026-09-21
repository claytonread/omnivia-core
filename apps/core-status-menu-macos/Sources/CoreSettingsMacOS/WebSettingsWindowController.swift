// ============================================================================
// OmniVia Core Settings — the web-hosted settings window (web-host amendment
// v1.0). Native AppKit window + WKWebView hosting the shared settings bundle,
// with the SettingsBridge adapter: native readiness state projects into the
// page; page actions enter only through the allowlisted typed channel.
// Closing it never stops Core (MT-001).
// ============================================================================

import AppKit
import Combine
import UserNotifications
import WebKit

@MainActor
public final class WebSettingsWindowController: NSObject, NSWindowDelegate {
    /// One window per companion (MR-001).
    private var window: NSWindow?
    private var webView: WKWebView?

    private let coordinator: ReadinessCoordinator
    private let navigator: SettingsNavigator
    private let bridge = SettingsBridge()
    private var cancellables: Set<AnyCancellable> = []

    /// Core preferences mirrored from the companion's own preference owner.
    public var startCoreAtLoginSelected = false
    public var attentionNotificationsSelected = false
    public var hasWorkspace = false

    public init(
        coordinator: ReadinessCoordinator,
        navigator: SettingsNavigator = SettingsNavigator(componentName: "OmniVia Core"),
        notificationCenter: UNUserNotificationCenter? = nil
    ) {
        self.coordinator = coordinator
        self.navigator = navigator
        if let notificationCenter {
            self.notificationCenter = notificationCenter
        } else if Bundle.main.bundleIdentifier != nil {
            self.notificationCenter = UNUserNotificationCenter.current()
        } else {
            self.notificationCenter = nil
        }
        super.init()
        coordinator.$snapshot.sink { [weak self] _ in self?.pushStatus() }.store(in: &cancellables)
        coordinator.$refreshInFlight.sink { [weak self] _ in self?.pushStatus() }.store(in: &cancellables)
    }

    private var notificationCenter: UNUserNotificationCenter?

    public func openOrFocus() {
        if let window {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            coordinator.scheduleRefresh(.foregroundReturn)
            return
        }
        let window = buildWindow()
        self.window = window
        window.delegate = self
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        coordinator.scheduleRefresh(.windowOpened)
    }

    public func windowWillClose(_ notification: Notification) {
        // Cancel UI-owned passive work; Core is untouched (MT-001, §8.3).
        window = nil
        webView = nil
    }

    private func buildWindow() -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 640),
            styleMask: [.titled, .closable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Core Settings"
        window.appearance = NSAppearance(named: .darkAqua)
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.backgroundColor = Ov.bgContent
        window.isReleasedWhenClosed = false

        let configuration = WKWebViewConfiguration()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.setValue(false, forKey: "drawsBackground")
        webView.navigationDelegate = self
        self.webView = webView

        // The adapter is the entire native surface the page can reach.
        bridge.onClose = { [weak self] in
            self?.window?.performClose(nil)
        }
        bridge.install(
            in: webView,
            coordinator: coordinator,
            navigator: navigator,
            notificationCenter: notificationCenter
        )

        window.contentView = webView
        loadBundle(into: webView)
        positionWindowControls()
        return window
    }

    /// The page's .cs-traffic close control (positioned by .cs-head CSS) is
    /// the window's close button; the native titlebar buttons are hidden so
    /// there is exactly one control, aligned with the brand row by CSS.
    private func positionWindowControls() {
        guard let window else { return }
        window.standardWindowButton(.closeButton)?.isHidden = true
        window.standardWindowButton(.miniaturizeButton)?.isHidden = true
        window.standardWindowButton(.zoomButton)?.isHidden = true
    }

    public func windowDidResize(_ notification: Notification) {
        positionWindowControls()
    }

    private func loadBundle(into webView: WKWebView) {
        // SPM .copy lands the package at <bundle>/Resources/prototype.
        let base = Bundle.module.resourceURL!
            .appendingPathComponent("prototype", isDirectory: true)
        let index = base.appendingPathComponent("Core-Settings.html")
        webView.loadFileURL(index, allowingReadAccessTo: base)
    }

    /// Push the current readiness state into the page through the bridge.
    private func pushStatus() {
        let preferences = ReadinessPreferences(
            startCoreAtLoginSelected: startCoreAtLoginSelected,
            attentionNotificationsSelected: attentionNotificationsSelected,
            hasWorkspace: hasWorkspace
        )
        let summary = ReadinessReducer.summarize(
            checks: coordinator.snapshot.checks,
            preferences: preferences,
            refreshInFlight: coordinator.refreshInFlight,
            everObserved: coordinator.snapshot.observedAt != nil
        )
        bridge.push(snapshot: coordinator.snapshot, summary: summary, preferences: preferences)
    }
}

extension WebSettingsWindowController: WKNavigationDelegate {
    public func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // The page is up; deliver the first projection.
        pushStatus()
        // Developer-harness verification: confirm the projection landed in the
        // page's state model.
        webView.evaluateJavaScript(
            "window.CoreFx ? JSON.stringify({startup: window.CoreFx.get().mac.coreStartup, notifications: window.CoreFx.get().mac.notifications}) : 'no CoreFx'"
        ) { result, _ in
            NSLog("projected state: %@", result as? String ?? "(none)")
        }
    }
}
