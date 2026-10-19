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
    private let activityStore: CaptureActivityStore
    /// Records what the durable owners now prove and returns the activity. The
    /// composition owns the reading, so this window never touches the queue. It
    /// throws when nothing could be recorded or re-read, and the surface then
    /// reports its activity as unavailable instead of showing stale facts as
    /// current.
    private let observeActivity: () throws -> [CaptureActivityRecord]
    /// Retained root groups. Read through a closure so the window never holds the
    /// access store, which owns bookmarks this surface has no business seeing.
    private let rootGroups: () -> [NativeRootAccessDescriptor]
    /// The document routes this Mac holds, read through the same kind of closure and for the
    /// same reason. The projections carry no bookmark: this surface only asks which documents
    /// still have a route of their own.
    private let documentRoutes: () -> [NativeAccessDescriptor]
    /// The authenticated application-operation transport for the two canonical document
    /// operations this surface uses. `nil` when no client could be resolved, which is a
    /// state the detail pane states rather than a reason to show nothing.
    private let historyTransport: CoreDocumentHistoryTransport?
    /// Locate (design §9.5) for one file, handed what Core last said about it so the owner can
    /// compare the chosen file with the last captured version. The window owns no picker and
    /// no bookmark: the picker, the comparison, the confirmation and the relink are the one
    /// owner's own, invoked with the file and nothing else.
    private let locateOriginal: ((FileSubscription, CoreDocumentHistory?) -> Void)?
    /// The Locate requests Core has not yet confirmed, read through a closure so the window
    /// never holds the access store. Projections only: no bookmark, no path.
    private let pendingRelinks: () -> [OriginalRelinkProgress]
    /// Answers whether the identity this Mac is authenticated for is the one a
    /// watch froze, after pausing every watch that is not. Resuming or checking
    /// a watch reads its source, so neither runs on a "no": a watch paused for a
    /// changed account or target is not resumed from here. `nil` means no
    /// authority exists to ask, which is also a "no".
    private let identityAuthorises: ((CaptureDeliveryBinding) async -> Bool)?
    /// Releases the intents held for `pauses` back to pending and runs a delivery
    /// pass, which checks the identity and the target again. `nil` offers no
    /// Try again.
    private let releaseHeld: ((Set<CaptureQueuePause>) async -> Void)?
    /// Source-level Disconnect for one file (design §13.3): revokes it here and records what
    /// Core is owed. Returns whether the revocation was made durable. `nil` offers none.
    private let disconnectSource: ((FileSubscription) async -> Bool)?
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
    private let revealButton = NSButton(title: "Show recovery details", target: nil, action: nil)
    private let clearButton = NSButton(title: "Clear completed activity", target: nil, action: nil)
    private let reprocessButton = NSButton(
        title: "Reprocess captured copy…", target: nil, action: nil
    )
    private let locateButton = NSButton(title: "Locate…", target: nil, action: nil)
    private let tryAgainButton = NSButton(title: "Try again", target: nil, action: nil)
    private let disconnectButton = NSButton(title: "Disconnect…", target: nil, action: nil)
    private var segment = FilesAndFoldersSegment.files
    private var statusFilter = FilesAndFoldersStatusFilter.all
    /// Readable by tests so the projection they assert is the one this window built.
    private(set) var model: FilesAndFoldersModel
    private var items: [FilesAndFoldersItem] = []
    /// What Core last said about the selected document. Held here rather than rebuilt
    /// from the stores, because no store holds it. Internal rather than private so the
    /// enforcement tests can hold one exact answer; nothing outside this module sees it.
    var historyState = CoreHistoryState.notRequested
    /// Which read the answer in hand belongs to. A selection change bumps it, so an
    /// answer that arrives after the user moved on is dropped instead of rendered
    /// against the wrong file.
    private var historyGeneration = 0

    public init(
        workspaceName: String,
        folderStore: FolderSubscriptionStore,
        folderMonitoring: FolderMonitoringCoordinator,
        fileStore: FileSubscriptionStore,
        fileMonitoring: FileMonitoringCoordinator,
        bulkStore: FolderBulkRunStore,
        bulkCoordinator: FolderBulkRunCoordinator,
        activityStore: CaptureActivityStore,
        observeActivity: @escaping () throws -> [CaptureActivityRecord],
        rootGroups: @escaping () -> [NativeRootAccessDescriptor],
        documentRoutes: @escaping () -> [NativeAccessDescriptor],
        historyTransport: CoreDocumentHistoryTransport? = nil,
        locateOriginal: ((FileSubscription, CoreDocumentHistory?) -> Void)? = nil,
        pendingRelinks: @escaping () -> [OriginalRelinkProgress] = { [] },
        identityAuthorises: ((CaptureDeliveryBinding) async -> Bool)? = nil,
        releaseHeld: ((Set<CaptureQueuePause>) async -> Void)? = nil,
        disconnectSource: ((FileSubscription) async -> Bool)? = nil
    ) {
        self.workspaceName = workspaceName
        self.folderStore = folderStore
        self.folderMonitoring = folderMonitoring
        self.fileStore = fileStore
        self.fileMonitoring = fileMonitoring
        self.bulkStore = bulkStore
        self.bulkCoordinator = bulkCoordinator
        self.activityStore = activityStore
        self.observeActivity = observeActivity
        self.rootGroups = rootGroups
        self.documentRoutes = documentRoutes
        self.historyTransport = historyTransport
        self.locateOriginal = locateOriginal
        self.pendingRelinks = pendingRelinks
        self.identityAuthorises = identityAuthorises
        self.releaseHeld = releaseHeld
        self.disconnectSource = disconnectSource
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

    public func tableViewSelectionDidChange(_ notification: Notification) {
        loadCoreHistory()
        updateControls()
    }

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
        revealButton.target = self
        revealButton.action = #selector(showRecoveryDetails)
        clearButton.target = self
        clearButton.action = #selector(clearCompletedActivity)
        reprocessButton.target = self
        reprocessButton.action = #selector(reprocessCapturedCopy)
        reprocessButton.setAccessibilityLabel("Reprocess captured copy")
        locateButton.target = self
        locateButton.action = #selector(locateOriginalFile)
        locateButton.setAccessibilityLabel("Locate the original file")
        tryAgainButton.target = self
        tryAgainButton.action = #selector(tryAgain)
        tryAgainButton.setAccessibilityLabel("Try again once access or the update is resolved")
        disconnectButton.target = self
        disconnectButton.action = #selector(disconnectFile)
        disconnectButton.setAccessibilityLabel("Disconnect this file in this workspace on this Mac")

        let searchSpacer = NSView()
        searchSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let filters = NSStackView(views: [search, statusFilterButton, searchSpacer, workspaceLabel])
        filters.orientation = .horizontal
        filters.spacing = 8
        let actionSpacer = NSView()
        actionSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let actions = NSStackView(
            views: [
                statusLabel, actionSpacer, clearButton, revealButton, tryAgainButton, locateButton,
                reprocessButton, checkButton, pauseResumeButton, stopButton, disconnectButton,
            ]
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

    /// Re-reads the durable stores behind the window. The composition root calls it when work
    /// that finished off the main actor (a Locate) changed what the window lists.
    public func refresh() { reload() }

    func reload() {
        let folders = folderStore.all()
        let runs = bulkStore.all()
        // Records first, so the surface shows what the durable owners prove now
        // rather than what they proved when this window was last open. If that
        // fails, nothing was saved and nothing current could be read: the last
        // records this process holds are shown, labelled as unavailable, and no
        // action acts on them.
        let activity: [CaptureActivityRecord]
        let availability: CaptureActivityAvailability
        do {
            activity = try observeActivity()
            availability = .current
        } catch {
            activity = activityStore.all()
            availability = .unavailable
        }
        model = FilesAndFoldersModel(
            workspaceName: workspaceName,
            // A disconnected source is no longer an entry here (design §13.3 step 6).
            files: FilesAndFoldersModel.connectedFiles(fileStore.all(), routes: documentRoutes()),
            folders: folders,
            retainedRoots: FilesAndFoldersModel.retainedRoots(
                groups: rootGroups(),
                runs: runs,
                folders: folders
            ),
            runs: runs,
            activity: activity,
            activityAvailability: availability,
            independentlyRoutedDocuments: FilesAndFoldersModel.independentlyRoutedDocuments(
                routes: documentRoutes()
            ),
            coreHistory: historyState,
            relinks: pendingRelinks()
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
        let actions = FilesAndFoldersModel.actions(
            for: selected,
            activity: model.activityAvailability,
            coreHistory: historyState,
            relinks: model.relinks
        )
        pauseResumeButton.title = actions.pauseTitle
        pauseResumeButton.isEnabled = actions.pauseEnabled
        stopButton.title = actions.stopTitle
        stopButton.isEnabled = actions.stopEnabled
        checkButton.isEnabled = actions.checkEnabled
        revealButton.isHidden = segment != .recentAdditions
        revealButton.isEnabled = actions.revealEnabled
        tryAgainButton.isHidden = segment != .recentAdditions
        tryAgainButton.isEnabled = actions.tryAgainEnabled && releaseHeld != nil
        clearButton.isHidden = segment != .recentAdditions
        clearButton.isEnabled = model.canMutateActivity && model.clearableActivityCount > 0
        // Both canonical controls belong to one file, so neither appears on a surface
        // that is not listing files. Reprocess is enabled only when Core reported the
        // action available; Locate only when the central decision permits it -- a wired
        // owner and a selected file are necessary, never sufficient.
        reprocessButton.isHidden = segment != .files
        reprocessButton.isEnabled = actions.reprocessEnabled
        locateButton.isHidden = segment != .files
        locateButton.isEnabled = permitsLocate(for: selected)
        disconnectButton.isHidden = segment != .files
        disconnectButton.isEnabled = actions.disconnectEnabled && disconnectSource != nil
        detailView.string = model.detail(for: selected)
            .map { "\($0.label): \($0.value)" }
            .joined(separator: "\n")
        if selected == nil {
            detailView.string = segment == .recentAdditions
                ? "Select an addition to see what this Mac records about each of its files."
                : "Select a file or folder to see what Core records about it."
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
        Task { await applyPauseResume(selected) }
    }

    /// Whether a watch may read its source now. Resuming and checking both do,
    /// so both ask first: the answer comes after every watch frozen against
    /// another account or target has been paused, and a "no" leaves the watch
    /// exactly as it was.
    private func mayReadSource(of item: FilesAndFoldersItem) async -> Bool {
        let frozen: CaptureDeliveryBinding
        switch item {
        case let .file(value): frozen = CaptureDeliveryBinding(value)
        case let .folder(value): frozen = CaptureDeliveryBinding(value)
        default: return true
        }
        if await identityAuthorises?(frozen) == true { return true }
        statusLabel.stringValue = "Paused: this was added under a different account or Core "
            + "target than the one signed in now. Nothing was read."
        reload()
        return false
    }

    func applyPauseResume(_ selected: FilesAndFoldersItem) async {
        switch selected {
        case let .folder(value):
            let resume = value.state == .paused || value.state == .needsAccess
                || value.state == .reviewRequired
            if resume {
                guard await mayReadSource(of: selected) else { return }
                _ = try? await folderMonitoring.resume(subscriptionID: value.subscriptionID)
            } else {
                _ = try? await folderMonitoring.pause(subscriptionID: value.subscriptionID)
            }
        case let .file(value):
            let resume = value.state == .paused || value.state == .needsAccess
            if resume {
                guard await mayReadSource(of: selected) else { return }
                _ = try? await fileMonitoring.resume(subscriptionID: value.subscriptionID)
            } else {
                _ = try? await fileMonitoring.pause(subscriptionID: value.subscriptionID)
            }
        case let .activity(value):
            // Only a frozen folder run has an authority that can retry or
            // pause. An ordinary submission has none, and its buttons are
            // disabled rather than silently doing something else.
            guard let run = value.run else { break }
            if run.state == .paused {
                _ = try? await bulkCoordinator.resume(runID: run.runID)
                _ = await bulkCoordinator.advance(runID: run.runID)
            } else if run.attentionCount > 0 {
                // Acts on the same frozen membership; discovers nothing.
                _ = try? await bulkCoordinator.retryUnavailable(runID: run.runID)
                _ = await bulkCoordinator.advance(runID: run.runID)
            } else {
                _ = try? await bulkCoordinator.pause(runID: run.runID)
            }
        case .retainedRoot:
            // Nothing watches a retained root, so there is nothing to pause.
            break
        }
        reload()
    }

    @objc private func stopUpdating() {
        guard let selected else { return }
        let isRun: Bool
        if case .activity = selected { isRun = true } else { isRun = false }
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
            case let .activity(value):
                guard let frozen = value.run else { break }
                // The exact stopped count, read back from the durable ledger.
                let run = try? await bulkCoordinator.stopRemaining(runID: frozen.runID)
                outcome = "Stopped \(run?.stoppedCount ?? frozen.plannedCount) of "
                    + "\(frozen.members.count) files. Already added files remain in Core "
                    + "and stay linked for manual refresh."
            case .retainedRoot:
                break
            }
            reload()
            if let outcome { statusLabel.stringValue = outcome }
        }
    }

    /// The confirmation is the design's own wording (§13.3), and Cancel is the default.
    @objc private func disconnectFile() {
        guard let selected, case .file = selected else { return }
        let alert = NSAlert()
        alert.messageText = Self.disconnectConfirmation
        alert.informativeText = selected.displayName
        alert.addButton(withTitle: "Cancel")
        alert.addButton(withTitle: "Disconnect")
        guard alert.runModal() == .alertSecondButtonReturn else { return }
        Task { _ = await applyDisconnect(selected) }
    }

    static let disconnectConfirmation = "Disconnect this file in this workspace on this Mac? "
        + "Core will stop reading it through all matching links here, including watched "
        + "folders. Captured versions and already queued additions remain. Your original "
        + "file will not change."

    /// Revokes the file here, at once, and lets the owner record and reconcile what Core is
    /// owed. Returns whether the revocation was made durable; Core being offline does not
    /// change that and is not waited for.
    @discardableResult
    func applyDisconnect(_ selected: FilesAndFoldersItem) async -> Bool {
        guard let disconnectSource, case let .file(value) = selected,
              FilesAndFoldersModel.actions(for: selected).disconnectEnabled
        else { return false }
        let revoked = await disconnectSource(value)
        reload()
        statusLabel.stringValue = revoked
            ? "Disconnected \(selected.displayName). Core will be told when it is reachable."
            : "Could not disconnect \(selected.displayName). Nothing changed."
        return revoked
    }

    @objc private func checkNow() {
        guard let selected else { return }
        statusLabel.stringValue = "Checking \(selected.displayName)…"
        Task { await applyCheck(selected) }
    }

    func applyCheck(_ selected: FilesAndFoldersItem) async {
        switch selected {
        case let .folder(value):
            guard await mayReadSource(of: selected) else { return }
            _ = await folderMonitoring.reconcile(subscriptionIDs: [value.subscriptionID])
        case let .file(value):
            guard await mayReadSource(of: selected) else { return }
            _ = await fileMonitoring.reconcile(subscriptionIDs: [value.subscriptionID])
        case let .activity(value):
            guard let run = value.run else { break }
            _ = await bulkCoordinator.advance(runID: run.runID)
        case .retainedRoot:
            break
        }
        reload()
    }

    /// The person's own statement that access or the update is resolved. It
    /// releases the reasons the selected addition is held for (a reason belongs
    /// to the target, so it covers every item held for it), then lets the next
    /// delivery pass decide: that pass checks the identity and the target again,
    /// so a refusal that still stands holds the item again and sends nothing.
    @objc private func tryAgain() {
        guard let selected else { return }
        Task { await applyTryAgain(selected) }
    }

    @discardableResult
    func applyTryAgain(_ selected: FilesAndFoldersItem) async -> Bool {
        guard let releaseHeld, case let .activity(group) = selected, group.heldCount > 0,
              model.canMutateActivity
        else { return false }
        statusLabel.stringValue = "Trying \(selected.displayName) again…"
        await releaseHeld(group.heldPauses)
        reload()
        return true
    }

    /// Reveals what an unknown outcome is recovered by. It reads the durable
    /// activity record and allocates nothing: no replacement key, no
    /// destination change and no removal.
    @objc private func showRecoveryDetails() {
        let recovery = model.recoveryDetail(for: selected)
        guard !recovery.isEmpty else { return }
        detailView.string = (model.detail(for: selected) + recovery)
            .map { "\($0.label): \($0.value)" }
            .joined(separator: "\n")
    }

    /// Whether Locate may be offered for `item`, or taken on it.
    ///
    /// One rule in one place, because the button's enabled state and the action's own
    /// check must not be able to disagree: a pass that left the button enabled is not a
    /// permission. Design §12.1 offers original actions "only if binding permits" and
    /// §7.2 gives a provisional route no linked-file capability, so this consumes the
    /// central decision instead of the two local facts it used to -- that a file is
    /// selected and an owner is wired, which are necessary and not sufficient.
    ///
    /// Fail-closed: no owner, a selection that is not a file, a requested but
    /// unconfirmed association, a snapshot-only capture, a held answer about another
    /// document, a stale read, a read that never happened and a relink already waiting on
    /// Core all permit nothing.
    func permitsLocate(for item: FilesAndFoldersItem?) -> Bool {
        guard locateOriginal != nil, case .file? = item else { return false }
        return FilesAndFoldersModel.actions(
            for: item,
            activity: model.activityAvailability,
            coreHistory: historyState,
            // Read now, not from the last reload: a provisional grant must refuse here
            // even if the list on screen predates its promotion or its creation, and a
            // relink Core has not answered must refuse a second candidate.
            provisionalAccessRefs: FilesAndFoldersModel.provisionalAccessRefs(routes: documentRoutes()),
            relinks: pendingRelinks()
        ).locateEnabled
    }

    /// Enforces that rule again from the facts held now, then hands the selection to the
    /// one owner of Locate. Returns whether the owner was invoked, which is what makes the
    /// refused path observable without a click.
    @discardableResult
    func invokeLocate(for item: FilesAndFoldersItem?) -> Bool {
        guard permitsLocate(for: item), let locateOriginal, case let .file(value)? = item else {
            return false
        }
        locateOriginal(value, historyState.availability(for: value.linkedDocumentID).currentHistory)
        reload()
        return true
    }

    /// Reads the selected file's canonical history from Core, off the main actor.
    ///
    /// One read per selection, and the answer is applied only if the selection has not
    /// moved since. A failed read after a good one keeps the good one and marks it stale,
    /// which is what lets the detail pane keep saying something true while saying that it
    /// may have moved on. A selection that is not a linked file holds no history at all.
    private func loadCoreHistory() {
        guard case let .file(value)? = selected else {
            historyGeneration += 1
            historyState = .notRequested
            model = model.replacingCoreHistory(.notRequested)
            return
        }
        guard let historyTransport else {
            historyState = CoreHistoryState(
                documentID: value.linkedDocumentID, availability: .unavailable(.unavailable)
            )
            model = model.replacingCoreHistory(historyState)
            return
        }
        historyGeneration += 1
        let generation = historyGeneration
        let documentID = value.linkedDocumentID
        let previous = historyState.availability(for: documentID).history
        historyState = CoreHistoryState(documentID: documentID, availability: .loading)
        model = model.replacingCoreHistory(historyState)
        let reader = CoreDocumentHistoryReader(transport: historyTransport)
        Task.detached(priority: .userInitiated) {
            let outcome = reader.read(documentID: documentID)
            await MainActor.run { [weak self] in
                guard let self, self.historyGeneration == generation else { return }
                switch outcome {
                case let .success(history):
                    self.historyState = CoreHistoryState(
                        documentID: documentID, availability: .current(history)
                    )
                case let .failure(error):
                    self.historyState = CoreHistoryState(
                        documentID: documentID,
                        availability: previous.map { .stale($0) } ?? .unavailable(error)
                    )
                }
                self.model = self.model.replacingCoreHistory(self.historyState)
                self.updateControls()
            }
        }
    }

    /// Processes one retained capture again, with the exact profile Core approved for it.
    ///
    /// Everything sent comes out of the history read: the capture, the profile reference
    /// and revision, and the extraction head the read observed. Nothing is composed here,
    /// the original is never read, and the outcome shown is the receipt's own -- a
    /// historical-only result says so rather than reading as a success.
    @objc private func reprocessCapturedCopy() {
        guard let selected, let historyTransport,
              let chosen = chooseReprocess(selected, name: selected.displayName),
              let intent = model.reprocessIntent(
                  for: selected,
                  captureID: chosen.captureID,
                  profileDigest: chosen.profileDigest
              )
        else { return }
        let alert = NSAlert()
        alert.messageText = "Process this captured copy of \(selected.displayName) again?"
        alert.informativeText = intent.confirmationMessage
        alert.addButton(withTitle: "Reprocess captured copy")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        statusLabel.stringValue = "Processing the captured copy of \(selected.displayName)…"
        reprocessButton.isEnabled = false
        Task.detached(priority: .userInitiated) { [historyTransport, intent] in
            let outcome = historyTransport.reprocess(
                captureID: intent.capture.captureID,
                profileRef: intent.profile.profileRef,
                revision: intent.profile.revision,
                expectedProfileDigest: intent.profile.profileDigest,
                expectedBaseExtractionID: intent.expectedBaseExtractionID,
                operationKey: intent.operationKey
            )
            await MainActor.run { [weak self] in
                guard let self else { return }
                let sentence: String
                switch outcome {
                case let .success(receipt):
                    sentence = receipt.outcomeSentence
                case let .failure(error):
                    sentence = Self.reprocessRefusal(error)
                }
                // Re-read before reporting, so the detail pane and the sentence state the
                // same facts and neither is this surface's own memory of them.
                self.loadCoreHistory()
                self.reload()
                self.statusLabel.stringValue = sentence
            }
        }
    }

    /// What a refused or unanswered reprocess actually proves. A conflict is Core's
    /// decision and says what changed; an unanswered call says the outcome is unknown and
    /// offers no new key.
    private static func reprocessRefusal(_ error: CoreHistoryError) -> String {
        switch error {
        case .unavailable:
            return "Core could not be reached from this Mac. Nothing was processed."
        case .timedOut:
            return "Checking status… Core did not answer in time. This request is "
                + "reconciled under its existing key; nothing new was sent."
        case .unreadable:
            return "Update required. Core answered with a document this version cannot read."
        case let .refused(code):
            switch code {
            case "conflict":
                return "Not processed. The captured copy or its source changed in Core "
                    + "since this history was read. The history has been re-read."
            case "idempotency_conflict":
                return "Checking status… this request was already sent with different "
                    + "details. Its original outcome stands."
            case "not_found":
                return "Not processed. Core holds no such captured copy."
            case "workspace_not_granted", "authorization_denied", "authentication_required",
                 "capability_not_granted":
                return "Access to this workspace is required before a captured copy can be "
                    + "processed again."
            default:
                return "Not processed. Core refused the request (\(code))."
            }
        }
    }

    /// The exact capture and the exact approved profile, chosen explicitly.
    ///
    /// One list of (captured copy, approved processing) pairs, built by the model so the
    /// pairing is tested rather than assembled here. There is no free text and nothing
    /// composed: every pair is a capture Core reports as retained and reprocessable with
    /// one of that capture's own approved selections.
    private func chooseReprocess(
        _ item: FilesAndFoldersItem,
        name: String
    ) -> (captureID: String, profileDigest: String)? {
        let pairs = model.reprocessOptions(for: item)
        guard !pairs.isEmpty else { return nil }
        let choice = NSPopUpButton()
        choice.setAccessibilityLabel("Captured copy and processing to use")
        choice.addItems(
            withTitles: pairs.map {
                FilesAndFoldersModel.reprocessOptionTitle(capture: $0.capture, profile: $0.profile)
            }
        )
        let label = NSTextField(labelWithString: "Captured copy and processing")
        let accessory = NSStackView(views: [label, choice])
        accessory.orientation = .vertical
        accessory.alignment = .leading
        accessory.spacing = 4
        accessory.frame = NSRect(x: 0, y: 0, width: 460, height: 56)
        let alert = NSAlert()
        alert.messageText = "Choose the captured copy of \(name) to process again"
        alert.informativeText = "Only copies Core reports as retained are listed, each with "
            + "the processing it approves for that copy. The original file is not read."
        alert.accessoryView = accessory
        alert.addButton(withTitle: "Continue")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn,
              pairs.indices.contains(choice.indexOfSelectedItem)
        else { return nil }
        let chosen = pairs[choice.indexOfSelectedItem]
        return (chosen.capture.captureID, chosen.profile.profileDigest)
    }

    /// The button's own selector. It recomputes the decision from the selection and the
    /// held answer as they are now, rather than trusting a button an earlier pass left
    /// enabled. This window chooses no file, holds no bookmark and replaces no frozen key.
    @objc private func locateOriginalFile() {
        invokeLocate(for: selected)
    }

    @objc private func clearCompletedActivity() {
        let clearable = model.clearableActivityCount
        // Clearing decides from a count. A count this surface could not refresh
        // is not one to decide from.
        guard model.canMutateActivity, clearable > 0 else { return }
        let alert = NSAlert()
        alert.messageText = "Clear \(clearable) completed activity "
            + (clearable == 1 ? "receipt?" : "receipts?")
        alert.informativeText = "This removes local receipts for finished additions only. "
            + "Additions still queued, sending or checking stay listed, their recovery keys and "
            + "queued copies are kept, and original links, running watches, folder run manifests "
            + "and everything already in Core are unchanged."
        alert.addButton(withTitle: "Clear receipts")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let removed = (try? activityStore.clearReceipts()) ?? 0
        reload()
        statusLabel.stringValue = "Cleared \(removed) completed "
            + (removed == 1 ? "receipt" : "receipts")
            + ". Queued work, links, watches and run manifests are unchanged."
    }
}
