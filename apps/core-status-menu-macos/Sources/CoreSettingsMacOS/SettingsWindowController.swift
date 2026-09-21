// ============================================================================
// OmniVia Core Settings — the five-pane settings window (MR-001, MR-002, §15).
//
// One owned window, opened/focused from the companion menu. Closing it never
// stops Core (the menu keeps its own lifecycle authority). Layout, row
// grammar, tokens and copy follow the Core Settings prototype (I01) and
// amendment v1.1 (I05): the OmniVia dark token layer (bg-content #1E1E20,
// sidebar #1C1C21, card #232326, brick accent #D2756C, semantic success/
// warning/danger), the branded 60px header with the concentric-circle mark,
// the 200px source list with pill selection, group cards with 10px radius,
// status chips (icon + word, never colour alone) and OmniVia buttons.
//
// Passive refresh runs on open and Refresh status — never prompting, probing,
// registering or connecting (§8.4, MT-002).
// ============================================================================

import AppKit
import Combine
import ServiceManagement
import UserNotifications

// MARK: - OmniVia dark tokens (styles/tokens.css, dark layer)

enum Ov {
    static let bgWindow = NSColor(srgbRed: 0x1c / 255.0, green: 0x1c / 255.0, blue: 0x21 / 255.0, alpha: 1)
    static let bgContent = NSColor(srgbRed: 0x1E / 255.0, green: 0x1E / 255.0, blue: 0x20 / 255.0, alpha: 1)
    static let bgContentSecondary = NSColor(srgbRed: 0x23 / 255.0, green: 0x23 / 255.0, blue: 0x26 / 255.0, alpha: 1)
    static let bgElevated = NSColor(srgbRed: 0x26 / 255.0, green: 0x26 / 255.0, blue: 0x2e / 255.0, alpha: 1)
    static let bgInsetDeep = NSColor(srgbRed: 0x14 / 255.0, green: 0x14 / 255.0, blue: 0x17 / 255.0, alpha: 1)
    static let accent = NSColor(srgbRed: 0xD2 / 255.0, green: 0x75 / 255.0, blue: 0x6C / 255.0, alpha: 1)
    static let accentActive = NSColor(srgbRed: 0xE6 / 255.0, green: 0x96 / 255.0, blue: 0x8D / 255.0, alpha: 1)
    static let success = NSColor(srgbRed: 0x3D / 255.0, green: 0xBE / 255.0, blue: 0x83 / 255.0, alpha: 1)
    static let warning = NSColor(srgbRed: 0xE0 / 255.0, green: 0xA3 / 255.0, blue: 0x3E / 255.0, alpha: 1)
    static let textPrimary = NSColor.white.withAlphaComponent(0.92)
    static let textSecondary = NSColor.white.withAlphaComponent(0.58)
    static let textTertiary = NSColor.white.withAlphaComponent(0.40)
    static let separator = NSColor.white.withAlphaComponent(0.10)
    static let borderSubtle = NSColor.white.withAlphaComponent(0.08)
    static let borderControl = NSColor.white.withAlphaComponent(0.18)
    static let hoverBg = NSColor.white.withAlphaComponent(0.09)
    static let selectionTextBg = NSColor(srgbRed: 210 / 255.0, green: 117 / 255.0, blue: 108 / 255.0, alpha: 0.30)
    static let onAccent = NSColor.white
}

// MARK: - OmniVia typography (styles/tokens.css §1)

enum OvFont {
    /// CSS weight → NSFont.Weight (CoreText scale). The prototype's canonical
    /// weights are 400 / 500 / 590 / 700; 590 sits between medium (0.23) and
    /// semibold (0.30).
    private static func weight(_ css: CGFloat) -> NSFont.Weight {
        switch css {
        case ..<450: return .regular        // 400
        case ..<580: return .medium         // 500
        case ..<650: return NSFont.Weight(rawValue: 0.28) // 590
        case ..<750: return .semibold       // 600
        default: return .bold               // 700
        }
    }

    /// SF Pro (--ov-font-sans). NSFont.systemFont resolves the optical
    /// Text/Display cut automatically at the ~20px breakpoint.
    static func sans(_ size: CGFloat, _ cssWeight: CGFloat = 400) -> NSFont {
        .systemFont(ofSize: size, weight: weight(cssWeight))
    }

