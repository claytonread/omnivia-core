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
        bridge.install(
            in: webView,
            coordinator: coordinator,
            navigator: navigator,
            notificationCenter: notificationCenter
        )

        let container = NSView()
        container.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = container
        container.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: container.topAnchor),
            webView.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: container.trailingAnchor),
        ])

        // The header band is the drag region: the web view consumes mouse
        // events, so a transparent overlay above it moves the window (the
        // prototype's `-webkit-app-region: drag` is Electron-only).
        let drag = HeaderDragView()
        drag.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(drag)
        NSLayoutConstraint.activate([
            drag.topAnchor.constraint(equalTo: container.topAnchor),
            drag.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            drag.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            drag.heightAnchor.constraint(equalToConstant: 60),
        ])

        loadBundle(into: webView)
        positionWindowControls()
        return window
    }

    /// Size the native titlebar container to the branded header (60px) and
    /// pin the traffic lights to its vertical centre with constraints, so
    /// they sit level with the OmniVia mark and survive every layout pass.
    private var controlsPinned = false

    private func positionWindowControls() {
        guard let window,
              let closeButton = window.standardWindowButton(.closeButton),
              let titlebarView = closeButton.superview,
              let container = titlebarView.superview else { return }
        let contentHeight = window.contentView?.bounds.height ?? 0
        container.frame = NSRect(x: 0, y: contentHeight - 60, width: window.frame.width, height: 60)

        if controlsPinned { return }
        titlebarView.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            titlebarView.topAnchor.constraint(equalTo: container.topAnchor),
            titlebarView.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            titlebarView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            titlebarView.trailingAnchor.constraint(equalTo: container.trailingAnchor),
        ])
        let miniaturize = window.standardWindowButton(.miniaturizeButton)
        let zoom = window.standardWindowButton(.zoomButton)
        for button in [closeButton, miniaturize, zoom].compactMap({ $0 }) {
            button.translatesAutoresizingMaskIntoConstraints = false
            NSLayoutConstraint.activate([
                button.centerYAnchor.constraint(equalTo: titlebarView.centerYAnchor),
            ])
        }
        // Standard 8px spacing between the lights, leading at the header's
        // traffic zone (x16).
        NSLayoutConstraint.activate([
            closeButton.leadingAnchor.constraint(equalTo: titlebarView.leadingAnchor, constant: 16),
            miniaturize!.leadingAnchor.constraint(equalTo: closeButton.trailingAnchor, constant: 8),
            zoom!.leadingAnchor.constraint(equalTo: miniaturize!.trailingAnchor, constant: 8),
        ])
        controlsPinned = true
    }

    public func windowDidResize(_ notification: Notification) {
        positionWindowControls()
    }

    public func windowDidBecomeKey(_ notification: Notification) {
        positionWindowControls()
    }

    /// Titlebar layout re-runs on many window updates; reassert the 60px
    /// container on each one so the traffic lights never drift back up.
    public func windowDidUpdate(_ notification: Notification) {
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

// MARK: - Header drag region

/// A transparent overlay across the header band. The web view consumes mouse
/// events, so without this the window cannot be moved by dragging the title
/// bar. Native traffic lights sit above it (titlebar container layer), so
/// they keep working.
final class HeaderDragView: NSView {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        window?.performDrag(with: event)
    }

    override func mouseDragged(with event: NSEvent) {
        window?.performDrag(with: event)
    }
}
