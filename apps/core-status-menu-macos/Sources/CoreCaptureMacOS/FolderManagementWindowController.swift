import AppKit
import DocumentIntakeMacOS

@MainActor
public final class FolderManagementWindowController: NSWindowController,
    NSTableViewDataSource, NSTableViewDelegate, NSSearchFieldDelegate
{
    private let workspaceName: String
    private let folderStore: FolderSubscriptionStore
    private let folderMonitoring: FolderMonitoringCoordinator
    private let fileStore: FileSubscriptionStore
    private let fileMonitoring: FileMonitoringCoordinator
    private let bulkStore: FolderBulkRunStore
    private let bulkCoordinator: FolderBulkRunCoordinator
    /// Retained root groups. Read through a closure so the window never holds the
    /// access store, which owns bookmarks this surface has no business seeing.
    private let rootGroups: () -> [NativeRootAccessDescriptor]
    private let segments = NSSegmentedControl(
        labels: FilesAndFoldersSegment.allCases.map(\.title),
        trackingMode: .selectOne,
        target: nil,
        action: nil
    )
    private let search = NSSearchField()
    private let statusFilterButton = NSPopUpButton()
    private let workspaceLabel = NSTextField(labelWithString: "")
    private let table = NSTableView()
    private let detailView = NSTextView()
    private let statusLabel = NSTextField(labelWithString: "")
    private let pauseResumeButton = NSButton(title: "Pause", target: nil, action: nil)
    private let stopButton = NSButton(title: "Stop updating", target: nil, action: nil)
    private let checkButton = NSButton(title: "Check now", target: nil, action: nil)
    private var segment = FilesAndFoldersSegment.files
    private var statusFilter = FilesAndFoldersStatusFilter.all
    private var model: FilesAndFoldersModel
    private var items: [FilesAndFoldersItem] = []

    public init(
        workspaceName: String,
        folderStore: FolderSubscriptionStore,
        folderMonitoring: FolderMonitoringCoordinator,
        fileStore: FileSubscriptionStore,
        fileMonitoring: FileMonitoringCoordinator,
        bulkStore: FolderBulkRunStore,
        bulkCoordinator: FolderBulkRunCoordinator,
        rootGroups: @escaping () -> [NativeRootAccessDescriptor]
    ) {
        self.workspaceName = workspaceName
        self.folderStore = folderStore
        self.folderMonitoring = folderMonitoring
        self.fileStore = fileStore
        self.fileMonitoring = fileMonitoring
        self.bulkStore = bulkStore
        self.bulkCoordinator = bulkCoordinator
        self.rootGroups = rootGroups
        model = FilesAndFoldersModel(workspaceName: workspaceName)
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

    public func numberOfRows(in tableView: NSTableView) -> Int { items.count }

    public func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard items.indices.contains(row), let identifier = tableColumn?.identifier else { return nil }
        let item = items[row]
        let text: String
        switch identifier.rawValue {
        case "source": text = item.displayName
        case "status": text = item.statusLabel
        case "files": text = item.countText
        default: text = item.activityText
        }
        let field = NSTextField(labelWithString: text)
        field.lineBreakMode = .byTruncatingMiddle
        field.toolTip = text
        return field
    }

    public func tableViewSelectionDidChange(_ notification: Notification) { updateControls() }

    public func controlTextDidChange(_ notification: Notification) { applyFilters() }

    private func makeWindow() -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 800, height: 560),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Files and folders"
        window.center()

        for (id, title, width) in [
            ("source", "Source", 265.0),
            ("status", "Status", 125.0),
            ("files", "Files", 60.0),
            ("activity", segment.activityColumnTitle, 300.0),
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

        segments.selectedSegment = segment.rawValue
        segments.target = self
        segments.action = #selector(segmentChanged)
        search.placeholderString = "Search by name or identifier"
        search.delegate = self
        search.sendsWholeSearchString = false
        statusFilterButton.target = self
        statusFilterButton.action = #selector(filterChanged)
        rebuildStatusFilter()
        workspaceLabel.stringValue = "Workspace: \(workspaceName)"
        workspaceLabel.textColor = .secondaryLabelColor

        detailView.isEditable = false
        detailView.isSelectable = true
        detailView.drawsBackground = false
        detailView.isVerticallyResizable = true
        detailView.isHorizontallyResizable = false
        detailView.autoresizingMask = [.width]
        detailView.textContainer?.widthTracksTextView = true
        let detailScroll = NSScrollView()
        detailScroll.documentView = detailView
        detailScroll.hasVerticalScroller = true
        detailScroll.borderType = .bezelBorder

        pauseResumeButton.target = self
        pauseResumeButton.action = #selector(pauseResume)
        stopButton.target = self
        stopButton.action = #selector(stopUpdating)
        checkButton.target = self
        checkButton.action = #selector(checkNow)

        let searchSpacer = NSView()
        searchSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let filters = NSStackView(views: [search, statusFilterButton, searchSpacer, workspaceLabel])
        filters.orientation = .horizontal
        filters.spacing = 8
        let actionSpacer = NSView()
        actionSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let actions = NSStackView(
            views: [statusLabel, actionSpacer, checkButton, pauseResumeButton, stopButton]
        )
        actions.orientation = .horizontal
        actions.spacing = 8
        let root = NSStackView(views: [segments, filters, scroll, detailScroll, actions])
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
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 240),
            detailScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 140),
            search.widthAnchor.constraint(greaterThanOrEqualToConstant: 220),
        ])
        window.contentView = host
        return window
    }

    private func rebuildStatusFilter() {
        let applicable = FilesAndFoldersStatusFilter.applicable(to: segment)
        statusFilterButton.removeAllItems()
        statusFilterButton.addItems(withTitles: applicable.map(\.title))
        if !applicable.contains(statusFilter) { statusFilter = .all }
        statusFilterButton.selectItem(at: applicable.firstIndex(of: statusFilter) ?? 0)
    }

    private func reload() {
        let folders = folderStore.all()
        let runs = bulkStore.all()
        model = FilesAndFoldersModel(
            workspaceName: workspaceName,
            files: fileStore.all(),
            folders: folders,
            retainedRoots: FilesAndFoldersModel.retainedRoots(
                groups: rootGroups(),
                runs: runs,
                folders: folders
            ),
            runs: runs
        )
        applyFilters()
    }

    private func applyFilters() {
        let selectedID = selected?.identifier
        items = model.items(
            segment: segment,
            search: search.stringValue,
            statusFilter: statusFilter
        )
        table.tableColumns.last?.title = segment.activityColumnTitle
        table.reloadData()
        if let selectedID, let row = items.firstIndex(where: { $0.identifier == selectedID }) {
            table.selectRowIndexes([row], byExtendingSelection: false)
        }
        statusLabel.stringValue = model.summary(items: items, segment: segment)
        updateControls()
    }

    private func updateControls() {
        let selected = self.selected
        let actions = FilesAndFoldersModel.actions(for: selected)
        pauseResumeButton.title = actions.pauseTitle
        pauseResumeButton.isEnabled = actions.pauseEnabled
        stopButton.title = actions.stopTitle
        stopButton.isEnabled = actions.stopEnabled
        checkButton.isEnabled = actions.checkEnabled
        detailView.string = model.detail(for: selected)
            .map { "\($0.label): \($0.value)" }
            .joined(separator: "\n")
        if selected == nil {
            detailView.string = "Select a file or folder to see what Core records about it."
        }
    }

    private var selected: FilesAndFoldersItem? {
        guard items.indices.contains(table.selectedRow) else { return nil }
        return items[table.selectedRow]
    }

    @objc private func segmentChanged() {
        segment = FilesAndFoldersSegment(rawValue: segments.selectedSegment) ?? .files
        rebuildStatusFilter()
        applyFilters()
    }

    @objc private func filterChanged() {
        let applicable = FilesAndFoldersStatusFilter.applicable(to: segment)
        guard applicable.indices.contains(statusFilterButton.indexOfSelectedItem) else { return }
        statusFilter = applicable[statusFilterButton.indexOfSelectedItem]
        applyFilters()
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
            case .retainedRoot:
                // Nothing watches a retained root, so there is nothing to pause.
                break
            }
            reload()
        }
    }

    @objc private func stopUpdating() {
        guard let selected else { return }
        let isRun: Bool
        if case .bulkRun = selected { isRun = true } else { isRun = false }
        let alert = NSAlert()
        alert.messageText = isRun
            ? "Stop adding remaining files from \(selected.displayName)?"
            : "Stop updating \(selected.displayName)?"
        alert.informativeText = isRun
            ? "Already published files continue and already added files stay in Core. Only files that have not started will be stopped. Files already captured remain linked for manual refresh."
            : "Core will stop adding new changes. Existing files will remain linked for manual refresh, and additions already queued will continue."
        alert.addButton(withTitle: isRun ? "Stop adding remaining files" : "Stop updating")
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
            case .retainedRoot:
                break
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
            case .retainedRoot:
                break
            }
            reload()
        }
    }
}