    /// SF Mono (--ov-font-mono) with tabular-nums, for paths, IDs, versions,
    /// counts, logs and code.
    static func mono(_ size: CGFloat, _ cssWeight: CGFloat = 400) -> NSFont {
        let base = NSFont.monospacedSystemFont(ofSize: size, weight: weight(cssWeight))
        let settings: [[NSFontDescriptor.FeatureKey: Any]] = [[
            .typeIdentifier: kNumberSpacingType,
            .selectorIdentifier: kMonospacedNumbersSelector,
        ]]
        let descriptor = base.fontDescriptor.addingAttributes([.featureSettings: settings])
        return NSFont(descriptor: descriptor, size: size) ?? base
    }

    /// A non-wrapping label with the token font, colour and letter-spacing
    /// (CSS `letter-spacing` em values → AppKit kern in points).
    static func label(
        _ text: String,
        size: CGFloat,
        cssWeight: CGFloat = 400,
        color: NSColor,
        tracking: CGFloat = 0,
        mono: Bool = false
    ) -> NSTextField {
        let font = mono ? self.mono(size, cssWeight) : sans(size, cssWeight)
        let attributed = NSAttributedString(string: text, attributes: [
            .font: font,
            .foregroundColor: color,
            .kern: tracking * size,
        ])
        return NSTextField(labelWithAttributedString: attributed)
    }

    // Prototype roles.
    static func paneTitle(_ text: String) -> NSTextField {
        label(text, size: 20, cssWeight: 700, color: Ov.textPrimary, tracking: -0.02)
    }
    static func groupHeader(_ text: String) -> NSTextField {
        label(text.uppercased(), size: 11, cssWeight: 700, color: Ov.textTertiary, tracking: 0.05)
    }
    static func wordmark(_ text: String) -> NSTextField {
        label(text, size: 13, cssWeight: 590, color: Ov.textPrimary, tracking: -0.006)
    }
}

@MainActor
public final class SettingsWindowController: NSObject, NSWindowDelegate {
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

