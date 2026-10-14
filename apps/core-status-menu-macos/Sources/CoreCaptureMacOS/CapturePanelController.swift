import AppKit
import DocumentIntakeMacOS

@MainActor
public final class CapturePanelController: NSObject, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate {
    private var window: NSWindow?
    private var draft = CaptureDraft()
    private let workspaceName: String
    private let submitter: ManualCaptureSubmission
    private let folderSubmitter: FolderCaptureSubmission?
    private let onQueued: (@MainActor () -> Void)?
    private let table = NSTableView()
    private let primaryButton = NSButton(title: "Add", target: nil, action: nil)
    private let statusLabel = NSTextField(labelWithString: "")
    private let note = NSTextView()
    private let keepUpdated = NSButton(
        checkboxWithTitle: "Keep selected files updated",
        target: nil,
        action: nil
    )
    private let receiptsLabel = NSTextField(wrappingLabelWithString: "")
    private var waitingSelections: [[ManualCaptureCandidate]] = []
    /// One receipt per confirmed or declined group, oldest first. Groups never
    /// share a receipt: the files, each import-once folder and each
    /// keep-updated folder report only for themselves.
    private(set) var receipts: [CaptureGroupReceipt] = []
    /// A group's confirmation is in flight until its receipt is recorded, so a
    /// second activation cannot publish the same group twice. Other groups are
    /// unaffected.
    private var fileGroupInFlight = false
    private var foldersInFlight: Set<URL> = []
    /// False only for lifecycle tests, which drive the draft without a window.
    private let opensWindow: Bool

    public convenience init(
        workspaceName: String,
        submitter: ManualCaptureSubmission,
        folderSubmitter: FolderCaptureSubmission? = nil,
        onQueued: (@MainActor () -> Void)? = nil
    ) {
        self.init(
            workspaceName: workspaceName,
            submitter: submitter,
            folderSubmitter: folderSubmitter,
            onQueued: onQueued,
            opensWindow: true
        )
    }

    init(
        workspaceName: String,
        submitter: ManualCaptureSubmission,
        folderSubmitter: FolderCaptureSubmission?,
        onQueued: (@MainActor () -> Void)?,
        opensWindow: Bool
    ) {
        self.workspaceName = workspaceName
        self.submitter = submitter
        self.folderSubmitter = folderSubmitter
        self.onQueued = onQueued
        self.opensWindow = opensWindow
        super.init()
    }

