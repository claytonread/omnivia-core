// ============================================================================
// OmniVia Core Settings — the five-pane settings window (MR-001, MR-002, §15).
//
// One owned window, opened/focused from the companion menu. Closing it never
// stops Core (the menu keeps its own lifecycle authority). Layout, row
// grammar, statuses and copy follow the Core Settings prototype (I01) and
// amendment v1.1 (I05): General, Data, Processing, Access, Maintenance, plus
// one macOS access summary in General. Passive refresh runs on open, pane
// change, foreground return and Refresh status — never prompting, probing,
// registering or connecting (§8.4, MT-002).
//
// Prototype fidelity notes: statuses use the amendment's exact distinctions
// (Off · Optional / Needs approval / Needs attention / Not checked / Checking…);
// status never relies on colour alone (icon + word); loading indicators are
// local to a row; no full-window blocking for a hung provider.
// ============================================================================

import AppKit
import Combine
import ServiceManagement
import UserNotifications

@MainActor
final class SettingsWindowController: NSObject, NSWindowDelegate {
    /// One window per companion (MR-001). Reopening focuses the existing one.
    private var window: NSWindow?

    private let coordinator: ReadinessCoordinator
    private let navigator: SettingsNavigator
    private let notificationCenter: UNUserNotificationCenter?
    private var cancellables: Set<AnyCancellable> = []

    // Pane selection
    private enum Pane: String, CaseIterable {
        case general = "General"
        case data = "Data"
        case processing = "Processing"
        case access = "Access"
        case maintenance = "Maintenance"
    }

    private var selectedPane: Pane = .general {
        didSet { guard oldValue != selectedPane else { return }; render() }
    }

    // Sidebar controls
    private var sidebarButtons: [NSButton: Pane] = [:]
    private var contentStack: NSStackView?
    private var summaryBox: NSTextField?
    private var summaryDetail: NSTextField?

    /// Core preferences mirrored from the companion's own preference owner.
    /// These are Core facts, persisted by their existing owner (§14.1) — the
    /// readiness layer only reads them.
    var startCoreAtLoginSelected = false
    var attentionNotificationsSelected = false
    var hasWorkspace = false

    init(
        coordinator: ReadinessCoordinator,
        navigator: SettingsNavigator = SettingsNavigator(componentName: "OmniVia Core"),
        notificationCenter: UNUserNotificationCenter? = UNUserNotificationCenter.current()
    ) {
        self.coordinator = coordinator
        self.navigator = navigator
        self.notificationCenter = notificationCenter
        super.init()
        coordinator.$snapshot.sink { [weak self] _ in self?.render() }.store(in: &cancellables)
        coordinator.$refreshInFlight.sink { [weak self] _ in self?.render() }.store(in: &cancellables)
    }

    // MARK: - Window lifecycle (MR-001)

    func openOrFocus() {
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

    func windowWillClose(_ notification: Notification) {
        // Cancel UI-owned passive work; Core is untouched (MT-001, §8.3).
        window = nil
        contentStack = nil
        sidebarButtons.removeAll()
    }

    // MARK: - Construction

    private func buildWindow() -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 640),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Core Settings"
        window.titlebarAppearsTransparent = false
        window.isReleasedWhenClosed = false

        let sidebar = buildSidebar()
        let content = NSStackView()
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 12
        content.edgeInsets = NSEdgeInsets(top: 24, left: 24, bottom: 24, right: 24)
        contentStack = content

        let scroll = NSScrollView()
        scroll.documentView = content
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false

        let split = NSSplitView(frame: NSRect(x: 0, y: 0, width: 900, height: 640))
        split.isVertical = true
        split.dividerStyle = .thin
        split.addArrangedSubview(sidebar)
        split.addArrangedSubview(scroll)
        split.autosaveName = "CoreSettingsSplit"
        split.setPosition(200, ofDividerAt: 0)

