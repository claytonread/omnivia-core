import AppKit
import CoreCaptureMacOS
import CoreSettingsMacOS
import DocumentIntakeMacOS
import Darwin
import Foundation

/// Collects one drag's promised files into a directory this process owns and
/// reports them once, after every promise has delivered or failed. AppKit
/// cannot make a drag source deliver, so a provider that goes quiet is bounded
/// by `promisedDeliveryTimeout`: the drag is then abandoned whole, the
/// directory is removed, and every late callback is ignored. Nothing is
/// retried, and the directory is removed on every path out.
@MainActor
private final class PromisedFileDelivery {
    private var collector: PromisedFileCollector
    private let destination: URL
    private let complete: ([URL]) -> Void
    private var timeout: Timer?

    init(
        expectedPerReceiver: [Int],
        destination: URL,
        complete: @escaping ([URL]) -> Void
    ) {
        collector = PromisedFileCollector(expectedPerReceiver: expectedPerReceiver)
        self.destination = destination
        self.complete = complete
        // The timer holds this collector so the bound survives a provider that
        // drops its callbacks; firing once releases it again.
        let timer = Timer(
            timeInterval: StatusItemDragPolicy.promisedDeliveryTimeout,
            repeats: false
        ) { _ in
            Task { @MainActor in
                guard self.collector.abandon() else { return }
                self.finish(nil)
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        timeout = timer
    }

    func receive(receiver: Int, url: URL?) {
        guard let delivered = collector.receive(receiver: receiver, url: url) else { return }
        finish(delivered)
    }

    /// Reviews the delivered copies, if they are the facts the bounds require,
    /// and removes the temporary directory either way.
    private func finish(_ delivered: [URL]?) {
        timeout?.invalidate()
        timeout = nil
        if let delivered,
           let accepted = StatusItemDragPolicy.acceptedPromisedFiles(
               delivered,
               destination: destination
           )
        {
            complete(accepted)
        }
        try? FileManager.default.removeItem(at: destination)
    }
}

@MainActor
final class StatusMenuAppDelegate: NSObject, NSApplicationDelegate {
    private let configuration: CompanionConfiguration
    private let singleton: CompanionSingleton
    private let coordinator: LifecycleCoordinator
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private let titleItem = NSMenuItem(title: "OmniVia Core — Checking…", action: nil, keyEquivalent: "")
    private let detailItem = NSMenuItem(title: "Contacting the Core Service", action: nil, keyEquivalent: "")
    private let refreshItem = NSMenuItem(title: "Refresh", action: #selector(refresh), keyEquivalent: "r")
    private let startItem = NSMenuItem(title: "Start Service", action: #selector(startService), keyEquivalent: "")
    private let stopItem = NSMenuItem(title: "Stop Service", action: #selector(stopService), keyEquivalent: "")
    private let logItem = NSMenuItem(title: "Show Service Log", action: #selector(showServiceLog), keyEquivalent: "l")
    private let addItem = NSMenuItem(title: "Add to Core…", action: #selector(openCapture), keyEquivalent: "a")
    private let filesItem = NSMenuItem(title: "Files and folders…", action: #selector(openFilesAndFolders), keyEquivalent: "")
    private let settingsItem = NSMenuItem(title: "Core Settings…", action: #selector(openSettings), keyEquivalent: ",")
    private var pollTimer: Timer?
    private var monitoringTimer: Timer?
    private var handoffTimer: Timer?
    /// Ticks the durable reconciliation schedule. It is deliberately much shorter than the
    /// shortest interval that schedule owns: a 15-minute metadata interval plus its jitter must
    /// come due on its own instant, not on an app timer's.
    private var reconciliationTimer: Timer?
    private var workspaceObservers: [NSObjectProtocol] = []
    /// File roots only: they have no durable ledger. Folder roots go through the bridge.
    private var folderEventObserver: FolderEventObserver?
    private var folderEventBridge: FolderEventBridge?
    /// Set when the observation ledger refused a write and its observer was stopped. Periodic
    /// reconciliation still covers the folder; nothing else surfaces this yet.
    private var folderObservationAttention: [FolderObservationAttention] = []
    /// Arms the periodic schedule from this process's lifecycle. Nothing else surfaces its
    /// attention values yet; they are kept rather than dropped.
    private var folderReconciliationDriver: FolderReconciliationDriver?
    private var folderReconciliationAttention: [FolderReconciliationAttention] = []
    private var folderEventObserverFingerprint = ""
    private var folderEventDebounceTimer: Timer?
    private var firstFolderEventAt: Date?
    private var dirtyFolderSubscriptions = Set<String>()
    private var dirtyFileSubscriptions = Set<String>()
    /// One owned Settings window (MR-001). Created on first open; closing it
    /// never touches Core lifecycle.
    private var settingsWindowController: SettingsWindowController?
    private var capturePanelController: CapturePanelController?
    private var folderManagementWindowController: FolderManagementWindowController?
    private var captureDeliveryCoordinator: CaptureDeliveryCoordinator?
    /// Tells Core about the sources the person disconnected (design §13.3 steps 4 and 5). It
    /// exists whenever delivery does, and every delivery pass, including the first after a
    /// restart, runs it.
    private var originalDisconnectReconciler: OriginalDisconnectReconciler?
    /// Kept so startup can finish any folder establishment an earlier process
    /// left pending, before monitoring reads a watched scope.
    private var intakeEnvironment: DocumentIntakeEnvironment?
    private var folderMonitoringCoordinator: FolderMonitoringCoordinator?
    private var folderBulkRunCoordinator: FolderBulkRunCoordinator?
    private var fileMonitoringCoordinator: FileMonitoringCoordinator?
    private var statusDropView: StatusItemDropView?
    /// Owns the captured copies a drop makes from text, a link or a promised
    /// file, so those copies are released with every other unsubmitted one.
    private var snapshotStore: OwnedSnapshotStore?
    private var nativeHandoffReceiver: NativeHandoffReceiver?
    private var nativeHandoffPeerServer: NativeHandoffPeerServer?
    /// Adopts durable Share requests that were queued while this companion was
    /// closed. Separate from the grant-bearing handoff above: it carries only
    /// captured copies and has no transfer window.
    private var shareInboxAdopter: ShareInboxAdopter?
    /// The most recent poll, so a window opened between polls starts with the
    /// state already known rather than waiting out the poll interval.
    private var lastSnapshot: MenuSnapshot?
    /// When Core was last polled as running. Held here, not in the settings
    /// window, because this companion runs continuously while that window
    /// spends most of its life closed.
    private var lastSeenRunning: Date?
    /// The same resolved `omnivia` the lifecycle runner uses. Held so the
    /// Settings window can administer installed MCP hosts through it; nil when
    /// the CLI could not be found, which the Access pane then states.
    private let mcp: McpAdministering?
    /// The packaged `omnivia-core-service`, resolved through the same path as
    /// the CLI. It owns this installation's managed OCR resource cache, which
    /// the Document processing pane administers; nil when it is not installed
    /// beside the companion, which that pane then states.
    private let ocr: OcrResourceAdministering?
    /// The same resolved `omnivia` again, for the one authority over this
    /// workspace's document-processing preferences. Nil when the CLI could not
    /// be found, which the Document processing pane then states.
    private let documentProcessing: DocumentProcessingAdministering?

    init(configuration: CompanionConfiguration, singleton: CompanionSingleton) {
        self.configuration = configuration
        self.singleton = singleton
        let runner: LifecycleRunning
        let resolvedExecutable: URL?
        var mcp: McpAdministering?
        do {
            let executable = try CLIResolver().resolve(override: configuration.cliOverride)
            resolvedExecutable = executable
            runner = CLICommandRunner(
                executable: executable,
                installationState: configuration.installationState,
                workspaceID: configuration.workspaceID
            )
            // `mcp` commands take no workspace id — a host's setup names its
            // own workspace — so this needs only the installation state.
            mcp = McpCommandRunner(
                executable: executable,
                installationState: configuration.installationState
            )
        } catch {
            resolvedExecutable = nil
            runner = UnavailableLifecycleRunner()
        }
        self.mcp = mcp
        // The governed application surface, not an administration mode: the
        // preferences are workspace state, so this one carries the workspace.
        self.documentProcessing = resolvedExecutable.flatMap {
            DocumentProcessingCommandRunner(
                executable: $0,
                installationState: configuration.installationState,
                workspaceRef: configuration.workspaceID
            )
        }
        // Resolved by name, not from `--cli`: that override names the CLI, and
        // the managed resource cache is administered by the service executable.
        // Absent, the pane says it has no route rather than guessing a path.
        self.ocr = (try? CLIResolver().resolve(
            override: nil, executableName: "omnivia-core-service"
        )).flatMap {
            OcrResourceCommandRunner(
                executable: $0, installationState: configuration.installationState
            )
        }
        coordinator = LifecycleCoordinator(runner: runner)
        super.init()
        let intakeRoot = configuration.installationState.deletingLastPathComponent()
            .appendingPathComponent("native-intake", isDirectory: true)
        if let environment = try? DocumentIntakeEnvironment(stateRoot: intakeRoot),
           let snapshotStore = try? OwnedSnapshotStore(
               root: intakeRoot.appendingPathComponent("owned-snapshots", isDirectory: true)
           )
        {
            // Adoption records name this companion's own grants, so they stay
            // beside the protected handoff area, outside the App Group.
            let intakeCandidates = try? NativeIntakeCandidateStore(
                root: intakeRoot.appendingPathComponent("native-intake-candidates", isDirectory: true)
            )
            // An owned copy that backs a live adoption record is bounded by its
            // request and review draft, never by age. If the records cannot be
            // listed nothing can be proven unused, so nothing is pruned.
            if let live = try? intakeCandidates?.nativeRequestIDs() {
                _ = snapshotStore.removeExpired(keeping: Set(live))
            }
            self.snapshotStore = snapshotStore
            intakeEnvironment = environment
            folderMonitoringCoordinator = environment.folderMonitoring
            folderBulkRunCoordinator = environment.folderBulkCoordinator
            fileMonitoringCoordinator = environment.fileMonitoring
            folderReconciliationDriver = FolderReconciliationDriver(
                subscriptions: environment.folderSubscriptions,
                scheduler: environment.folderReconciliation,
                onAttention: { [weak self] value in
                    Task { @MainActor [weak self] in
                        self?.folderReconciliationAttention.append(value)
                    }
                }
            )
            folderManagementWindowController = FolderManagementWindowController(
                // The configured workspace, stated rather than inferred from a row.
                workspaceName: configuration.workspaceID,
                folderStore: environment.folderSubscriptions,
                folderMonitoring: environment.folderMonitoring,
                fileStore: environment.fileSubscriptions,
                fileMonitoring: environment.fileMonitoring,
                bulkStore: environment.folderBulkRuns,
                bulkCoordinator: environment.folderBulkCoordinator,
                activityStore: environment.captureActivity,
                observeActivity: { try environment.recordCaptureActivity() },
                rootGroups: { environment.rootAccessGroups() },
                documentRoutes: { environment.documentAccessRoutes() },
                // The same authenticated application-operation client `document capture`
                // is delivered through, for the two canonical document operations the
                // management window reads and authorises. Absent when no client could be
                // resolved, which the detail pane states.
                historyTransport: resolvedExecutable.map {
                    CLICoreDocumentHistoryTransport(
                        executable: $0,
                        installationState: configuration.installationState,
                        workspaceRef: configuration.workspaceID
                    )
                },
                locateOriginal: { [weak self] in
                    self?.capturePanelController?.openOriginalChooser()
                },
                // Resume and Check now read a watched source, so each asks the
                // delivery coordinator whether the signed-in identity is the one
                // the watch froze; without a coordinator the answer is no.
                identityAuthorises: { [weak self] frozen in
                    guard let delivery = self?.captureDeliveryCoordinator else { return false }
                    return await delivery.confirmsIdentity(of: frozen)
                },
                // The person's own "access or update is resolved". Releasing
                // only returns the held intents to pending; the delivery pass
                // that follows is the one that checks the target again.
                releaseHeld: { [weak self] pauses in
                    guard let delivery = self?.captureDeliveryCoordinator else { return }
                    for pause in pauses.sorted(by: { $0.rawValue < $1.rawValue }) {
                        _ = try? await delivery.releaseHeld(pause)
                    }
                    self?.deliverPendingCaptures()
                },
                // Source-level Disconnect: the revocation is local, durable and immediate; the
                // reconciliation that tells Core runs with the next delivery pass and again
                // after any restart for as long as Core has not acknowledged it.
                disconnectSource: { [weak self] file in
                    let revoked = (try? await environment.disconnectSource(
                        linkedDocumentID: file.linkedDocumentID,
                        fallback: .installedCompanion(
                            installationState: configuration.installationState,
                            workspaceRef: configuration.workspaceID
                        )
                    )) != nil
                    self?.deliverPendingCaptures()
                    return revoked
                }
            )
            let companionBinding = CaptureDeliveryBinding.installedCompanion(
                installationState: configuration.installationState,
                workspaceRef: configuration.workspaceID
            )
            let submitter = ManualCaptureSubmission(
                environment: environment,
                binding: companionBinding,
                snapshotStore: snapshotStore,
                candidates: intakeCandidates
            )
            let folderSubmitter = FolderCaptureSubmission(
                environment: environment,
                binding: companionBinding
            )
            if let resolvedExecutable {
                let delivery = CLICaptureDelivery(
                    executable: resolvedExecutable,
                    installationState: configuration.installationState,
                    deliveryBinding: companionBinding
                )
                originalDisconnectReconciler = environment.originalDisconnectReconciler(
                    transport: delivery
                )
                captureDeliveryCoordinator = CaptureDeliveryCoordinator(
                    queue: environment.queue,
                    transport: delivery,
                    // The watches a changed signed-in principal or target
                    // profile has to pause before anything reads a source
                    // again; see `pauseWorkUnderChangedIdentity`.
                    fileSubscriptions: environment.fileSubscriptions,
                    folderSubscriptions: environment.folderSubscriptions,
                    gate: environment.publicationGate
                )
            }
            capturePanelController = CapturePanelController(
                workspaceName: configuration.workspaceID,
                submitter: submitter,
                folderSubmitter: folderSubmitter,
                onQueued: { [weak self] in self?.deliverPendingCaptures() }
            )
            ShortcutIntakeRuntime.configure(
                snapshotStore: snapshotStore,
                openHandler: { [weak self] in self?.capturePanelController?.openOrFocus() },
                reviewHandler: { [weak self] urls in
                    self?.capturePanelController?.addIncomingSnapshots(urls, surface: .shortcut)
                }
            )
            if let inbox = try? ShareInboxStore.appGroupInbox() {
                shareInboxAdopter = ShareInboxAdopter(
                    inbox: inbox,
                    snapshotStore: snapshotStore,
                    openChooser: { [weak self] in
                        self?.capturePanelController?.openOriginalChooser()
                    },
                    accept: { [weak self] candidates in
                        self?.capturePanelController?.addIncomingAdopted(candidates) ?? false
                    }
                )
            }
            if let resources = try? NativeHandoffMailbox.appGroupMailbox(),
               let mailbox = try? NativeHandoffMailbox(
                   root: intakeRoot.appendingPathComponent(
                       "authenticated-native-handoff",
                       isDirectory: true
                   )
               ),
               let intakeCandidates
            {
                let receiver = NativeHandoffReceiver(
                    mailbox: mailbox,
                    resources: resources,
                    candidates: intakeCandidates,
                    submitter: submitter,
                    snapshotStore: snapshotStore,
                    accept: { [weak self] candidates in
                        self?.capturePanelController?.addIncomingAdopted(candidates)
                    }
                )
                nativeHandoffReceiver = receiver
                nativeHandoffPeerServer = try? NativeHandoffPeerServer(
                    rendezvous: resources,
                    receiver: receiver,
                    companionBundleURL: Bundle.main.bundleURL
                )
            }
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        configureMenu()
        // The Finder Service is advertised only once this companion can
        // actually open a review draft. With no review there is nothing for the
        // Service to hand a selection to, so the menu item never appears.
        if isReadyForReview {
            NSApp.servicesProvider = self
            NSUpdateDynamicServices()
        }
        coordinator.onChange = { [weak self] snapshot in
            self?.render(snapshot)
        }
        singleton.startListening { [weak self] in
            self?.coordinator.refresh()
        }
        render(.checking)
        coordinator.refresh()
        deliverPendingCaptures()
        reconcileWatchedFolders()
        // Arms `.startup` and surfaces whatever the persisted schedule already owed. Scheduling
        // reads no watched scope, so it does not wait on pending establishment above; running the
        // demands does, and the executor's own establishment gate is what holds them back.
        runReconciliation(folderReconciliationDriver?.startup())
        nativeHandoffReceiver?.reconcile()
        shareInboxAdopter?.reconcile()

        let timer = Timer(timeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.coordinator.refresh()
                self?.deliverPendingCaptures()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        pollTimer = timer
        let monitoringTimer = Timer(timeInterval: 15 * 60, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in self?.reconcileWatchedFolders() }
        }
        RunLoop.main.add(monitoringTimer, forMode: .common)
        self.monitoringTimer = monitoringTimer
        let handoffTimer = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.nativeHandoffReceiver?.reconcile()
                self?.shareInboxAdopter?.reconcile()
            }
        }
        RunLoop.main.add(handoffTimer, forMode: .common)
        self.handoffTimer = handoffTimer
        let reconciliationTimer = Timer(
            timeInterval: FolderReconciliationDriver.tickInterval,
            repeats: true
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.runReconciliation(self?.folderReconciliationDriver?.tick())
            }
        }
        RunLoop.main.add(reconciliationTimer, forMode: .common)
        self.reconciliationTimer = reconciliationTimer
        // Sleep stops the clock for the schedule and stops FSEvents for the stream, and a remount
        // replaces a root this process cannot otherwise notice. Both are immediate triggers; the
        // handlers do nothing but hand them to the driver and its demands to the executor.
        let workspace = NSWorkspace.shared.notificationCenter
        workspaceObservers = [
            workspace.addObserver(
                forName: NSWorkspace.didWakeNotification,
                object: nil,
                queue: .main
            ) { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.runReconciliation(self?.folderReconciliationDriver?.wake())
                }
            },
            workspace.addObserver(
                forName: NSWorkspace.didMountNotification,
                object: nil,
                queue: .main
            ) { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.runReconciliation(self?.folderReconciliationDriver?.remount())
                }
            },
        ]
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        capturePanelController?.addIncoming(urls, surface: .menuDrop)
    }

    /// True once a review draft can be opened. Every native intake surface is
    /// gated on it: an unready companion registers no Service and installs no
    /// drop target rather than taking a selection it cannot show.
    private var isReadyForReview: Bool { capturePanelController != nil }

    /// The Finder Service. Files and folders come from the pasteboard the
    /// Service was handed and from nowhere else: this reads no Finder selection
    /// and no general clipboard, and it opens a review draft rather than
    /// queueing anything.
    @objc func addToOmniViaCore(
        _ pasteboard: NSPasteboard,
        userData: String?,
        error: AutoreleasingUnsafeMutablePointer<NSString?>
    ) {
        let urls = pasteboard.readObjects(
            forClasses: [NSURL.self],
            options: [.urlReadingFileURLsOnly: true]
        ) as? [URL] ?? []
        if let refusal = CaptureIntakePolicy.admit(
            itemCount: urls.count,
            isReady: isReadyForReview
        ) {
            error.pointee = refusal.message as NSString
            return
        }
        capturePanelController?.addIncoming(urls, surface: .finderService)
    }

    func applicationWillTerminate(_ notification: Notification) {
        // The status menu is a companion, not the service owner. Quitting it
        // deliberately performs no Core lifecycle command.
        pollTimer?.invalidate()
        monitoringTimer?.invalidate()
        handoffTimer?.invalidate()
        reconciliationTimer?.invalidate()
        folderEventDebounceTimer?.invalidate()
        let workspace = NSWorkspace.shared.notificationCenter
        for observer in workspaceObservers { workspace.removeObserver(observer) }
        workspaceObservers = []
        folderEventObserver = nil
        folderEventBridge = nil
        // Review lives in this process, so quitting ends every candidate's
        // owner. The panel releases what it holds; the receiver then stops
        // taking requests and releases whatever never reached review.
        capturePanelController?.discardUnsubmittedCandidates()
        // A durable Share request that has not been adopted stays durable: it
        // is better left in the spool than owned by a process that is ending.
        shareInboxAdopter?.companionWillTerminate()
        nativeHandoffReceiver?.companionWillTerminate()
        coordinator.companionWillTerminate()
        singleton.close()
    }

    private func configureMenu() {
        menu.autoenablesItems = false
        titleItem.isEnabled = false
        detailItem.isEnabled = false
        for item in [refreshItem, startItem, stopItem, logItem, addItem, filesItem, settingsItem] {
            item.target = self
        }

        menu.addItem(titleItem)
        menu.addItem(detailItem)
        menu.addItem(.separator())
        menu.addItem(addItem)
        menu.addItem(filesItem)
        menu.addItem(settingsItem)
        menu.addItem(.separator())
        menu.addItem(refreshItem)
        menu.addItem(startItem)
        menu.addItem(stopItem)
        menu.addItem(logItem)
        menu.addItem(.separator())
        let quit = NSMenuItem(
            title: "Quit Status Menu",
            action: #selector(quitStatusMenu),
            keyEquivalent: "q"
        )
        quit.target = self
        menu.addItem(quit)
        statusItem.menu = menu
        addItem.isEnabled = capturePanelController != nil
        filesItem.isEnabled = folderManagementWindowController != nil
        if isReadyForReview, let button = statusItem.button {
            let dropView = StatusItemDropView { [weak self] contents in
                self?.receiveStatusItemDrop(contents)
            }
            button.addSubview(dropView)
            NSLayoutConstraint.activate([
                dropView.leadingAnchor.constraint(equalTo: button.leadingAnchor),
                dropView.trailingAnchor.constraint(equalTo: button.trailingAnchor),
                dropView.topAnchor.constraint(equalTo: button.topAnchor),
                dropView.bottomAnchor.constraint(equalTo: button.bottomAnchor),
            ])
            statusDropView = dropView
        }
    }

    /// Routes one accepted status-item drop to review. Originals are reviewed
    /// where they are; a promised file, a dropped link and dropped text have no
    /// original to keep, so each becomes a captured copy this companion owns
    /// and then goes to the same review draft. Nothing is queued here.
    private func receiveStatusItemDrop(_ contents: StatusItemDropContents) {
        guard let capturePanelController else { return }
        if !contents.fileURLs.isEmpty {
            capturePanelController.addIncoming(contents.fileURLs, surface: .menuDrop)
            return
        }
        if !contents.promises.isEmpty {
            receivePromisedFiles(contents.promises)
            return
        }
        // A dropped link is kept as the text it is. The companion resolves no
        // link and fetches nothing from the network.
        let leaf = contents.textIsLink ? "Dropped Link" : "Dropped Text"
        reviewCapturedCopies(contents.texts.enumerated().map { index, text in
            .data(Data(text.utf8), suggestedFilename: "\(leaf) \(index + 1).txt")
        })
    }

    /// Receives promised files into a directory this process owns, copies them
    /// into the owned-copy store under their bounds, and sends the copies to
    /// review. The promise is the source's only delivery, so the temporary
    /// directory goes away as soon as the copies exist — or as soon as the
    /// delivery times out or is refused.
    private func receivePromisedFiles(_ receivers: [NSFilePromiseReceiver]) {
        let expectedPerReceiver = receivers.map(\.fileNames.count)
        let expected = expectedPerReceiver.reduce(0, +)
        guard expected > 0, expected <= StatusItemDragPolicy.maximumPromisedFiles else { return }
        let destination = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString.lowercased(), isDirectory: true)
        guard (try? FileManager.default.createDirectory(
            at: destination,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )) != nil else { return }

        let delivery = PromisedFileDelivery(
            expectedPerReceiver: expectedPerReceiver,
            destination: destination
        ) { [weak self] urls in
            self?.reviewCapturedCopies(urls.map { .file($0) })
        }
        for (index, receiver) in receivers.enumerated() {
            receiver.receivePromisedFiles(
                atDestination: destination,
                options: [:],
                operationQueue: .main
            ) { url, error in
                // `operationQueue: .main` delivers on the main thread, which is
                // this actor.
                MainActor.assumeIsolated {
                    delivery.receive(receiver: index, url: error == nil ? url : nil)
                }
            }
        }
    }

    /// Stages owned copies and opens them for review as captured copies, which
    /// are never linked back to the temporary bytes they came from.
    private func reviewCapturedCopies(_ inputs: [OwnedSnapshotInput]) {
        guard !inputs.isEmpty,
              let snapshotStore,
              let capturePanelController,
              let urls = try? snapshotStore.stage(inputs)
        else { return }
        capturePanelController.addIncomingSnapshots(urls, surface: .menuDrop)
    }

    private func render(_ snapshot: MenuSnapshot) {
        lastSnapshot = snapshot
        if case .status(let status) = snapshot.condition, status.lifecycleState == .running,
           status.connectionState != .unreachable {
            lastSeenRunning = Date()
        }
        let projection = MenuProjection.project(snapshot)
        titleItem.title = projection.title
        detailItem.title = projection.detail
        refreshItem.isEnabled = projection.refreshEnabled
        startItem.isEnabled = projection.startEnabled
        stopItem.isEnabled = projection.stopEnabled

        guard let button = statusItem.button else { return }
        if let image = NSImage(
            systemSymbolName: projection.symbolName,
            accessibilityDescription: projection.title
        ) {
            image.isTemplate = true
            button.image = image
            button.title = ""
        } else {
            button.image = nil
            button.title = "O"
        }
        button.toolTip = "\(projection.title) — \(projection.detail)"
        reportServiceState(snapshot)
    }

    /// Forward the state this companion already polls to the settings window's
    /// sidebar footer. The window invokes no lifecycle command itself (MR-005),
    /// so the state is pushed here rather than fetched there.
    private func reportServiceState(_ snapshot: MenuSnapshot) {
        guard let controller = settingsWindowController else { return }
        switch snapshot.condition {
        case .checking:
            // The first poll, before any status: leave the footer as it was
            // rather than flicker a state we do not have yet.
            break
        case .unavailable:
            // The adapter itself could not be run, so Core's state is unknown
            // — which is not the same as Core being unreachable or stopped.
            controller.updateServiceState(.unknown)
        case .status(let status):
            let state: CoreServiceState
            if status.connectionState == .unreachable {
                state = .unreachable(lastSeen: lastSeenRunning)
            } else {
                switch status.lifecycleState {
                case .running: state = .running
                case .starting: state = .starting
                case .stopping: state = .stopping
                case .stopped: state = .stopped
                case .failed: state = .failed
                case .unknown: state = .unknown
                }
            }
            controller.updateServiceState(state, host: status.target.displayName)
        }
    }

    @objc private func refresh() {
        coordinator.refresh()
    }

    @objc private func openSettings() {
        if settingsWindowController == nil {
            let context = ReadinessContext(
                installationRevision: configuration.installationState.path,
                localSessionRef: UUID().uuidString,
                targetRef: "local",
                workspaceRef: configuration.workspaceID,
                configurationRevision: "1"
            )
            settingsWindowController = SettingsWindowController(
                coordinator: ReadinessCoordinator(context: context),
                mcp: mcp,
                ocr: ocr,
                documentProcessing: documentProcessing
            )
        }
        if let lastSnapshot { reportServiceState(lastSnapshot) }
        settingsWindowController?.openOrFocus()
    }

    @objc private func openCapture() {
        capturePanelController?.openOrFocus()
    }

    @objc private func openFilesAndFolders() {
        folderManagementWindowController?.openOrFocus()
    }

    private func deliverPendingCaptures() {
        guard let captureDeliveryCoordinator else { return }
        let monitoring = folderMonitoringCoordinator
        let fileMonitoring = fileMonitoringCoordinator
        let bulkRuns = folderBulkRunCoordinator
        let environment = intakeEnvironment
        let disconnects = originalDisconnectReconciler
        // Record before anything is staged, so a published item is on the Recent
        // additions surface while it is still only queued on this Mac, and again
        // after, so its outcome is durable whether or not the window is open.
        // Delivery never depends on this: a failure to record leaves the durable
        // owners untouched and claims nothing, and the window reports its own
        // activity as unavailable when it cannot record either.
        _ = try? environment?.recordCaptureActivity()
        Task {
            _ = await captureDeliveryCoordinator.deliverPending()
            _ = await disconnects?.reconcilePending()
            _ = await bulkRuns?.reconcileReceipts()
            _ = await bulkRuns?.advanceAll()
            _ = await monitoring?.reconcileReceipts()
            _ = await fileMonitoring?.reconcileReceipts()
            _ = try? environment?.recordCaptureActivity()
            await self.refreshFolderEventObservation()
        }
    }

    /// Hands the demands a lifecycle entry point just armed to the one executor, off the main
    /// actor. That actor is the whole of the no-overlap guarantee: a second batch waits for the
    /// first instead of running beside it, and a demand the first batch already reported is no
    /// longer owed by the time the second reaches it.
    private func runReconciliation(_ demands: [FolderReconciliationScheduler.Demand]?) {
        guard let demands, !demands.isEmpty,
              let executor = intakeEnvironment?.folderReconciliationExecutor
        else { return }
        let delivery = captureDeliveryCoordinator
        Task.detached {
            // Startup, the periodic tick, wake and remount all arrive here. A
            // demand armed before the identity changed is not permission to
            // read: pausing first leaves the executor's own `active` check to
            // skip it, rather than reading the scope under the new account. A
            // pause that could not be persisted leaves the watch active, so this
            // pass runs nothing and the work stays owed.
            guard (try? await delivery?.pauseWorkUnderChangedIdentity()) != nil else { return }
            await executor.run(demands)
        }
    }

    private func reconcileWatchedFolders() {
        guard let folderMonitoringCoordinator else { return }
        let delivery = captureDeliveryCoordinator
        let fileMonitoring = fileMonitoringCoordinator
        let bulkRuns = folderBulkRunCoordinator
        let environment = intakeEnvironment
        Task {
            // A signed-in principal or target profile that changed while this
            // Mac was not running pauses its watches here, before the first
            // scan of this process arms anything: no automatic read continues
            // under an identity the frozen consent was not given for, and a
            // pause that could not be persisted reads nothing at all.
            guard (try? await delivery?.pauseWorkUnderChangedIdentity()) != nil else { return }
            // An establishment an earlier process left pending holds a root
            // grant nothing can discover through yet. Finish it before anything
            // reads a watched scope.
            _ = await environment?.reconcilePendingFolderEstablishment()
            _ = await bulkRuns?.reconcileReceipts()
            _ = await bulkRuns?.advanceAll()
            _ = await folderMonitoringCoordinator.reconcileAll()
            _ = await fileMonitoring?.reconcileAll()
            _ = await delivery?.deliverPending()
            _ = await folderMonitoringCoordinator.reconcileReceipts()
            _ = await fileMonitoring?.reconcileReceipts()
            _ = try? environment?.recordCaptureActivity()
            await self.refreshFolderEventObservation()
        }
    }

    private func refreshFolderEventObservation() async {
        guard let folderMonitoringCoordinator, let environment = intakeEnvironment else { return }
        let folderRoots = await folderMonitoringCoordinator.activeObservationRoots()
        let fileRoots = await fileMonitoringCoordinator?.activeObservationRoots().map {
            FolderObservationRoot(subscriptionID: "file::\($0.subscriptionID)", url: $0.url)
        } ?? []
        let fingerprint = (
            folderRoots.map {
                let generation = environment.folderSubscriptions.subscription(id: $0.subscriptionID)?.configGeneration ?? 0
                return "folder::\($0.subscriptionID)\u{0}\(generation)\u{0}\($0.url.standardizedFileURL.path)"
            } + fileRoots.map { "\($0.subscriptionID)\u{0}\($0.url.standardizedFileURL.path)" }
        ).sorted().joined(separator: "\u{1}")
        guard fingerprint != folderEventObserverFingerprint else { return }
        folderEventObserver = nil
        folderEventBridge = nil
        folderEventObserverFingerprint = fingerprint
        folderEventObserver = FolderEventObserver(roots: fileRoots) { [weak self] signal in
            Task { @MainActor [weak self] in self?.receiveFolderEvent(signal) }
        }
        folderEventBridge = FolderEventBridge(
            roots: folderRoots,
            ledger: environment.folderObservation,
            subscriptions: environment.folderSubscriptions,
            reconciliation: environment.folderReconciliation,
            onAttention: { [weak self] value in
                Task { @MainActor [weak self] in self?.folderObservationAttention.append(value) }
            }
        ) { [weak self] signal in
            // The bridge has already persisted what produced this signal and the
            // reconciliation schedule has already heard it unprefixed.
            let prefixed = FolderEventSignal(
                subscriptionIDs: Set(signal.subscriptionIDs.map { "folder::\($0)" }),
                requiresFullReconciliation: signal.requiresFullReconciliation
            )
            Task { @MainActor [weak self] in self?.receiveFolderEvent(prefixed) }
        }
    }

    private func receiveFolderEvent(_ signal: FolderEventSignal) {
        guard !signal.subscriptionIDs.isEmpty,
              let folderMonitoringCoordinator
        else { return }
        let folderIDs = Set(signal.subscriptionIDs.compactMap { value in
            value.hasPrefix("folder::") ? String(value.dropFirst("folder::".count)) : nil
        })
        let fileIDs = Set(signal.subscriptionIDs.compactMap { value in
            value.hasPrefix("file::") ? String(value.dropFirst("file::".count)) : nil
        })
        dirtyFolderSubscriptions.formUnion(folderIDs)
        dirtyFileSubscriptions.formUnion(fileIDs)
        if firstFolderEventAt == nil { firstFolderEventAt = Date() }
        let fileMonitoring = fileMonitoringCoordinator
        Task {
            await folderMonitoringCoordinator.markDirty(subscriptionIDs: folderIDs)
            await fileMonitoring?.markDirty(subscriptionIDs: fileIDs)
        }
        folderEventDebounceTimer?.invalidate()
        let elapsed = Date().timeIntervalSince(firstFolderEventAt ?? Date())
        let interval = signal.requiresFullReconciliation ? 0.1 : min(3, max(0.1, 30 - elapsed))
        let timer = Timer(timeInterval: interval, repeats: false) { [weak self] _ in
            Task { @MainActor [weak self] in self?.reconcileDirtyFolders() }
        }
        RunLoop.main.add(timer, forMode: .common)
        folderEventDebounceTimer = timer
    }

    private func reconcileDirtyFolders() {
        guard let folderMonitoringCoordinator else { return }
        let subscriptions = dirtyFolderSubscriptions
        let fileSubscriptions = dirtyFileSubscriptions
        dirtyFolderSubscriptions.removeAll()
        dirtyFileSubscriptions.removeAll()
        firstFolderEventAt = nil
        folderEventDebounceTimer = nil
        let delivery = captureDeliveryCoordinator
        let fileMonitoring = fileMonitoringCoordinator
        Task {
            // Filesystem events are not permission to read under a changed
            // identity, and this is the one pass a resumed watch can reach
            // without going through startup; see `pauseWorkUnderChangedIdentity`.
            guard (try? await delivery?.pauseWorkUnderChangedIdentity()) != nil else { return }
            _ = await folderMonitoringCoordinator.reconcile(subscriptionIDs: subscriptions)
            _ = await fileMonitoring?.reconcile(subscriptionIDs: fileSubscriptions)
            _ = await delivery?.deliverPending()
            _ = await folderMonitoringCoordinator.reconcileReceipts()
            _ = await fileMonitoring?.reconcileReceipts()
            await self.refreshFolderEventObservation()
        }
    }

    @objc private func startService() {
        coordinator.start()
    }

    @objc private func stopService() {
        coordinator.stop()
    }

    @objc private func showServiceLog() {
        let installationHome = configuration.installationState.deletingLastPathComponent()
        let runDirectory = installationHome.appendingPathComponent("run", isDirectory: true)
        let log = runDirectory.appendingPathComponent("service.log")
        var isDirectory: ObjCBool = false

        if FileManager.default.fileExists(atPath: log.path, isDirectory: &isDirectory) {
            NSWorkspace.shared.activateFileViewerSelecting([log])
            return
        }
        for directory in [runDirectory, installationHome] {
            if FileManager.default.fileExists(atPath: directory.path, isDirectory: &isDirectory),
               isDirectory.boolValue
            {
                NSWorkspace.shared.open(directory)
                return
            }
        }

        // A fixed phrase: the companion may reveal a log that already exists, but
        // it names no path of its own — an installation location is not a word
        // this process puts on screen.
        let alert = NSAlert()
        alert.messageText = "No Core service log yet"
        alert.informativeText = "Start Core once to create the service log."
        alert.alertStyle = .informational
        alert.runModal()
    }

    @objc private func quitStatusMenu() {
        NSApplication.shared.terminate(nil)
    }
}

do {
    // Top-level `main.swift` code runs on the process's start thread, which is
    // the main thread; `assumeIsolated` states that rather than hopping through
    // an async entry point just to satisfy the isolation checker.
    try MainActor.assumeIsolated {
        let configuration = try CompanionConfiguration.loadOrParse(arguments: CommandLine.arguments)
        let singleton: CompanionSingleton
        switch try CompanionSingleton.acquire(installationState: configuration.installationState) {
        case .forwarded:
            return
        case let .primary(acquired):
            singleton = acquired
        }
        let application = NSApplication.shared
        let delegate = StatusMenuAppDelegate(configuration: configuration, singleton: singleton)
        application.setActivationPolicy(.accessory)
        application.delegate = delegate
        application.run()
    }
} catch is CompanionConfigurationError {
    // One fixed usage line. Even the companion's own argument errors echo the
    // words they were given, and this process writes none of those back.
    FileHandle.standardError.write(
        Data("usage: omnivia-core-status-menu [--cli <absolute path>] --installation-state <absolute path> --workspace-id <id>\n".utf8)
    )
    Darwin.exit(2)
} catch {
    FileHandle.standardError.write(Data("OmniVia Core status menu is unavailable\n".utf8))
    Darwin.exit(1)
}
