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
        // Companion preferences are persisted by their existing owner (§14.1):
        // the notification preference is a real local preference; startup
        // registration has no qualified mechanism, so its state stays off.
        attentionNotificationsSelected = UserDefaults.standard.bool(
            forKey: "omnivia.core.settings.attentionNotifications"
        )
        coordinator.$snapshot.sink { [weak self] _ in self?.pushStatus() }.store(in: &cancellables)
        coordinator.$refreshInFlight.sink { [weak self] _ in self?.pushStatus() }.store(in: &cancellables)
        bridge.actionHandler = { [weak self] action, _ in
            switch action {
            case .toggleAttentionNotifications:
                guard let self else { return }
                self.attentionNotificationsSelected.toggle()
                UserDefaults.standard.set(
                    self.attentionNotificationsSelected,
                    forKey: "omnivia.core.settings.attentionNotifications"
                )
                self.pushStatus()
            case .toggleStartAtLogin:
                // §19 binding: no qualified registration mechanism exists in
                // this build. Decline honestly; never simulate a registration.
                self?.bridge.showToast("Starting Core at login isn't available in this build.")
            default:
                break
            }
        }
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

        let container = NSView(frame: NSRect(x: 0, y: 0, width: 900, height: 640))
        window.contentView = container
        container.addSubview(webView)
        webView.translatesAutoresizingMaskIntoConstraints = false
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

    /// Make the titlebar 60px tall with a titlebar accessory (the public,
    /// supported mechanism), then centre the traffic lights vertically in it
    /// on every layout pass — level with the OmniVia mark. Titlebar layout
    /// keeps re-top-anchoring the lights, so the reassert runs each update.
    private var accessoryInstalled = false

    private func positionWindowControls() {
        guard let window,
              let close = window.standardWindowButton(.closeButton),
              let bar = close.superview else { return }

        if !accessoryInstalled {
            var chain: [String] = []
            var view: NSView? = close
            while let v = view {
                chain.append("\(type(of: v)) \(NSStringFromRect(v.frame)) clip=\(v.clipsToBounds || v.wantsLayer == true && v.layer?.masksToBounds == true)")
                view = v.superview
            }
            NSLog("titlebar chain: %@", chain.joined(separator: " -> "))
            accessoryInstalled = true
        }
        // DEBUG BLOCK ABOVE TEMPORARY
        if !accessoryInstalled {
            let accessoryView = NSView(frame: NSRect(x: 0, y: 0, width: 1, height: 60))
            let accessory = NSTitlebarAccessoryViewController()
            accessory.view = accessoryView
            accessory.layoutAttribute = .right
            window.addTitlebarAccessoryViewController(accessory)
            accessoryInstalled = true
        }

        // The container spans the branded header height: 60px, anchored to the
        // window's top edge (growing it with setFrameSize alone kept the old
        // origin and pushed it above the window). The bar fills the container.
        if let container = bar.superview {
            let contentHeight = window.frame.height
            let target = NSRect(x: 0, y: contentHeight - 60, width: window.frame.width, height: 60)
            if abs(container.frame.minY - target.minY) > 0.5 || abs(container.frame.height - 60) > 0.5 {
                container.setFrameSize(NSSize(width: target.width, height: 60))
                container.setFrameOrigin(NSPoint(x: 0, y: target.minY))
            }
        }
        if bar.frame.height != 60 {
            bar.setFrameSize(NSSize(width: bar.frame.width, height: 60))
        }
        // Centre of the lights: 30pt below the window's top edge (the header's
        // vertical centre). Convert from window coordinates so the bar's own
        // orientation cannot flip the result.
        let windowTop = window.frame.height
        for button in [close,
                       window.standardWindowButton(.miniaturizeButton),
                       window.standardWindowButton(.zoomButton)].compactMap({ $0 }) {
            let centreInWindow = NSPoint(x: button.frame.midX, y: windowTop - 30)
            let centreInBar = bar.convert(centreInWindow, from: nil)
            var frame = button.frame
            frame.origin.y = centreInBar.y - frame.height / 2
            button.setFrameOrigin(frame.origin)
        }
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