        window.contentView = split
        render()
        return window
    }

    private func buildSidebar() -> NSView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)

        let brand = NSTextField(labelWithString: "OmniVia — Core Settings")
        brand.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        stack.addArrangedSubview(brand)
        stack.addArrangedSpacer(12)

        for pane in Pane.allCases {
            let button = NSButton(title: pane.rawValue, target: self, action: #selector(selectPane(_:)))
            button.bezelStyle = .regularSquare
            button.isBordered = false
            button.setButtonType(.momentaryChange)
            button.alignment = .left
            button.keyEquivalentModifierMask = []
            sidebarButtons[button] = pane
            stack.addArrangedSubview(button)
            stack.addArrangedSubview(.spacer(height: 2))
        }

        stack.addArrangedSpacer(24)
        let serviceLine = NSTextField(
            labelWithString: "Core service status lives in the menu. Closing this window never stops Core."
        )
        serviceLine.font = NSFont.systemFont(ofSize: 10)
        serviceLine.textColor = .secondaryLabelColor
        serviceLine.lineBreakMode = .byWordWrapping
        serviceLine.preferredMaxLayoutWidth = 170
        stack.addArrangedSubview(serviceLine)

        let container = NSView()
        container.translatesAutoresizingMaskIntoConstraints = false
        stack.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: container.topAnchor),
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: container.trailingAnchor),
        ])
        // Highlight the selected pane.
        refreshSidebarSelection()
        return container
    }

    private func refreshSidebarSelection() {
        for (button, pane) in sidebarButtons {
            let selected = pane == selectedPane
            button.contentTintColor = selected ? .controlAccentColor : .labelColor
            button.font = NSFont.systemFont(ofSize: 13, weight: selected ? .semibold : .regular)
        }
    }

    @objc private func selectPane(_ sender: NSButton) {
        guard let pane = sidebarButtons[sender] else { return }
        selectedPane = pane
        refreshSidebarSelection()
    }

    // MARK: - Rendering

    private func render() {
        guard let content = contentStack else { return }
        content.views.forEach { content.removeView($0) }

        let title = NSTextField(labelWithString: selectedPane.rawValue)
        title.font = NSFont.systemFont(ofSize: 20, weight: .semibold)
        content.addArrangedSubview(title)
        content.addArrangedSubview(.spacer(height: 4))

        switch selectedPane {
        case .general: renderGeneral(content)
        case .data: renderData(content)
        case .processing: renderProcessing(content)
        case .access: renderAccess(content)
        case .maintenance: renderMaintenance(content)
        }
        content.addArrangedSubview(.spacer(height: 1))
    }

    private func addRow(_ stack: NSStackView, title: String, detail: String?) -> (NSTextField, NSStackView) {
        let row = NSStackView()
        row.orientation = .vertical
        row.alignment = .leading
        row.spacing = 3
        let t = NSTextField(labelWithString: title)
        t.font = NSFont.systemFont(ofSize: 13, weight: .medium)
        row.addArrangedSubview(t)
        if let detail {
            let line = NSTextField(wrappingLabelWithString: detail)
            line.font = NSFont.systemFont(ofSize: 12)
            line.textColor = .secondaryLabelColor
            line.preferredMaxLayoutWidth = 600
            row.addArrangedSubview(line)
        }
        stack.addArrangedSubview(row)
        stack.addArrangedSubview(.spacer(height: 10))
        return (t, row)
    }

    /// Status chip: icon + word, never colour alone (§15).
    private func statusChip(_ text: String, symbol: String) -> NSView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.spacing = 5
        let icon = NSImageView()
        if let image = NSImage(systemSymbolName: symbol, accessibilityDescription: text) {
            icon.image = image
            icon.contentTintColor = .secondaryLabelColor
        }
        icon.setContentHuggingPriority(.required, for: .horizontal)
        let label = NSTextField(labelWithString: text)
        label.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        label.textColor = .secondaryLabelColor
        row.addArrangedSubview(icon)
        row.addArrangedSubview(label)
        return row
    }

    private func actionButton(_ title: String, action: Selector, subject: String? = nil) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded
        button.setButtonType(.momentaryPushIn)
        // Accessible names identify their subject (§15, MR-017).
        if let subject { button.setAccessibilityLabel("\(title) — \(subject)") }
        return button
    }

    // MARK: General

    private func renderGeneral(_ content: NSStackView) {
        let preferences = currentPreferences()

        // macOS access summary (one per window; §7.5 aggregation).
        let summary = ReadinessReducer.summarize(
            checks: coordinator.snapshot.checks,
            preferences: preferences,
            refreshInFlight: coordinator.refreshInFlight,
            everObserved: coordinator.snapshot.observedAt != nil
        )
        let box = group("macOS access", into: content)
        let headline = NSTextField(wrappingLabelWithString: summary.headline)
        headline.font = NSFont.systemFont(ofSize: 13, weight: .medium)
        headline.preferredMaxLayoutWidth = 600
        box.addArrangedSubview(headline)
        if let detail = summary.detail {
            let line = NSTextField(wrappingLabelWithString: detail)
            line.font = NSFont.systemFont(ofSize: 12)
            line.textColor = .secondaryLabelColor
            line.preferredMaxLayoutWidth = 600
            box.addArrangedSubview(line)
        }
        let actions = NSStackView()
        actions.orientation = .horizontal
        actions.spacing = 8
        actions.addArrangedSubview(actionButton("Refresh status", action: #selector(refreshStatus)))
        if summary.kind == .attention {
            for issue in summary.issues {
                switch issue.checkID {
                case .coreBackground, .companionLogin:
                    actions.addArrangedSubview(actionButton("Open Login Items", action: #selector(openLoginItems), subject: "Core startup"))
                case .notificationsDelivery:
                    actions.addArrangedSubview(actionButton("Open Notifications", action: #selector(openNotifications), subject: "notifications"))
                default: break
                }
            }
        }
        box.addArrangedSubview(actions)
        content.addArrangedSubview(.spacer(height: 8))

        // Startup (companion login / Core background are distinct subjects, MT-008).
        let startupGroup = group("Startup", into: content)
        let startupCheck = coordinator.snapshot.checks.first { $0.checkID == .coreBackground }
        let serviceRunning: Bool? = nil // Service health is a separate projection (§7.1), not claimed here.
        let startupRow = ReadinessReducer.startupRow(check: startupCheck, serviceRunning: serviceRunning)
        let (startupTitle, startupRowView) = addRow(startupGroup, title: "Start Core at login", detail: nil)
        startupRowView.addArrangedSubview(
            statusChip(startupRow.registration, symbol: symbol(for: startupRow.registration))
        )
        if let detail = startupRow.detail {
            let line = NSTextField(wrappingLabelWithString: detail)
            line.font = NSFont.systemFont(ofSize: 12)
            line.textColor = .secondaryLabelColor
            line.preferredMaxLayoutWidth = 600
            startupRowView.addArrangedSubview(line)
        }
        // Binding disposition (§19): no qualified registration mechanism exists,
        // so the toggle is shown with its honest state and the OS route — never
        // a registration we cannot own.
        startupGroup.addArrangedSubview(actionButton("Open Login Items", action: #selector(openLoginItems), subject: "Core startup"))

        // Notifications.
        let notifyGroup = group("Notifications", into: content)
        let notifyCheck = coordinator.snapshot.checks.first { $0.checkID == .notificationsDelivery }
        let (_, notifyRowView) = addRow(
            notifyGroup,
            title: "Notify me when Core needs attention",
            detail: "Core's preference and macOS authorisation are separate facts."
        )
        let notifyState: (String, String)
        switch notifyCheck?.observedState {
        case .allowed: notifyState = ("Allowed", "checkmark.circle")
        case .limitedDelivery: notifyState = ("Limited — alerts are off", "exclamationmark.circle")
        case .denied: notifyState = ("Blocked in macOS", "exclamationmark.triangle")
        case .notDetermined: notifyState = ("Not set up", "circle.dashed")
        case .unrecognized: notifyState = ("Not checked", "circle.dashed")
        default:
            notifyState = coordinator.refreshInFlight ? ("Checking…", "clock") : ("Not checked", "circle.dashed")
        }
        notifyRowView.addArrangedSubview(statusChip(notifyState.0, symbol: notifyState.1))
        let notifyActions = NSStackView()
        notifyActions.orientation = .horizontal
        notifyActions.spacing = 8
        if notifyCheck?.observedState == .notDetermined {
            // Explicit, user-initiated request only; never on load (MT-013).
            notifyActions.addArrangedSubview(actionButton("Allow notifications…", action: #selector(requestNotifications), subject: "notifications"))
        } else if notifyCheck?.observedState == .denied || notifyCheck?.observedState == .limitedDelivery {
            notifyActions.addArrangedSubview(actionButton("Open Notifications", action: #selector(openNotifications), subject: "notifications"))
        }
        notifyActions.addArrangedSubview(actionButton("Refresh status", action: #selector(refreshStatus)))
        notifyGroup.addArrangedSubview(notifyActions)
    }

    // MARK: Data

    private func renderData(_ content: NSStackView) {
        // No backup feature exists in this build (binding §5): the pane shows
        // the honest neutral state, not a broken control (MT-007).
        let group = group("Backup", into: content)
        addRow(
            group,
            title: "Backup destination",
            detail: "Not installed — optional. No backup feature is installed in this build; when it exists, its access status and a bounded test will appear here."
        )
    }

    // MARK: Processing

    private func renderProcessing(_ content: NSStackView) {
        let group = group("Sources", into: content)
        // No source feature exists in this build (binding §5); per §6.2 the
        // conditional checks stay absent rather than becoming a checklist.
        addRow(
            group,
            title: "No configured sources",
            detail: "Sources are added by their owning feature. Per-source access status and recovery appear beside each source once that feature exists."
        )
    }

    // MARK: Access

    private func renderAccess(_ content: NSStackView) {
        let group = group("Connections", into: content)
        addRow(
            group,
            title: "No configured connections",
            detail: "Connection recovery appears beside each connection once the connection feature exists. macOS permissions and Core app-to-Core authority remain separate facts."
        )
    }

    // MARK: Maintenance

    private func renderMaintenance(_ content: NSStackView) {
        let group = group("macOS check details", into: content)
        let snapshot = coordinator.snapshot
        let details = """
        Generation: \(snapshot.generation)
        Installation: \(snapshot.context.installationRevision)
        Workspace: \(snapshot.context.workspaceRef)
        Observed: \(snapshot.observedAt.map { ISO8601DateFormatter().string(from: $0) } ?? "never")
        """
        let text = NSTextField(wrappingLabelWithString: details)
        text.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        text.preferredMaxLayoutWidth = 600
        group.addArrangedSubview(text)
        group.addArrangedSubview(actionButton("Refresh status", action: #selector(refreshStatus)))

        // Diagnostics are bounded and safe by construction (§14.2): the fields
        // above are the whole export — no paths, no bookmarks, no secrets.
        let note = NSTextField(wrappingLabelWithString: "These details are what diagnostics export contains. Nothing else is recorded.")
        note.font = NSFont.systemFont(ofSize: 12)
        note.textColor = .secondaryLabelColor
        note.preferredMaxLayoutWidth = 600
        group.addArrangedSubview(note)
    }

    // MARK: Helpers

    private func group(_ title: String, into content: NSStackView) -> NSStackView {
        let header = NSTextField(labelWithString: title)
        header.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
        header.textColor = .secondaryLabelColor
        content.addArrangedSubview(header)
        let box = NSStackView()
        box.orientation = .vertical
        box.alignment = .leading
        box.spacing = 6
        box.edgeInsets = NSEdgeInsets(top: 10, left: 12, bottom: 12, right: 12)
        box.wantsLayer = true
        box.layer?.cornerRadius = 8
        box.layer?.backgroundColor = NSColor.quaternaryLabelColor.withAlphaComponent(0.08).cgColor
        content.addArrangedSubview(box)
        content.addArrangedSubview(.spacer(height: 10))
        return box
    }

    private func symbol(for startupState: String) -> String {
        switch startupState {
        case "Enabled": return "checkmark.circle"
        case "Needs approval": return "exclamationmark.circle"
        case "Not checked", "Checking…": return "circle.dashed"
        default: return "circle.slash"
        }
    }

    private func currentPreferences() -> ReadinessPreferences {
        ReadinessPreferences(
            startCoreAtLoginSelected: startCoreAtLoginSelected,
            attentionNotificationsSelected: attentionNotificationsSelected,
            hasWorkspace: hasWorkspace
        )
    }

    // MARK: - Explicit actions (§11)

    @objc private func refreshStatus() {
        coordinator.scheduleRefresh(.explicitRefresh)
    }

    @objc private func openLoginItems() {
        Task { @MainActor in
            let (result, route) = await navigator.open(.loginItems)
            presentNavigation(result, route: route, destination: .loginItems)
        }
    }

    @objc private func openNotifications() {
        Task { @MainActor in
            let (result, route) = await navigator.open(.notifications)
            presentNavigation(result, route: route, destination: .notifications)
        }
    }

    /// Explicit notification request (§9.2, §11.2): only from this trusted
    /// button, only once per pending interaction; the passive provider re-reads
    /// the sender's settings afterwards — the request result itself is never
    /// treated as a permission grant.
    @objc private func requestNotifications() {
        guard let center = notificationCenter else { return }
        Task { @MainActor in
            let settings = await center.notificationSettings()
            if settings.authorizationStatus == .notDetermined {
                _ = try? await center.requestAuthorization(options: [.alert, .sound])
            }
            // Re-read through the passive provider; the reducer reflects denial
            // or limited delivery honestly (MT-014/MT-015).
            coordinator.scheduleRefresh(.explicitRefresh)
        }
    }

    private func presentNavigation(_ result: NavigationResult, route: String?, destination: SettingsDestination) {
        guard let window else { return }
        let alert = NSAlert()
        switch result {
        case .navigationRequested, .genericSettingsOpened:
            alert.messageText = "System Settings opened"
            // Navigation requested — never "access enabled" (§10.4).
            alert.informativeText = (route ?? navigator.manualRoute(for: destination) ?? "")
                + "\n\nStatus re-reads when you come back to this window."
        case .launchFailed, .cancelled, .unsupportedDestination:
            alert.messageText = "System Settings could not be opened"
            alert.informativeText = (route ?? navigator.manualRoute(for: destination) ?? "")
                + "\n\nNothing has changed; use Try again if you want another attempt."
        }
        alert.alertStyle = result == .launchFailed ? .warning : .informational
        alert.beginSheetModal(for: window)
    }
}

// MARK: - Small layout helpers

private extension NSView {
    static func spacer(height: CGFloat) -> NSView {
        let view = NSView()
        view.translatesAutoresizingMaskIntoConstraints = false
        view.heightAnchor.constraint(equalToConstant: height).isActive = true
        return view
    }
}

private extension NSStackView {
    func addArrangedSpacer(_ height: CGFloat) {
        addArrangedSubview(NSView.spacer(height: height))
    }
}
