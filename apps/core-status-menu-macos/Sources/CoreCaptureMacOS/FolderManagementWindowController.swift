import AppKit
import DocumentIntakeMacOS

@MainActor
public final class FolderManagementWindowController: NSWindowController,
    NSTableViewDataSource, NSTableViewDelegate
{
    private let store: FolderSubscriptionStore
    private let monitoring: FolderMonitoringCoordinator
    private let table = NSTableView()
    private let statusLabel = NSTextField(labelWithString: "")
    private let pauseResumeButton = NSButton(title: "Pause", target: nil, action: nil)
    private let stopButton = NSButton(title: "Stop watching", target: nil, action: nil)
    private let checkButton = NSButton(title: "Check now", target: nil, action: nil)
    private var subscriptions: [FolderSubscription] = []

    public init(store: FolderSubscriptionStore, monitoring: FolderMonitoringCoordinator) {
        self.store = store
        self.monitoring = monitoring
        super.init(window: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }

    public func openOrFocus() {
        if window == nil { window = makeWindow() }
        reload()
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    public func numberOfRows(in tableView: NSTableView) -> Int { subscriptions.count }

    public func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard row < subscriptions.count, let identifier = tableColumn?.identifier else { return nil }
        let subscription = subscriptions[row]
        let text: String
        switch identifier.rawValue {
        case "folder": text = subscription.rootDisplayName
        case "status": text = Self.label(subscription.state)
        case "files": text = String(subscription.members.count)
        case "checked": text = subscription.lastCompletedMetadataScanAt ?? "Not yet"
        default: text = ""
        }
        let field = NSTextField(labelWithString: text)
        field.lineBreakMode = .byTruncatingMiddle
        field.toolTip = text
        return field
    }

    public func tableViewSelectionDidChange(_ notification: Notification) { updateControls() }

    private func makeWindow() -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 650, height: 380),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Files & Folders"
        window.center()

        for (id, title, width) in [
            ("folder", "Folder", 220.0),
            ("status", "Status", 130.0),
            ("files", "Files", 70.0),
            ("checked", "Last checked", 180.0),
        ] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id))
            column.title = title
            column.width = width
            table.addTableColumn(column)
        }
        table.delegate = self
        table.dataSource = self
        table.allowsMultipleSelection = false
        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder

        pauseResumeButton.target = self
        pauseResumeButton.action = #selector(pauseResume)
        stopButton.target = self
        stopButton.action = #selector(stopWatching)
        checkButton.target = self
        checkButton.action = #selector(checkNow)
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let actions = NSStackView(views: [statusLabel, spacer, checkButton, pauseResumeButton, stopButton])
        actions.orientation = .horizontal
        actions.spacing = 8
        let root = NSStackView(views: [scroll, actions])
        root.orientation = .vertical
        root.spacing = 12
        root.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        root.translatesAutoresizingMaskIntoConstraints = false
        let host = NSView()
        host.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: host.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: host.trailingAnchor),
            root.topAnchor.constraint(equalTo: host.topAnchor),
            root.bottomAnchor.constraint(equalTo: host.bottomAnchor),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 270),
        ])
        window.contentView = host
        return window
    }

    private func reload() {
        subscriptions = store.all()
        table.reloadData()
        statusLabel.stringValue = subscriptions.isEmpty
            ? "No folders are being watched."
            : "\(subscriptions.count) folder watch(es)"
        updateControls()
    }

    private func updateControls() {
        guard table.selectedRow >= 0, table.selectedRow < subscriptions.count else {
            pauseResumeButton.isEnabled = false
            stopButton.isEnabled = false
            checkButton.isEnabled = false
            return
        }
        let state = subscriptions[table.selectedRow].state
        pauseResumeButton.title = state == .paused || state == .needsAccess ? "Resume" : "Pause"
        pauseResumeButton.isEnabled = state == .active || state == .paused || state == .needsAccess
        stopButton.isEnabled = state != .stopped
        checkButton.isEnabled = state == .active
    }

    private var selected: FolderSubscription? {
        guard table.selectedRow >= 0, table.selectedRow < subscriptions.count else { return nil }
        return subscriptions[table.selectedRow]
    }

    @objc private func pauseResume() {
        guard let selected else { return }
        Task {
            if selected.state == .paused || selected.state == .needsAccess {
                _ = try? await monitoring.resume(subscriptionID: selected.subscriptionID)
            } else {
                _ = try? await monitoring.pause(subscriptionID: selected.subscriptionID)
            }
            reload()
        }
    }

    @objc private func stopWatching() {
        guard let selected else { return }
        let alert = NSAlert()
        alert.messageText = "Stop watching \(selected.rootDisplayName)?"
        alert.informativeText = "Captured files remain in Core. New folder changes will no longer be added automatically."
        alert.addButton(withTitle: "Stop watching")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        Task {
            _ = try? await monitoring.stop(subscriptionID: selected.subscriptionID)
            reload()
        }
    }

    @objc private func checkNow() {
        guard let selected else { return }
        statusLabel.stringValue = "Checking \(selected.rootDisplayName)…"
        Task {
            _ = await monitoring.reconcile(subscriptionIDs: [selected.subscriptionID])
            reload()
        }
    }

    private static func label(_ state: FolderSubscriptionState) -> String {
        switch state {
        case .waitingForCore: "Waiting for Core"
        case .active: "Watching"
        case .paused: "Paused"
        case .reviewRequired: "Review required"
        case .needsAccess: "Needs folder access"
        case .stopped: "Stopped"
        }
    }
}