    public func openOrFocus() {
        guard opensWindow else { return }
        if let window {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let built = buildWindow()
        window = built
        built.delegate = self
        built.center()
        built.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Opens review and the reviewed file chooser.
    ///
    /// This is the Share extension's "Link an original…" continuation. The
    /// original is chosen here, through the same fixed `CapturePickerPolicy`
    /// every other pick uses — never from the copy a share sheet handed over,
    /// which is not an original and is never monitored.
    public func openOriginalChooser() {
        guard opensWindow else { return }
        openOrFocus()
        chooseFilesAction()
    }

    public func addIncoming(_ urls: [URL], surface: CaptureSurface = .manual) {
        _ = addIncoming(urls, surface: surface, source: .original)
    }

    public func addIncomingSnapshots(_ urls: [URL], surface: CaptureSurface) {
        _ = addIncoming(urls, surface: surface, source: .capturedSnapshot)
    }

    @discardableResult
    private func addIncoming(
        _ urls: [URL],
        surface: CaptureSurface,
        source: ManualCaptureCandidate.Source
    ) -> Bool {
        let candidates = urls.map {
            ManualCaptureCandidate(url: $0, surface: surface, source: source)
        }
        return addIncoming(candidates)
    }

    /// True only when review owns this batch (or already owns the same stable
    /// candidates). Durable inbox callers use the result as their
    /// acknowledgement boundary and retain their request when review is full.
    @discardableResult
    public func addIncomingAdopted(_ candidates: [ManualCaptureCandidate]) -> Bool {
        addIncoming(candidates, discardOnRefusal: false)
    }

    /// Every candidate handed in ends up in exactly one place: in review, waiting
    /// behind the current selection, or released. Nothing is dropped holding a
    /// grant or owned bytes.
    @discardableResult
    private func addIncoming(
        _ candidates: [ManualCaptureCandidate],
        discardOnRefusal: Bool = true
    ) -> Bool {
        // A native candidate already in review or waiting is the same candidate
        // arriving again. Leave it, and the resources it names, untouched:
        // releasing the repeat would release the live candidate's grant.
        let live = draft.nativeCandidateRefs
            .union(waitingSelections.flatMap { $0 }.compactMap { $0.native?.candidateRef })
        let incoming = candidates.filter { candidate in
            candidate.native.map { !live.contains($0.candidateRef) } ?? true
        }
        guard !incoming.isEmpty else { return true }
        openOrFocus()
        if draft.items.isEmpty {
            add(incoming)
            return true
        } else if waitingSelections.count < 10,
                  waitingSelections.reduce(0, { $0 + $1.count }) + incoming.count <= 100
        {
            waitingSelections.append(incoming)
            statusLabel.stringValue = "New selection waiting · \(incoming.count) item(s). Finish the current selection first."
            return true
        } else {
            if discardOnRefusal { submitter.discardUnsubmitted(incoming) }
            statusLabel.stringValue = "Incoming selection limit reached. Finish a selection and try again."
            return false
        }
    }

    public func windowWillClose(_ notification: Notification) { window = nil }

    private func buildWindow() -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 400, height: 540),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Add to Core"
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(srgbRed: 0.12, green: 0.12, blue: 0.13, alpha: 1)
        window.minSize = NSSize(width: 380, height: 460)

        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 14
        root.edgeInsets = NSEdgeInsets(top: 22, left: 20, bottom: 18, right: 20)
        root.translatesAutoresizingMaskIntoConstraints = false

        let title = NSTextField(labelWithString: "Add to Core")
        title.font = .systemFont(ofSize: 22, weight: .bold)
        root.addArrangedSubview(title)
        let subtitle = NSTextField(wrappingLabelWithString: "Paste a note or choose files. Original files stay in their current folders.")
        subtitle.textColor = .secondaryLabelColor
        root.addArrangedSubview(subtitle)

        let noteScroll = NSScrollView()
        noteScroll.hasVerticalScroller = true
        noteScroll.borderType = .bezelBorder
        noteScroll.documentView = note
        note.font = .systemFont(ofSize: 14)
        note.string = ""
        note.isRichText = false
        root.addArrangedSubview(noteScroll)
        noteScroll.heightAnchor.constraint(equalToConstant: 92).isActive = true
        noteScroll.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true

        let chooseFiles = NSButton(title: "Choose files…", target: self, action: #selector(chooseFilesAction))
        chooseFiles.keyEquivalent = "o"
        chooseFiles.keyEquivalentModifierMask = [.command]
        let chooseFolder = NSButton(title: "Choose folder…", target: self, action: #selector(chooseFolderAction))
        chooseFolder.keyEquivalent = "o"
        chooseFolder.keyEquivalentModifierMask = [.command, .shift]
        let remove = NSButton(title: "Remove", target: self, action: #selector(removeSelectedAction))
        remove.toolTip = "Takes the selected item out of this selection without adding it."
        let chooser = NSStackView(views: [chooseFiles, chooseFolder, remove])
        chooser.spacing = 8
        root.addArrangedSubview(chooser)
        keepUpdated.state = .off
        keepUpdated.toolTip = "Core will add new versions when these files change while OmniVia Core is running."
        root.addArrangedSubview(keepUpdated)

        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("selection"))
        column.title = "Selection"
        table.addTableColumn(column)
        table.headerView = nil
        table.rowHeight = 34
        table.dataSource = self
        table.delegate = self
        table.target = self
        table.doubleAction = #selector(reviewSelectedFolder)
        let listScroll = NSScrollView()
        listScroll.documentView = table
        listScroll.hasVerticalScroller = true
        listScroll.borderType = .bezelBorder
        root.addArrangedSubview(listScroll)
        listScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 150).isActive = true
        listScroll.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true

        let destination = NSTextField(labelWithString: "Destination: \(workspaceName)")
        destination.font = .systemFont(ofSize: 12, weight: .medium)
        root.addArrangedSubview(destination)

        statusLabel.textColor = .secondaryLabelColor
        statusLabel.font = .systemFont(ofSize: 12)
        statusLabel.maximumNumberOfLines = 2
        root.addArrangedSubview(statusLabel)

        receiptsLabel.textColor = .secondaryLabelColor
        receiptsLabel.font = .systemFont(ofSize: 11)
        receiptsLabel.maximumNumberOfLines = 6
        root.addArrangedSubview(receiptsLabel)
        receiptsLabel.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true

        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let cancel = NSButton(title: "Cancel", target: self, action: #selector(closeAction))
        cancel.keyEquivalent = "\u{1b}"
        primaryButton.target = self
        primaryButton.action = #selector(submitAction)
        primaryButton.keyEquivalent = "\r"
        primaryButton.keyEquivalentModifierMask = [.command]
        primaryButton.bezelStyle = .rounded
        primaryButton.contentTintColor = NSColor(srgbRed: 0.82, green: 0.46, blue: 0.42, alpha: 1)
        let actions = NSStackView(views: [spacer, cancel, primaryButton])
        actions.spacing = 8
        root.addArrangedSubview(actions)
        actions.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true

        let host = CapturePanelDropView { [weak self] urls in
            self?.add(urls, surface: .capturePanelDrop)
        }
        host.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: host.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: host.trailingAnchor),
            root.topAnchor.constraint(equalTo: host.topAnchor),
            root.bottomAnchor.constraint(equalTo: host.bottomAnchor),
        ])
        window.contentView = host
        refresh()
        return window
    }

    /// Applies one reviewed picker policy. Both native pickers are configured
    /// here and nowhere else, so neither can drift from `CapturePickerPolicy`.
    private static func panel(for policy: CapturePickerPolicy) -> NSOpenPanel {
        let panel = NSOpenPanel()
        panel.canChooseFiles = policy.canChooseFiles
        panel.canChooseDirectories = policy.canChooseDirectories
        panel.allowsMultipleSelection = policy.allowsMultipleSelection
        panel.treatsFilePackagesAsDirectories = policy.treatsFilePackagesAsDirectories
        panel.resolvesAliases = policy.resolvesAliases
        return panel
    }

    @objc private func chooseFilesAction() {
        let panel = Self.panel(for: .files)
        // `panel.urls` is the order the person chose, and review keeps it.
        if panel.runModal() == .OK { add(panel.urls, surface: .manual) }
    }

    private func reviewFolder(_ root: URL, removeFromDraft: Bool = false) {
        guard let folderSubmitter else {
            statusLabel.stringValue = "Folder import isn’t available in this build."
            return
        }
        let mode = NSPopUpButton()
        mode.addItems(withTitles: ["Add current files", "Watch folder"])
        let existing = NSPopUpButton()
        existing.addItems(withTitles: ["Include existing files", "Only new and changed files"])
        existing.toolTip = "Applies when Watch folder is selected."
        let recursive = NSButton(checkboxWithTitle: "Include subfolders", target: nil, action: nil)
        recursive.state = .on
        let hidden = NSButton(checkboxWithTitle: "Include hidden files", target: nil, action: nil)
        hidden.state = .off
        let destination = NSTextField(
            labelWithString: "Destination: \(folderSubmitter.destinationWorkspaceRef)"
        )
        let options = NSStackView(views: [destination, mode, existing, recursive, hidden])
        options.orientation = .vertical
        options.alignment = .leading
        options.spacing = 8
        options.frame = NSRect(x: 0, y: 0, width: 300, height: 140)
        let alert = NSAlert()
        alert.messageText = "Add folder to Core"
        alert.informativeText = """
            \(root.lastPathComponent) · supported formats only.
            Excluded: hidden items, symbolic links and Finder aliases, packages, \
            other mounted volumes, temporary and lock files, dependency and \
            cache folders, and Core's own stores.
            Original files stay in their current folders. Core keeps captured versions.
            """
        alert.accessoryView = options
        alert.addButton(withTitle: "Review files")
        alert.addButton(withTitle: "Cancel")
        let confirmed = alert.runModal() == .alertFirstButtonReturn
        let policy = FolderCapturePolicy(
            mode: mode.indexOfSelectedItem == 1 ? .keepUpdated : .importOnce,
            recursive: recursive.state == .on,
            includeHidden: hidden.state == .on,
            watchInitialContent: existing.indexOfSelectedItem == 1
                ? .onlyNewAndChanged : .includeExisting
        )
        guard confirmed else {
            declineFolder(root, mode: policy.mode)
            return
        }
        let key = root.standardizedFileURL
        guard foldersInFlight.insert(key).inserted else {
            statusLabel.stringValue = "\(root.lastPathComponent): already being reviewed."
            return
        }
        let title = root.lastPathComponent
        statusLabel.stringValue = "\(title): Reviewing folder scope…"
        Task {
            defer { foldersInFlight.remove(key) }
            do {
                let plan = try await Task.detached(priority: .userInitiated) {
                    try folderSubmitter.plan(root: root, policy: policy)
                }.value
                // A scope the scan could not fully read keeps the draft and
                // enrols nothing, the same as a scan that hit a bound.
                if let coverage = plan.coverageDetail {
                    let refusal = NSAlert()
                    refusal.messageText = "Add folder to Core"
                    refusal.informativeText = """
                        \(coverage)
                        Read so far: \(plan.scopeDetail).
                        No files have been added.
                        """
                    refusal.addButton(withTitle: "OK")
                    refusal.runModal()
                    record(Self.folderReceipt(title: title, mode: policy.mode, outcome: .failed, detail: coverage))
                    return
                }
                // A scan that hit a bound keeps the draft and enrols nothing.
                if let limit = plan.limit {
                    let refusal = NSAlert()
                    refusal.messageText = "Add folder to Core"
                    refusal.informativeText = """
                        \(limit.reviewDetail)
                        Reviewed so far: \(plan.scopeDetail).
                        No files have been added.
                        """
                    refusal.addButton(withTitle: "OK")
                    refusal.runModal()
                    record(Self.folderReceipt(title: title, mode: policy.mode, outcome: .failed, detail: limit.reviewDetail))
                    return
                }
                let confirmation = NSAlert()
                confirmation.messageText = policy.mode == .keepUpdated
                    ? "Watch this folder?"
                    : "Add the current files in this folder?"
                let promise = policy.mode == .keepUpdated
                    ? policy.watchInitialContent == .onlyNewAndChanged
                        ? "Existing files are checked to recognise later changes. Their contents are not added until a later change is detected."
                        : "Core will add new and changed supported files while OmniVia Core is running."
                    : "Captured files remain linked for manual refresh. New files will not be added automatically."
                confirmation.informativeText = """
                    \(plan.rootDisplayName) · destination \(plan.destinationWorkspaceRef) · \
                    subfolders \(policy.recursive ? "included" : "excluded")
                    \(plan.scopeDetail), from \(plan.examinedEntries) examined items.
                    \(Self.exclusionDetail(plan))
                    Original files stay in their current folders. Core keeps captured versions.
                    \(promise)
                    Files are captured as they are read. This is not a backup of the folder at one instant.
                    """
                confirmation.addButton(
                    withTitle: policy.mode == .keepUpdated ? "Watch folder" : "Add current files"
                )
                confirmation.addButton(withTitle: "Cancel")
                guard confirmation.runModal() == .alertFirstButtonReturn else {
                    declineFolder(root, mode: policy.mode)
                    return
                }
                statusLabel.stringValue = "\(title): " + (policy.mode == .keepUpdated
                    && policy.watchInitialContent == .onlyNewAndChanged
                    ? "Setting baseline… Core checks existing files to recognise later changes without adding them."
                    : "Preparing current folder versions…")
                if await confirmFolder(plan, removingReviewRow: removeFromDraft ? root : nil).outcome
                    != .failed
                {
                    onQueued?()
                }
            } catch {
                record(Self.folderReceipt(
                    title: title,
                    mode: policy.mode,
                    outcome: .failed,
                    detail: Self.folderFailureDetail(error)
                ))
            }
        }
    }

    /// Publishes one reviewed folder plan as its own group and reports only for
    /// it. Nothing else in review is read, published or released: a pending
    /// ordinary-file group and every other folder row are left exactly as they
    /// were, and a failure keeps this folder's own row in review to retry.
    func confirmFolder(
        _ plan: FolderCapturePlan,
        removingReviewRow root: URL? = nil
    ) async -> CaptureGroupReceipt {
        let title = plan.rootDisplayName
        let mode = plan.policy.mode
        guard let folderSubmitter else {
            return record(Self.folderReceipt(
                title: title, mode: mode, outcome: .failed,
                detail: "Folder import isn’t available in this build."
            ))
        }
        do {
            let result = try await folderSubmitter.submitWithReceipt(plan)
            // The folder is queued now, and the work that queued it adopted
            // its own root grant under its own owner. So the row leaves
            // review and the provisional candidate it held is released —
            // its root owner and its durable record — which never touches
            // the grant the submitted work holds.
            if let root,
               let index = draft.items.firstIndex(where: {
                   $0.url.standardizedFileURL == root.standardizedFileURL
               }),
               let removed = draft.remove(at: index)
            {
                if let owned = removed.ownedCandidate {
                    submitter.discardUnsubmitted([owned])
                }
            }
            let excludedBaseline = mode == .keepUpdated
                && plan.policy.watchInitialContent == .onlyNewAndChanged
            // Folder addition queued, never all files added: the manifest
            // is frozen and each child reports its own outcome.
            let progress = "Folder addition queued · \(plan.members.count) files frozen · "
                + "\(result.entries.count) queued on this Mac · the rest continue in bounded batches."
            return record(Self.folderReceipt(
                title: title,
                mode: mode,
                submissionID: result.submissionID,
                outcome: .queued(queued: result.entries.count),
                detail: excludedBaseline
                    ? "Setting baseline… Existing files will not be added."
                    : mode == .keepUpdated
                    ? "\(progress) Watching starts after every baseline file is confirmed by Core."
                    : progress
            ))
        } catch {
            return record(Self.folderReceipt(
                title: title,
                mode: mode,
                outcome: .failed,
                detail: Self.folderFailureDetail(error)
            ))
        }
    }

    /// The person declined this folder at its own dialog. Only this group's
    /// receipt says so, and its row stays in review for a later decision.
    func declineFolder(_ root: URL, mode: FolderCaptureMode) {
        record(Self.folderReceipt(
            title: root.lastPathComponent,
            mode: mode,
            outcome: .cancelled,
            detail: "Folder review cancelled. Nothing was added."
        ))
    }

    private static func folderReceipt(
        title: String,
        mode: FolderCaptureMode,
        submissionID: String? = nil,
        outcome: CaptureGroupReceipt.Outcome,
        detail: String
    ) -> CaptureGroupReceipt {
        CaptureGroupReceipt(
            id: CaptureGroupReceipt.newID(),
            kind: mode == .keepUpdated ? .folderKeepUpdated : .folderImportOnce,
            title: title,
            submissionID: submissionID,
            outcome: outcome,
            detail: detail
        )
    }

    private static func folderFailureDetail(_ error: Error) -> String {
        switch error as? FolderCaptureSubmissionError {
        case .emptyScope?:
            return "No supported documents were found in that scope."
        case .coverageIncomplete?:
            return "Part of this folder could not be read, so it is not the whole folder. Check access to it and try again. No files were added."
        case .scanLimitExceeded?:
            return "This folder exceeds a reviewed limit. Choose a smaller scope or add exclusions. No files were added."
        case .unsafeRoot?:
            return "Choose a narrower document folder, not a filesystem or home folder."
        case .duplicateWatch?:
            return "This folder is already watched for this destination."
        case .watchLimitExceeded?:
            return "This Mac already has 20 active watched folders. Stop one before adding another."
        default:
            return "Couldn’t queue this folder. Check access, scope, and size limits."
        }
    }

    /// Appends a group's receipt, shows it, and returns it. The status line
    /// always names the group it reports on.
    @discardableResult
    private func record(_ receipt: CaptureGroupReceipt) -> CaptureGroupReceipt {
        receipts.append(receipt)
        if receipts.count > 20 { receipts.removeFirst(receipts.count - 20) }
        statusLabel.stringValue = receipt.summary
        receiptsLabel.stringValue = receipts.suffix(4).reversed().map(\.summary).joined(separator: "\n")
        receiptsLabel.toolTip = receipts.suffix(4).reversed()
            .map { "\($0.id) · \($0.submissionID ?? "no submission")" }
            .joined(separator: "\n")
        refresh()
        return receipt
    }

    /// Names the actual reasons items were left out, with their counts, rather
    /// than one undifferentiated skipped total.
    private static func exclusionDetail(_ plan: FolderCapturePlan) -> String {
        let reasons = plan.exclusionCounts
            .filter { $0.value > 0 }
            .sorted { $0.key.rawValue < $1.key.rawValue }
            .map { "\($0.key.reviewDetail): \($0.value)" }
        return reasons.isEmpty ? "Nothing was excluded." : reasons.joined(separator: " · ")
    }

    @objc private func chooseFolderAction() {
        let panel = Self.panel(for: .folder)
        if panel.runModal() == .OK, let root = panel.url { reviewFolder(root) }
    }

    private func add(
        _ urls: [URL],
        surface: CaptureSurface,
        source: ManualCaptureCandidate.Source = .original
    ) {
        do {
            try draft.add(urls, surface: surface, source: source)
            describeDraft()
        } catch CaptureDraftError.tooManyItems {
            statusLabel.stringValue = "Choose no more than 10 files at a time."
        } catch CaptureDraftError.duplicateItem {
            statusLabel.stringValue = "That item is already in this selection."
        } catch {
            statusLabel.stringValue = "That item is not a supported document."
        }
        refresh()
    }

    private func add(_ candidates: [ManualCaptureCandidate]) {
        do {
            try draft.add(candidates)
            describeDraft()
        } catch {
            // The draft takes a selection whole or not at all, so none of these
            // candidates is in review and all of them are still ours to release.
            submitter.discardUnsubmitted(candidates)
            switch error as? CaptureDraftError {
            case .tooManyItems:
                statusLabel.stringValue = "Choose no more than 10 files at a time."
            case .duplicateItem:
                statusLabel.stringValue = "That item is already in this selection."
            default:
                statusLabel.stringValue = "That item is not a supported document."
            }
        }
        refresh()
    }

    /// One sentence for what the current review list now needs. A refused item
    /// is named first, because nothing is published while one is still listed.
    private func describeDraft() {
        if draft.containsUnsupported {
            statusLabel.stringValue = "One or more chosen items aren’t supported documents. Select each one and choose Remove."
        } else if draft.containsSnapshots {
            keepUpdated.state = .off
            statusLabel.stringValue = "Captured copies are ready for review. They are not linked to temporary source files."
        } else if draft.containsFolders {
            statusLabel.stringValue = draft.canSubmitFiles
                ? "Files and each folder are added separately. Add the files, or double-click a folder to review its scope."
                : "Double-click a folder to review its scope before adding it."
        } else {
            statusLabel.stringValue = "Ready to add."
        }
    }

    /// Takes the selected row out of review and releases whatever it held. This
    /// is the only way a refused item, or a folder the person will not review,
    /// leaves the list.
    @objc private func removeSelectedAction() {
        let row = table.selectedRow
        guard row >= 0, let removed = draft.remove(at: row) else { return }
        // Whatever the row held goes with it — a file grant, owned bytes, or a
        // native folder row's provisional root grant — together with its
        // durable record.
        if let owned = removed.ownedCandidate {
            submitter.discardUnsubmitted([owned])
        }
        if draft.items.isEmpty {
            statusLabel.stringValue = "Choose files to begin."
            promoteWaitingSelection()
        } else {
            describeDraft()
        }
        refresh()
    }

    @objc private func submitAction() { beginSubmit() }

    /// Starts publishing the reviewed draft. The returned task ends once the
    /// selection is queued or has failed; it is internal so lifecycle tests can
    /// await it. Add is disabled before the task starts, so a second click
    /// cannot start a second publication.
    @discardableResult
    func beginSubmit() -> Task<Void, Never>? {
        guard draft.canSubmitFiles, !fileGroupInFlight else { return nil }
        fileGroupInFlight = true
        draft.note = note.string
        let candidates = draft.fileCandidates(keepUpdated: keepUpdated.state == .on)
        let title = candidates.count == 1 ? candidates[0].displayName : "\(candidates.count) files"
        statusLabel.stringValue = "\(title): Preparing current file versions…"
        refresh()
        return Task {
            defer {
                fileGroupInFlight = false
                refresh()
            }
            do {
                // Publication releases the candidates' durable records itself;
                // the grants they name now belong to the queued captures.
                let entries = try await submitter.submit(candidates)
                // Only the rows just published leave review. A folder row, or a
                // file added while this was in flight, is not part of this group.
                draft.removeSubmitted(candidates)
                note.string = ""
                keepUpdated.state = .off
                record(Self.fileReceipt(
                    title: title,
                    submissionID: entries.first?.intent.submissionId,
                    outcome: .queued(queued: entries.count),
                    detail: entries.count == 1 ? "Queued on this Mac." : "\(entries.count) items queued on this Mac."
                ))
                promoteWaitingSelection()
                refresh()
                onQueued?()
            } catch {
                var detail = "Couldn’t queue this selection. Check access and try again."
                switch error as? ManualCaptureSubmissionError {
                case .duplicateAutomaticSource?:
                    detail = "One of these files is already kept updated for this destination."
                case .automaticSourceLimitExceeded?:
                    detail = "This Mac already has the maximum number of individually updated files."
                case .snapshotCannotBeMonitored?:
                    keepUpdated.state = .off
                    detail = "Captured copies cannot be kept updated. Add them as snapshots instead."
                default: break
                }
                record(Self.fileReceipt(title: title, submissionID: nil, outcome: .failed, detail: detail))
            }
        }
    }

    private static func fileReceipt(
        title: String,
        submissionID: String?,
        outcome: CaptureGroupReceipt.Outcome,
        detail: String
    ) -> CaptureGroupReceipt {
        CaptureGroupReceipt(
            id: CaptureGroupReceipt.newID(),
            kind: .ordinaryFiles,
            title: title,
            submissionID: submissionID,
            outcome: outcome,
            detail: detail
        )
    }

    /// Closing the window keeps the draft, so the draft — not the window — owns
    /// its candidates until they are published or discarded.
    @objc private func closeAction() { window?.close() }

    /// The next waiting selection becomes the draft. One the draft refuses is
    /// released rather than dropped, and the one after it gets its turn.
    private func promoteWaitingSelection() {
        while draft.items.isEmpty, !waitingSelections.isEmpty {
            let next = waitingSelections.removeFirst()
            do {
                try draft.add(next)
                statusLabel.stringValue = "Review the next incoming selection."
            } catch {
                submitter.discardUnsubmitted(next)
            }
        }
    }

    /// Releases every candidate still in review or waiting — their file grants,
    /// folder root grants, owned bytes and durable records — without publishing
    /// any. This is the discard and quit path; nothing is added to Core.
    public func discardUnsubmittedCandidates() {
        submitter.discardUnsubmitted(draft.ownedCandidates + waitingSelections.flatMap { $0 })
        draft = CaptureDraft()
        waitingSelections.removeAll()
        refresh()
    }

    /// The native candidates currently in review or waiting, by reference.
    var liveNativeCandidateRefs: Set<String> {
        draft.nativeCandidateRefs
            .union(waitingSelections.flatMap { $0 }.compactMap { $0.native?.candidateRef })
    }

    var reviewItemCount: Int { draft.items.count }
    var reviewItemNames: [String] { draft.items.map(\.displayName) }
    var waitingSelectionCount: Int { waitingSelections.count }
    /// False while review holds a captured copy: a copy has no original to
    /// follow, so it can never be kept updated.
    var reviewAllowsKeepUpdated: Bool { !draft.containsSnapshots }

    @objc private func reviewSelectedFolder() {
        let row = table.selectedRow
        guard row >= 0,
              row < draft.items.count,
              draft.items[row].kind == .folder
        else { return }
        reviewFolder(draft.items[row].url, removeFromDraft: true)
    }

    private func refresh() {
        table.reloadData()
        keepUpdated.isEnabled = !draft.containsSnapshots
        if draft.containsSnapshots { keepUpdated.state = .off }
        primaryButton.isEnabled = draft.canSubmitFiles && !fileGroupInFlight
        if draft.items.isEmpty && statusLabel.stringValue.isEmpty {
            statusLabel.stringValue = "Choose files to begin."
        }
    }

    public func numberOfRows(in tableView: NSTableView) -> Int { draft.items.count }

    public func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let item = draft.items[row]
        let cell = NSTableCellView()
        let icon = NSImageView(image: NSWorkspace.shared.icon(forFile: item.url.path))
        icon.translatesAutoresizingMaskIntoConstraints = false
        let label = NSTextField(labelWithString: item.kind == .unsupported
            ? "\(item.displayName) — not a supported document"
            : item.displayName)
        label.translatesAutoresizingMaskIntoConstraints = false
        label.lineBreakMode = .byTruncatingMiddle
        label.toolTip = item.displayName
        if item.kind == .unsupported { label.textColor = .secondaryLabelColor }
        cell.addSubview(icon)
        cell.addSubview(label)
        NSLayoutConstraint.activate([
            icon.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 6),
            icon.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            icon.widthAnchor.constraint(equalToConstant: 22),
            icon.heightAnchor.constraint(equalToConstant: 22),
            label.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 8),
            label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -6),
            label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        cell.imageView = icon
        cell.textField = label
        return cell
    }
}