        var symbol: String {
            switch self {
            case .general: return "slider.horizontal.3"
            case .data: return "internaldrive"
            case .processing: return "arrow.triangle.2.circlepath"
            case .access: return "shield"
            case .maintenance: return "wrench.adjustable"
            }
        }
    }

    private var selectedPane: Pane = .general {
        didSet { guard oldValue != selectedPane else { return }; render(); renderSidebar() }
    }

    private var sidebarStack: NSStackView?
    private var contentStack: NSStackView?

    /// Core preferences mirrored from the companion's own preference owner.
    /// These are Core facts, persisted by their existing owner (§14.1) — the
    /// readiness layer only reads them.
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
        // Resolve lazily and bundle-guarded: UNUserNotificationCenter.current()
        // raises when the process has no bundle identity (§13.2 honest states).
        if let notificationCenter {
            self.notificationCenter = notificationCenter
        } else if Bundle.main.bundleIdentifier != nil {
            self.notificationCenter = UNUserNotificationCenter.current()
        } else {
            self.notificationCenter = nil
        }
        super.init()
        coordinator.$snapshot.sink { [weak self] _ in self?.render() }.store(in: &cancellables)
        coordinator.$refreshInFlight.sink { [weak self] _ in self?.render() }.store(in: &cancellables)
    }

    // MARK: - Window lifecycle (MR-001)

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
        contentStack = nil
        sidebarStack = nil
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
        window.backgroundColor = Ov.bgContent
        window.isReleasedWhenClosed = false

        // ---- Branded header (60px, prototype .cs-head) ----
        let header = headerView()

        // ---- Sidebar (200px, prototype .sw-side) ----
        let sidebar = buildSidebar()

        // ---- Content pane (prototype .sw-pane) ----
        let content = NSStackView()
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 0
        content.edgeInsets = NSEdgeInsets(top: 30, left: 40, bottom: 90, right: 40)
        let contentHost = NSView()
        contentHost.wantsLayer = true
        contentHost.layer?.backgroundColor = Ov.bgContent.cgColor
        content.translatesAutoresizingMaskIntoConstraints = false
        contentHost.addSubview(content)
        NSLayoutConstraint.activate([
            content.topAnchor.constraint(equalTo: contentHost.topAnchor),
            content.leadingAnchor.constraint(equalTo: contentHost.leadingAnchor),
            content.trailingAnchor.constraint(equalTo: contentHost.trailingAnchor),
        ])
        let scroll = NSScrollView()
        scroll.documentView = contentHost
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = true
        scroll.backgroundColor = Ov.bgContent
        scroll.translatesAutoresizingMaskIntoConstraints = false
        contentStack = content

        // ---- Body: sidebar + pane ----
        let body = NSStackView()
        body.orientation = .horizontal
        body.alignment = .top
        body.spacing = 0
        body.addArrangedSubview(sidebar)
        body.addArrangedSubview(scroll)

        let root = NSStackView()
        root.orientation = .vertical
        root.spacing = 0
        root.addArrangedSubview(header)
        root.addArrangedSubview(body)

        window.contentView = root
        NSLayoutConstraint.activate([
            sidebar.widthAnchor.constraint(equalToConstant: 200),
        ])
        renderSidebar()
        render()
        return window
    }

    /// 60px branded header: the concentric-circle mark in brick, "OmniVia"
    /// wordmark with "Core Settings" secondary (prototype .cs-brand).
    private func headerView() -> NSView {
        let header = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: 900, height: 60))
        header.material = .titlebar
        header.blendingMode = .withinWindow
        header.state = .active
        header.wantsLayer = true
        header.heightAnchor.constraint(equalToConstant: 60).isActive = true

        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 10
        row.edgeInsets = NSEdgeInsets(top: 0, left: 78, bottom: 0, right: 16)
        row.translatesAutoresizingMaskIntoConstraints = false
        header.addSubview(row)
        NSLayoutConstraint.activate([
            row.topAnchor.constraint(equalTo: header.topAnchor),
            row.bottomAnchor.constraint(equalTo: header.bottomAnchor),
            row.leadingAnchor.constraint(equalTo: header.leadingAnchor),
            row.trailingAnchor.constraint(lessThanOrEqualTo: header.trailingAnchor),
        ])

        // The mark: two concentric circles, brick (assets/omnivia-mark.svg geometry).
        let mark = MarkView(frame: NSRect(x: 0, y: 0, width: 22, height: 22))
        row.addArrangedSubview(mark)

        let titleColumn = NSStackView()
        titleColumn.orientation = .vertical
        titleColumn.alignment = .leading
        titleColumn.spacing = 1
        let wordmark = OvFont.wordmark("OmniVia")
        let sub = OvFont.label("Core Settings", size: 11, color: Ov.textSecondary)
        titleColumn.addArrangedSubview(wordmark)
        titleColumn.addArrangedSubview(sub)
        row.addArrangedSubview(titleColumn)

        // Bottom separator.
        let line = NSView()
        line.wantsLayer = true
        line.layer?.backgroundColor = Ov.separator.cgColor
        line.translatesAutoresizingMaskIntoConstraints = false
        header.addSubview(line)
        NSLayoutConstraint.activate([
            line.heightAnchor.constraint(equalToConstant: 1),
            line.leadingAnchor.constraint(equalTo: header.leadingAnchor),
            line.trailingAnchor.constraint(equalTo: header.trailingAnchor),
            line.bottomAnchor.constraint(equalTo: header.bottomAnchor),
        ])
        return header
    }

    // MARK: Sidebar

    private func buildSidebar() -> NSView {
        let effect = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: 200, height: 640))
        effect.material = .sidebar
        effect.blendingMode = .withinWindow
        effect.state = .active

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 1
        stack.edgeInsets = NSEdgeInsets(top: 4, left: 8, bottom: 12, right: 8)
        stack.translatesAutoresizingMaskIntoConstraints = false
        effect.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: effect.topAnchor),
            stack.leadingAnchor.constraint(equalTo: effect.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: effect.trailingAnchor),
        ])
        sidebarStack = stack
        return effect
    }

    private func renderSidebar() {
        guard let stack = sidebarStack else { return }
        stack.views.forEach { stack.removeView($0) }

        for pane in Pane.allCases {
            let row = SidebarRowView(title: pane.rawValue, symbol: pane.symbol)
            row.isSelected = pane == selectedPane
            row.onSelect = { [weak self] in self?.selectedPane = pane }
            stack.addArrangedSubview(row)
            row.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }

        stack.addArrangedSubview(ovSpacer(20))

        // Footer (prototype .cs-foot): service-status line, one place only.
        let foot = NSTextField(wrappingLabelWithString: "Core service status lives in the menu. Closing this window never stops Core.")
        foot.font = OvFont.sans(11.5)
        foot.textColor = Ov.textSecondary
        foot.preferredMaxLayoutWidth = 170
        stack.addArrangedSubview(foot)
    }

    // MARK: - Rendering

    private func render() {
        guard let content = contentStack else { return }
        content.views.forEach { content.removeView($0) }

        let title = OvFont.paneTitle(selectedPane.rawValue)
        content.addArrangedSubview(title)
        content.addArrangedSubview(ovSpacer(22))

        switch selectedPane {
        case .general: renderGeneral(content)
        case .data: renderData(content)
        case .processing: renderProcessing(content)
        case .access: renderAccess(content)
        case .maintenance: renderMaintenance(content)
        }
        content.addArrangedSubview(ovSpacer(8))
    }

    // MARK: Card/group helpers (prototype .sw-group / .sw-rows)

    private func card(_ stack: NSStackView, header: String?) -> NSStackView {
        if let header {
            let label = OvFont.groupHeader(header)
            stack.addArrangedSubview(label)
            stack.addArrangedSubview(ovSpacer(8))
        }
        let card = NSStackView()
        card.orientation = .vertical
        card.alignment = .leading
        card.spacing = 0
        card.edgeInsets = NSEdgeInsets(top: 0, left: 0, bottom: 0, right: 0)
        card.wantsLayer = true
        card.layer?.backgroundColor = Ov.bgContent.cgColor
        card.layer?.cornerRadius = 10
        card.layer?.borderWidth = 0.5
        card.layer?.borderColor = Ov.borderSubtle.cgColor
        stack.addArrangedSubview(card)
        stack.addArrangedSubview(ovSpacer(18))
        return card
    }

    private enum ChipTone { case ok, warn, neu, checking }

    /// Status chip (prototype .cs-status): icon + word, never colour alone.
    private func chip(_ text: String, tone: ChipTone) -> NSView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 5
        let symbol: String
        switch tone {
        case .ok: symbol = "checkmark.circle.fill"
        case .warn: symbol = "exclamationmark.circle"
        case .neu, .checking: symbol = "circle.dashed"
        }
        let icon = NSImageView()
        if let image = NSImage(systemSymbolName: symbol, accessibilityDescription: text)?
            .withSymbolConfiguration(.init(pointSize: 13, weight: .regular)) {
            icon.image = image
            icon.contentTintColor = tone == .ok ? Ov.success : (tone == .warn ? Ov.warning : Ov.textTertiary)
        }
        icon.setContentHuggingPriority(.required, for: .horizontal)
        let label = OvFont.label(text, size: 11.5, color: Ov.textSecondary)
        row.addArrangedSubview(icon)
        row.addArrangedSubview(label)
        return row
    }

    /// Row (prototype .sw-row): title line with trailing control column and an
    /// optional detail under it. Returns the row stack for appending chips.
    @discardableResult
    private func row(
        into card: NSStackView,
        title: String,
        detail: String?,
        trailing: NSView? = nil
    ) -> NSStackView {
        let line = NSStackView()
        line.orientation = .horizontal
        line.alignment = .centerY
        line.spacing = 8
        let t = OvFont.label(title, size: 13, cssWeight: 600, color: Ov.textPrimary)
        line.addArrangedSubview(t)
        if let trailing {
            let spring = NSView()
            spring.setContentHuggingPriority(.init(1), for: .horizontal)
            line.addArrangedSubview(spring)
            line.addArrangedSubview(trailing)
        }
        let body = NSStackView()
        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = 3
        body.edgeInsets = NSEdgeInsets(top: 13, left: 16, bottom: 13, right: 16)
        body.addArrangedSubview(line)
        if let detail {
            let d = NSTextField(wrappingLabelWithString: detail)
            d.font = OvFont.sans(11.5)
            d.textColor = Ov.textTertiary
            d.preferredMaxLayoutWidth = 560
            body.addArrangedSubview(d)
        }
        card.addArrangedSubview(body)
        addSeparator(below: body, in: card)
        return body
    }

    private func addSeparator(below view: NSView, in card: NSStackView) {
        let index = card.arrangedSubviews.firstIndex(of: view).map { $0 + 1 } ?? card.arrangedSubviews.count
        let line = NSView()
        line.wantsLayer = true
        line.layer?.backgroundColor = Ov.separator.cgColor
        line.heightAnchor.constraint(equalToConstant: 1).isActive = true
        card.insertArrangedSubview(line, at: index)
    }

    /// Recovery block (prototype .cs-fix): title, known effect, actions.
    private func recoveryBlock(
        title: String,
        detail: String? = nil,
        actions: [NSButton],
        warn: Bool = false
    ) -> NSStackView {
        let block = NSStackView()
        block.orientation = .vertical
        block.alignment = .leading
        block.spacing = 6
        block.edgeInsets = NSEdgeInsets(top: 10, left: 12, bottom: 10, right: 12)
        block.wantsLayer = true
        block.layer?.backgroundColor = Ov.bgContentSecondary.cgColor
        block.layer?.cornerRadius = 8
        block.layer?.borderWidth = 1
        block.layer?.borderColor = warn ? Ov.warning.withAlphaComponent(0.35).cgColor : Ov.borderSubtle.cgColor

        let t = NSTextField(wrappingLabelWithString: title)
        t.font = OvFont.sans(12.5, 500)
        t.textColor = Ov.textPrimary
        t.preferredMaxLayoutWidth = 520
        block.addArrangedSubview(t)
        if let detail {
            let d = NSTextField(wrappingLabelWithString: detail)
            d.font = OvFont.sans(11.5)
            d.textColor = Ov.textTertiary
            d.preferredMaxLayoutWidth = 520
            block.addArrangedSubview(d)
        }
        if !actions.isEmpty {
            let acts = NSStackView()
            acts.orientation = .horizontal
            acts.spacing = 8
            acts.alignment = .centerY
            actions.forEach { acts.addArrangedSubview($0) }
            block.addArrangedSubview(acts)
        }
        return block
    }

    // MARK: OmniVia buttons (prototype .ov-btn / .ov-btn--bordered / --primary)

    private func button(
        _ title: String,
        style: OvButtonStyle = .bordered,
        action: Selector,
        subject: String? = nil
    ) -> NSButton {
        let button = OvButton(title: title, style: style)
        button.target = self
        button.action = action
        if let subject { button.setAccessibilityLabel("\(title) — \(subject)") }
        return button
    }

    // MARK: General

    private func renderGeneral(_ content: NSStackView) {
        let preferences = currentPreferences()

        // ---- macOS access summary (one per window; §7.5 aggregation) ----
        let summary = ReadinessReducer.summarize(
            checks: coordinator.snapshot.checks,
            preferences: preferences,
            refreshInFlight: coordinator.refreshInFlight,
            everObserved: coordinator.snapshot.observedAt != nil
        )
        let summaryCard = card(content, header: "macOS access")
        let headlineRow = NSStackView()
        headlineRow.orientation = .horizontal
        headlineRow.alignment = .centerY
        headlineRow.spacing = 8
        let headline = NSTextField(wrappingLabelWithString: summary.headline)
        headline.font = OvFont.sans(13, 590)
        headline.textColor = Ov.textPrimary
        headline.preferredMaxLayoutWidth = 480
        headlineRow.addArrangedSubview(headline)
        let spring = NSView()
        spring.setContentHuggingPriority(.init(1), for: .horizontal)
        headlineRow.addArrangedSubview(spring)
        headlineRow.addArrangedSubview(
            button("Refresh status", style: .bordered, action: #selector(refreshStatus))
        )
        let summaryBody = NSStackView()
        summaryBody.orientation = .vertical
        summaryBody.alignment = .leading
        summaryBody.spacing = 4
        summaryBody.edgeInsets = NSEdgeInsets(top: 11, left: 0, bottom: 11, right: 0)
        summaryBody.addArrangedSubview(headlineRow)
        if let detail = summary.detail {
            let d = NSTextField(wrappingLabelWithString: detail)
            d.font = OvFont.sans(11.5)
            d.textColor = Ov.textTertiary
            d.preferredMaxLayoutWidth = 560
            summaryBody.addArrangedSubview(d)
        }
        summaryCard.addArrangedSubview(summaryBody)

        if summary.kind == .attention {
            for issue in summary.issues {
                var actions: [NSButton] = []
                var detail: String?
                switch issue.checkID {
                case .coreBackground, .companionLogin:
                    actions = [button("Open Login Items", style: .bordered, action: #selector(openLoginItems), subject: "Core startup")]
                    detail = "System Settings → General → Login Items & Extensions. Allow \"OmniVia Core\" under Allow in the background."
                case .notificationsDelivery:
                    actions = [button("Open Notifications", style: .bordered, action: #selector(openNotifications), subject: "notifications")]
                    detail = "System Settings → Notifications → OmniVia Core. Turn on Allow notifications and Alerts."
                default: break
                }
                guard !actions.isEmpty else { continue }
                summaryCard.addArrangedSubview(
                    recoveryBlock(title: issue.title, detail: detail, actions: actions, warn: true)
                )
            }
        }

        // ---- Startup (companion login / Core background are distinct subjects, MT-008) ----
        let startupCard = card(content, header: "Startup")
        let startupCheck = coordinator.snapshot.checks.first { $0.checkID == .coreBackground }
        let startupRow = ReadinessReducer.startupRow(check: startupCheck, serviceRunning: nil)
        let startupState: (String, ChipTone)
        switch startupRow.registration {
        case "Enabled": startupState = ("Enabled", .ok)
        case "Needs approval": startupState = ("Needs approval", .warn)
        case "Not set up": startupState = ("Off · Optional", .neu)
        default: startupState = (coordinator.refreshInFlight ? "Checking…" : "Not checked", .checking)
        }
        let startupBody = row(
            into: startupCard,
            title: "Start Core at login",
            detail: startupRow.detail ?? "Core's preference and the macOS registration are separate facts.",
            trailing: chip(startupState.0, tone: startupState.1)
        )
        // Binding disposition (§19): no qualified registration mechanism exists,
        // so the row shows the honest state and the OS route — never a
        // registration this build cannot own.
        startupBody.addArrangedSubview(
            recoveryBlock(
                title: "No startup mechanism is installed in this build.",
                detail: "When a qualified registration exists, its state and recovery appear here. Opening Login Items is always available.",
                actions: [button("Open Login Items", style: .bordered, action: #selector(openLoginItems), subject: "Core startup")]
            )
        )

        // ---- Notifications ----
        let notifyCard = card(content, header: "Notifications")
        let notifyCheck = coordinator.snapshot.checks.first { $0.checkID == .notificationsDelivery }
        let notifyState: (String, ChipTone)
        switch notifyCheck?.observedState {
        case .allowed: notifyState = ("Allowed", .ok)
        case .limitedDelivery: notifyState = ("Limited — alerts are off", .warn)
        case .denied: notifyState = ("Blocked in macOS", .warn)
        case .notDetermined: notifyState = ("Not set up", .neu)
        case .unrecognized: notifyState = ("Not checked", .neu)
        default:
            notifyState = coordinator.refreshInFlight ? ("Checking…", .checking) : ("Not checked", .neu)
        }
        let notifyBody = row(
            into: notifyCard,
            title: "Notify me when Core needs attention",
            detail: "Core's preference and macOS authorisation are separate facts.",
            trailing: chip(notifyState.0, tone: notifyState.1)
        )
        var notifyActions: [NSButton] = []
        if notifyCheck?.observedState == .notDetermined {
            // Explicit, user-initiated request only; never on load (MT-013).
            notifyActions.append(button("Allow notifications…", style: .primary, action: #selector(requestNotifications), subject: "notifications"))
        } else if notifyCheck?.observedState == .denied || notifyCheck?.observedState == .limitedDelivery {
            notifyActions.append(button("Open Notifications", style: .bordered, action: #selector(openNotifications), subject: "notifications"))
        }
        if !notifyActions.isEmpty {
            notifyBody.addArrangedSubview(recoveryBlock(title: recoveryTitle(for: notifyCheck), actions: notifyActions, warn: notifyCheck?.observedState == .denied))
        }
    }

    private func recoveryTitle(for check: ReadinessCheck?) -> String {
        switch check?.observedState {
        case .denied: return "Notifications are blocked in macOS."
        case .limitedDelivery: return "Alerts are off in macOS; only badges are allowed."
        default: return "Notifications need your permission before they can be delivered."
        }
    }

    // MARK: Data

    private func renderData(_ content: NSStackView) {
        let backupCard = card(content, header: "Backup")
        row(
            into: backupCard,
            title: "Backup destination",
            detail: "Not installed — optional. No backup feature is installed in this build; when it exists, its access status and a bounded test will appear here.",
            trailing: chip("Off · Optional", tone: .neu)
        )
    }

    // MARK: Processing

    private func renderProcessing(_ content: NSStackView) {
        let sourcesCard = card(content, header: "Sources")
        row(
            into: sourcesCard,
            title: "No configured sources",
            detail: "Sources are added by their owning feature. Per-source access status and recovery appear beside each source once that feature exists."
        )
    }

    // MARK: Access

    private func renderAccess(_ content: NSStackView) {
        let connectionsCard = card(content, header: "Connections")
        row(
            into: connectionsCard,
            title: "No configured connections",
            detail: "Connection recovery appears beside each connection once the connection feature exists. macOS permissions and Core app-to-Core authority remain separate facts."
        )
    }

    // MARK: Maintenance

    private func renderMaintenance(_ content: NSStackView) {
        let detailsCard = card(content, header: "macOS check details")
        let snapshot = coordinator.snapshot
        let observed = snapshot.observedAt.map { ISO8601DateFormatter().string(from: $0) } ?? "never"
        let details = """
        Generation: \(snapshot.generation)
        Installation: \(snapshot.context.installationRevision)
        Workspace: \(snapshot.context.workspaceRef)
        Observed: \(observed)
        """
        let text = NSTextField(wrappingLabelWithString: details)
        text.font = OvFont.mono(11)
        text.textColor = Ov.textSecondary
        text.preferredMaxLayoutWidth = 560
        let logBox = NSStackView()
        logBox.orientation = .vertical
        logBox.alignment = .leading
        logBox.edgeInsets = NSEdgeInsets(top: 10, left: 12, bottom: 10, right: 12)
        logBox.wantsLayer = true
        logBox.layer?.backgroundColor = Ov.bgInsetDeep.cgColor
        logBox.layer?.cornerRadius = 7
        logBox.addArrangedSubview(text)
        detailsCard.addArrangedSubview(logBox)
        detailsCard.addArrangedSubview(ovSpacer(8))
        detailsCard.addArrangedSubview(
            button("Refresh status", style: .bordered, action: #selector(refreshStatus))
        )

        // Diagnostics are bounded and safe by construction (§14.2): the fields
        // above are the whole export — no paths, no bookmarks, no secrets.
        let note = NSTextField(wrappingLabelWithString: "These details are what diagnostics export contains. Nothing else is recorded.")
        note.font = OvFont.sans(11.5)
        note.textColor = Ov.textTertiary
        note.preferredMaxLayoutWidth = 560
        detailsCard.addArrangedSubview(note)
    }

    // MARK: Helpers

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

// MARK: - The OmniVia brand mark (assets/omnivia-wordmark.svg geometry:
// rounded brick tile, rx 34/120, with two white concentric rings)

final class MarkView: NSView {
    var tile = NSColor(srgbRed: 0x9E / 255.0, green: 0x30 / 255.0, blue: 0x2A / 255.0, alpha: 1)
        { didSet { needsDisplay = true } }

    override func draw(_ dirtyRect: NSRect) {
        guard let context = NSGraphicsContext.current?.cgContext else { return }
        // viewBox geometry: tile 120×120 (rx 34); ring paths on the 80pt circle
        // set (outer ring r 37.8→31.2, inner ring r 23.7→18.3 around centre).
        let size = min(bounds.width, bounds.height)
        context.setFillColor(tile.cgColor)
        let radius = size * 34.0 / 120.0
        let path = NSBezierPath(roundedRect: bounds, xRadius: radius, yRadius: radius)
        path.fill()

        let centre = CGPoint(x: bounds.midX, y: bounds.midY)
        // Ring geometry from the wordmark SVG, normalised to the 120 tile.
        // Outer ring: outer r 37.8, inner 31.2 → annulus centred on tile.
        // Inner ring: outer r 23.7, inner 18.3.
        let rings: [(outer: CGFloat, inner: CGFloat)] = [
            (37.8 / 120.0, 31.2 / 120.0),
            (23.7 / 120.0, 18.3 / 120.0),
        ]
        context.setFillColor(NSColor.white.cgColor)
        for ring in rings {
            let outer = ring.outer * size
            let inner = ring.inner * size
            let outerPath = CGPath(ellipseIn: CGRect(
                x: centre.x - outer, y: centre.y - outer, width: outer * 2, height: outer * 2
            ), transform: nil)
            let innerPath = CGPath(ellipseIn: CGRect(
                x: centre.x - inner, y: centre.y - inner, width: inner * 2, height: inner * 2
            ), transform: nil)
            // Annulus: even-odd fill of the outer ellipse minus the inner one.
            context.addPath(outerPath)
            context.addPath(innerPath)
            context.fillPath(using: .evenOdd)
        }
    }
}

// MARK: - Sidebar row (prototype source-list item: hover + selected pill)

final class SidebarRowView: NSView {
    var isSelected = false { didSet { applySelection() } }
    var onSelect: (() -> Void)?

    private let label: NSTextField
    private let icon: NSImageView

    init(title: String, symbol: String) {
        label = OvFont.label(title, size: 13, color: Ov.textPrimary)
        icon = NSImageView()
        icon.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)?
            .withSymbolConfiguration(.init(pointSize: 15, weight: .regular))
        icon.contentTintColor = Ov.textSecondary
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = 6

        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 9
        row.edgeInsets = NSEdgeInsets(top: 0, left: 8, bottom: 0, right: 8)
        row.translatesAutoresizingMaskIntoConstraints = false
        addSubview(row)
        heightAnchor.constraint(equalToConstant: 28).isActive = true
        NSLayoutConstraint.activate([
            row.topAnchor.constraint(equalTo: topAnchor),
            row.bottomAnchor.constraint(equalTo: bottomAnchor),
            row.leadingAnchor.constraint(equalTo: leadingAnchor),
            row.trailingAnchor.constraint(equalTo: trailingAnchor),
        ])
        row.addArrangedSubview(icon)
        row.addArrangedSubview(label)

        addGestureRecognizer(NSClickGestureRecognizer(target: self, action: #selector(clicked)))
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func applySelection() {
        layer?.backgroundColor = isSelected ? Ov.accent.cgColor : NSColor.clear.cgColor
        label.textColor = isSelected ? Ov.onAccent : Ov.textPrimary
        label.font = OvFont.sans(13, isSelected ? 500 : 400)
        icon.contentTintColor = isSelected ? Ov.onAccent : Ov.textSecondary
    }

    override func layout() {
        super.layout()
        applySelection()
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        addTrackingArea(NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeInKeyWindow],
            owner: self
        ))
    }

    override func mouseEntered(with event: NSEvent) {
        if !isSelected { layer?.backgroundColor = Ov.hoverBg.cgColor }
        icon.contentTintColor = isSelected ? Ov.onAccent : Ov.textPrimary
    }

    override func mouseExited(with event: NSEvent) { applySelection() }

    @objc private func clicked() { onSelect?() }
    override var acceptsFirstResponder: Bool { true }
}

// MARK: - OmniVia buttons (.ov-btn / .ov-btn--bordered / --primary)

enum OvButtonStyle { case bordered, primary }

final class OvButton: NSButton {
    private let style: OvButtonStyle

    init(title: String, style: OvButtonStyle) {
        self.style = style
        super.init(frame: .zero)
        self.title = title
        isBordered = false
        setButtonType(.momentaryPushIn)
        wantsLayer = true
        layer?.cornerRadius = 6
        font = OvFont.sans(13, 500)
        contentTintColor = style == .primary ? Ov.onAccent : Ov.textPrimary
        applyStyle()

        heightAnchor.constraint(equalToConstant: 28).isActive = true
        widthAnchor.constraint(greaterThanOrEqualToConstant: textWidth + 24).isActive = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private var textWidth: CGFloat {
        let attributes: [NSAttributedString.Key: Any] = [.font: font ?? NSFont.systemFont(ofSize: 12.5)]
        return (title as NSString).size(withAttributes: attributes).width
    }

    private func applyStyle() {
        if style == .primary {
            layer?.backgroundColor = Ov.accent.cgColor
            layer?.borderWidth = 0
        } else {
            layer?.backgroundColor = Ov.bgElevated.cgColor
            layer?.borderWidth = 1
            layer?.borderColor = Ov.borderControl.cgColor
        }
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        guard window != nil, trackingAreas.isEmpty else { return }
        addTrackingArea(NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeInKeyWindow], owner: self))
    }

    override func mouseEntered(with event: NSEvent) {
        layer?.backgroundColor = (style == .primary ? Ov.accentActive : Ov.hoverBg).cgColor
    }

    override func mouseExited(with event: NSEvent) { applyStyle() }
}

// MARK: - Small layout helpers

private func ovSpacer(_ height: CGFloat) -> NSView {
    let view = NSView()
    view.translatesAutoresizingMaskIntoConstraints = false
    view.heightAnchor.constraint(equalToConstant: height).isActive = true
    return view
}
