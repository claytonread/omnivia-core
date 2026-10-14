import AppKit
import DocumentIntakeMacOS

@MainActor
public final class FolderManagementWindowController: NSWindowController,
    NSTableViewDataSource, NSTableViewDelegate
{
    private enum Row {
        case file(FileSubscription)
        case folder(FolderSubscription)
        case bulkRun(FolderBulkRun)

        var id: String {
            switch self {
            case let .file(value): value.subscriptionID
            case let .folder(value): value.subscriptionID
            case let .bulkRun(value): value.runID
            }
        }

        var displayName: String {
            switch self {
            case let .file(value): value.displayName
            case let .folder(value): value.rootDisplayName
            case let .bulkRun(value): value.rootDisplayName
            }
        }

        var subscriptionState: FolderSubscriptionState? {
            switch self {
            case let .file(value): value.state
            case let .folder(value): value.state
            case .bulkRun: nil
            }
        }

        var isActive: Bool {
            switch self {
            case .file(let value): value.state == .active
            case .folder(let value): value.state == .active
            case .bulkRun(let value): value.state == .adding
            }
        }
    }

    private let folderStore: FolderSubscriptionStore
    private let folderMonitoring: FolderMonitoringCoordinator
    private let fileStore: FileSubscriptionStore
    private let fileMonitoring: FileMonitoringCoordinator
    private let bulkStore: FolderBulkRunStore
    private let bulkCoordinator: FolderBulkRunCoordinator
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
        fileMonitoring: FileMonitoringCoordinator,
        bulkStore: FolderBulkRunStore,
        bulkCoordinator: FolderBulkRunCoordinator
    ) {
        self.folderStore = folderStore
        self.folderMonitoring = folderMonitoring
        self.fileStore = fileStore
        self.fileMonitoring = fileMonitoring
        self.bulkStore = bulkStore
        self.bulkCoordinator = bulkCoordinator
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
        case ("kind", .bulkRun): text = "Folder run"
        case ("status", let .file(file)): text = Self.label(file.state)
        case ("status", let .folder(folder)): text = Self.label(folder.state)
        case ("status", let .bulkRun(run)): text = Self.label(run.state)
        case ("files", .file): text = "1"
        case ("files", let .folder(folder)): text = String(folder.members.count)
        case ("files", let .bulkRun(run)): text = "\(run.admittedCount)/\(run.members.count)"
        case ("checked", let .file(file)): text = file.lastCheckedAt ?? "Not yet"
        case ("checked", let .folder(folder)):
            text = folder.reviewChangeCount.map { "\($0) changes require review" }
                ?? folder.lastCompletedMetadataScanAt
                ?? "Not yet"
        case ("checked", let .bulkRun(run)):
            // Separate per-file facts, never one whole-folder success.
            text = "\(run.admittedCount) added · \(run.publishedCount) in progress · "
                + "\(run.plannedCount) waiting · \(run.stoppedCount) stopped · "
                + "\(run.attentionCount) needs attention"
                + (Self.isTerminal(run.state)
                    ? " · links retained for manual refresh" : "")
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
        let bulkRuns = bulkStore.all().map(Row.bulkRun)
        rows = (files + folders + bulkRuns).sorted {
            if $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedSame {
                return $0.id < $1.id
            }
            return $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending
        }
        table.reloadData()
        if rows.isEmpty {
            statusLabel.stringValue = "No files or folders are kept updated."
        } else {
            let active = rows.filter(\.isActive).count
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
        switch selected {
        case let .bulkRun(run):
            // Resuming is not retrying: a member that already failed keeps its
            // own outcome until Retry unavailable files is chosen for it.
            pauseResumeButton.title = run.state == .paused
                ? "Resume"
                : run.attentionCount > 0 ? "Retry unavailable files" : "Pause"
            pauseResumeButton.isEnabled = [.adding, .paused, .needsAttention].contains(run.state)
            stopButton.title = "Stop adding remaining files"
            stopButton.isEnabled = run.plannedCount > 0
            checkButton.isEnabled = run.state == .adding
        case .file, .folder:
            let state = selected.subscriptionState
            pauseResumeButton.title = state == .reviewRequired
                ? "Continue changes"
                : state == .paused || state == .needsAccess ? "Resume" : "Pause"
            pauseResumeButton.isEnabled = state.map {
                [.active, .paused, .needsAccess, .reviewRequired].contains($0)
            } ?? false
            stopButton.title = "Stop updating"
            stopButton.isEnabled = state != .stopped
            checkButton.isEnabled = state == .active
        }
    }

    private var selected: Row? {
        guard rows.indices.contains(table.selectedRow) else { return nil }
        return rows[table.selectedRow]
    }

    @objc private func pauseResume() {
        guard let selected else { return }
        Task {
            switch selected {
            case let .folder(value):
                let resume = value.state == .paused || value.state == .needsAccess
                    || value.state == .reviewRequired
                if resume {
                    _ = try? await folderMonitoring.resume(subscriptionID: value.subscriptionID)
                } else {
                    _ = try? await folderMonitoring.pause(subscriptionID: value.subscriptionID)
                }
            case let .file(value):
                let resume = value.state == .paused || value.state == .needsAccess
                if resume {
                    _ = try? await fileMonitoring.resume(subscriptionID: value.subscriptionID)
                } else {
                    _ = try? await fileMonitoring.pause(subscriptionID: value.subscriptionID)
                }
            case let .bulkRun(value):
                if value.state == .paused {
                    _ = try? await bulkCoordinator.resume(runID: value.runID)
                    _ = await bulkCoordinator.advance(runID: value.runID)
                } else if value.attentionCount > 0 {
                    // Acts on the same frozen membership; discovers nothing.
                    _ = try? await bulkCoordinator.retryUnavailable(runID: value.runID)
                    _ = await bulkCoordinator.advance(runID: value.runID)
                } else {
                    _ = try? await bulkCoordinator.pause(runID: value.runID)
                }
            }
            reload()
        }
    }

    @objc private func stopUpdating() {
        guard let selected else { return }
        let alert = NSAlert()
        alert.messageText = selected.subscriptionState == nil
            ? "Stop adding remaining files from \(selected.displayName)?"
            : "Stop updating \(selected.displayName)?"
        alert.informativeText = selected.subscriptionState == nil
            ? "Already published files continue and already added files stay in Core. Only files that have not started will be stopped. Files already captured remain linked for manual refresh."
            : "Core will stop adding new changes. Existing files will remain linked for manual refresh, and additions already queued will continue."
        alert.addButton(
            withTitle: selected.subscriptionState == nil
                ? "Stop adding remaining files" : "Stop updating"
        )
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        Task {
            var outcome: String?
            switch selected {
            case let .folder(value):
                _ = try? await folderMonitoring.stop(subscriptionID: value.subscriptionID)
            case let .file(value):
                _ = try? await fileMonitoring.stop(subscriptionID: value.subscriptionID)
            case let .bulkRun(value):
                // The exact stopped count, read back from the durable ledger.
                let run = try? await bulkCoordinator.stopRemaining(runID: value.runID)
                outcome = "Stopped \(run?.stoppedCount ?? value.plannedCount) of "
                    + "\(value.members.count) files. Already added files remain in Core "
                    + "and stay linked for manual refresh."
            }
            reload()
            if let outcome { statusLabel.stringValue = outcome }
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
            case let .bulkRun(value):
                _ = await bulkCoordinator.advance(runID: value.runID)
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

    private static func isTerminal(_ state: FolderBulkRunState) -> Bool {
        state == .complete || state == .stopped
    }

    private static func label(_ state: FolderBulkRunState) -> String {
        switch state {
        case .adding: "Adding"
        case .paused: "Paused"
        case .complete: "Added"
        case .needsAttention: "Needs attention"
        case .stopped: "Stopped"
        }
    }
}
