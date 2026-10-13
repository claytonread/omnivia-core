import AppKit
import DocumentIntakeMacOS

@MainActor
public final class FolderManagementWindowController: NSWindowController,
    NSTableViewDataSource, NSTableViewDelegate
{
    private enum Row {
        case file(FileSubscription)
        case folder(FolderSubscription)

        var id: String {
            switch self {
            case let .file(value): value.subscriptionID
            case let .folder(value): value.subscriptionID
            }
        }

        var displayName: String {
            switch self {
            case let .file(value): value.displayName
            case let .folder(value): value.rootDisplayName
            }
        }

        var state: FolderSubscriptionState {
            switch self {
            case let .file(value): value.state
            case let .folder(value): value.state
            }
        }
    }

    private let folderStore: FolderSubscriptionStore
    private let folderMonitoring: FolderMonitoringCoordinator
    private let fileStore: FileSubscriptionStore
    private let fileMonitoring: FileMonitoringCoordinator
    private let table = NSTableView()
    private let statusLabel = NSTextField(labelWithString: "")
    private let pauseResumeButton = NSButton(title: "Pause", target: nil, action: nil)
    private let stopButton = NSButton(title: "Stop updating", target: nil, action: nil)
    private let checkButton = NSButton(title: "Check now", target: nil, action: nil)
    private var rows: [Row] = []

    public init(
        folderStore: FolderSubscriptionStore,
        folderMonitoring: FolderMonitoringCoordinator,
        fileStore: FileSubscriptionStore,
        fileMonitoring: FileMonitoringCoordinator
    ) {
        self.folderStore = folderStore
        self.folderMonitoring = folderMonitoring
        self.fileStore = fileStore
        self.fileMonitoring = fileMonitoring
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

    public func numberOfRows(in tableView: NSTableView) -> Int { rows.count }

    public func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard rows.indices.contains(row), let identifier = tableColumn?.identifier else { return nil }
        let value = rows[row]
        let text: String
        switch (identifier.rawValue, value) {
        case ("source", _): text = value.displayName
        case ("kind", .file): text = "File"
        case ("kind", .folder): text = "Folder"
        case ("status", _): text = Self.label(value.state)
        case ("files", .file): text = "1"
        case ("files", let .folder(folder)): text = String(folder.members.count)
        case ("checked", let .file(file)): text = file.lastCheckedAt ?? "Not yet"
        case ("checked", let .folder(folder)): text = folder.lastCompletedMetadataScanAt ?? "Not yet"
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
            contentRect: NSRect(x: 0, y: 0, width: 760, height: 410),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Files & Folders"
        window.center()

        for (id, title, width) in [
            ("source", "Source", 245.0),
            ("kind", "Kind", 65.0),
            ("status", "Status", 125.0),
            ("files", "Files", 55.0),
            ("checked", "Last checked", 185.0),
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
        stopButton.action = #selector(stopUpdating)
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
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 290),
        ])
        window.contentView = host
        return window
    }

    private func reload() {
        let files = fileStore.all().map(Row.file)
        let folders = folderStore.all().map(Row.folder)
        rows = (files + folders).sorted {
            if $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedSame {
                return $0.id < $1.id
            }
            return $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending
        }
        table.reloadData()
        if rows.isEmpty {
            statusLabel.stringValue = "No files or folders are kept updated."
        } else {
            let active = rows.filter { $0.state == .active }.count
            statusLabel.stringValue = "\(active) active · \(rows.count) total"
        }
        updateControls()
    }

    private func updateControls() {
        guard let selected else {
            pauseResumeButton.isEnabled = false
            stopButton.isEnabled = false
            checkButton.isEnabled = false
            return
        }
        pauseResumeButton.title = selected.state == .paused || selected.state == .needsAccess
            ? "Resume" : "Pause"
        pauseResumeButton.isEnabled = [.active, .paused, .needsAccess].contains(selected.state)
        stopButton.isEnabled = selected.state != .stopped
        checkButton.isEnabled = selected.state == .active
    }

    private var selected: Row? {
        guard rows.indices.contains(table.selectedRow) else { return nil }
        return rows[table.selectedRow]
    }

    @objc private func pauseResume() {
        guard let selected else { return }
        Task {
            let resume = selected.state == .paused || selected.state == .needsAccess
            switch selected {
            case let .folder(value):
                if resume {
                    _ = try? await folderMonitoring.resume(subscriptionID: value.subscriptionID)
                } else {
                    _ = try? await folderMonitoring.pause(subscriptionID: value.subscriptionID)
                }
            case let .file(value):
                if resume {
                    _ = try? await fileMonitoring.resume(subscriptionID: value.subscriptionID)
                } else {
                    _ = try? await fileMonitoring.pause(subscriptionID: value.subscriptionID)
                }
            }
            reload()
        }
    }

    @objc private func stopUpdating() {
        guard let selected else { return }
        let alert = NSAlert()
        alert.messageText = "Stop updating \(selected.displayName)?"
        alert.informativeText = "Captured content remains in Core. Future changes will no longer be added automatically."
        alert.addButton(withTitle: "Stop updating")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        Task {
            switch selected {
            case let .folder(value):
                _ = try? await folderMonitoring.stop(subscriptionID: value.subscriptionID)
            case let .file(value):
                _ = try? await fileMonitoring.stop(subscriptionID: value.subscriptionID)
            }
            reload()
        }
    }

    @objc private func checkNow() {
        guard let selected else { return }
        statusLabel.stringValue = "Checking \(selected.displayName)…"
        Task {
            switch selected {
            case let .folder(value):
                _ = await folderMonitoring.reconcile(subscriptionIDs: [value.subscriptionID])
            case let .file(value):
                _ = await fileMonitoring.reconcile(subscriptionIDs: [value.subscriptionID])
            }
            reload()
        }
    }

    private static func label(_ state: FolderSubscriptionState) -> String {
        switch state {
        case .waitingForCore: "Waiting for Core"
        case .active: "Watching"
        case .paused: "Paused"
        case .reviewRequired: "Review required"
        case .needsAccess: "Needs access"
        case .stopped: "Stopped"
        }
    }
}
