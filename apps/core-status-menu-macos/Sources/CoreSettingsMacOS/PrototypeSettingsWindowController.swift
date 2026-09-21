// ============================================================================
// OmniVia Core Settings — prototype host (SPEC-CORE-MAC-READINESS-001 §15).
//
// Hosts the Core Settings design package (I01 + amendment v1.1, the exact
// HTML/CSS/JS prototype) in a WKWebView inside the native companion's owned
// window, so the screen renders with full design fidelity. The native
// readiness layer (ReadinessCoordinator, providers, navigator) remains the
// authority: the web surface is presentation only, and no native check,
// permission request, or lifecycle action can originate from page script —
// native actions enter through an explicit message bridge added when the
// qualified native reads are wired (Phase 2+). All prototype data is the
// package's own synthetic fixtures; nothing here touches login items, files,
// services or the network.
// ============================================================================

import AppKit
import WebKit

@MainActor
public final class PrototypeSettingsWindowController: NSObject, NSWindowDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    /// One window per companion (MR-001).
    private var window: NSWindow?
    private var webView: WKWebView?

    public func openOrFocus() {
        if let window {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let window = buildWindow()
        self.window = window
        window.delegate = self
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    public func windowWillClose(_ notification: Notification) {
        // Closing the window never stops Core (MT-001).
        window = nil
        webView = nil
    }

    /// Surfacing the loaded page's title in the (hidden) title lets external
    /// tooling verify the prototype actually rendered, not a blank web area.
    public func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        window?.subtitle = webView.title ?? ""
        // Developer-harness verification only (harness log): the prototype
        // finished rendering. Harmless in the companion; remove at Phase 2.
        NSLog("prototype did load: %@", webView.title ?? "(no title)")
    }

    // MARK: - Construction

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
        window.backgroundColor = NSColor(srgbRed: 0x1E / 255.0, green: 0x1E / 255.0, blue: 0x20 / 255.0, alpha: 1)
        window.isReleasedWhenClosed = false

        let configuration = WKWebViewConfiguration()
        // The bridge channel exists for Phase 2 native wiring; page script can
        // only post messages into it, it can never invoke a native action
        // without the controller's explicit handler (§12.1 trusted path).
        configuration.userContentController.add(self, name: "native")
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.setValue(false, forKey: "drawsBackground")
        webView.navigationDelegate = self
        self.webView = webView

        window.contentView = webView
        loadPrototype(into: webView)
        return window
    }

    private func loadPrototype(into webView: WKWebView) {
        // SPM .copy lands the package at <bundle>/Resources/prototype, so the
        // read-access root is Bundle.module.resourceURL + "prototype".
        let base = Bundle.module.resourceURL!
            .appendingPathComponent("prototype", isDirectory: true)
        let index = base.appendingPathComponent("Core-Settings.html")
        webView.loadFileURL(index, allowingReadAccessTo: base)
    }

    // MARK: - WKScriptMessageHandler (receive-only until Phase 2 wiring)

    public func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        // No native action is reachable from page script in this build. Every
        // recovery action must come from the trusted native interaction path
        // (§11.1); log-and-ignore keeps that boundary enforceable.
    }
}
