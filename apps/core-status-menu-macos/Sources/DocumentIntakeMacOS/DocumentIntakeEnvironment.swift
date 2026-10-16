import Foundation

/// One in-process owner for the bookmark store and durable preparation queue.
/// File and folder intake must share this object so two stale in-memory bookmark
/// snapshots cannot overwrite one another.
public final class DocumentIntakeEnvironment: @unchecked Sendable {
    let accessStore: NativeAccessStore
    let preparation: CapturePreparationCoordinator
    /// The single shared publication gate every owner above was built with.
    public let publicationGate: PublicationGate
    let reader: any CoordinatedOriginalReading
    public let queue: CaptureIntentQueue
    public let folderSubscriptions: FolderSubscriptionStore
    public let folderMonitoring: FolderMonitoringCoordinator
    /// Durable event-stream cursor and dirty obligations. Fed by `FolderEventBridge` and
    /// acknowledged by `folderReconciliationExecutor` for the work it actually covered.
    public let folderObservation: FolderObservationLedger
    /// Durable periodic schedule and coverage truth for active folder subscriptions.
    public let folderReconciliation: FolderReconciliationScheduler
    /// Runs the demands the schedule arms, off the main actor.
    public let folderReconciliationExecutor: FolderReconciliationExecutor
    /// Durable automatic-update scheduling: which verified scopes are dirty, what one candidate is
    /// in flight, and where background rotation stands. Individual file watches now decide every
    /// automatic check through it — `fileMonitoring` signals it and reads nothing it has not
    /// claimed. Folder monitoring still goes straight to preparation.
    public let automaticScheduling: AutomaticSchedulingStateMachine
    public let folderBulkRuns: FolderBulkRunStore
    public let folderBulkCoordinator: FolderBulkRunCoordinator
    /// Durable local activity for Recent additions. It reads the queue and the
    /// run ledger and writes only its own file, so pruning or clearing activity
    /// never touches recovery data, a link, a watch or a frozen manifest.
    public let captureActivity: CaptureActivityStore
    public let fileSubscriptions: FileSubscriptionStore
    public let fileMonitoring: FileMonitoringCoordinator
    /// One logical source and one linked document per workspace per native
    /// file. Every route that enrols a file — a folder scan, a later folder
    /// discovery, a direct submission — takes its identities from here, so
    /// overlapping routes over one original share one source.
    public let canonicalSources: CanonicalSourceRegistry
    /// The canonical native/Core-owned stores every folder scope excludes, so a
    /// scan can never ingest the companion's own bookmarks, queue or receipts.
    /// Resolved here, once, from the real state root rather than matched by name.
    public let controlledStores: [URL]

    /// Process-monotonic milliseconds, zeroed at this call, which is the clock
    /// `AutomaticSchedulingStateMachine` measures its quiet period and maximum wait in. It is an
    /// elapsed duration rather than a wall-clock reading, so a time change cannot move it
    /// backwards; a test supplies its own instead.
    public static func processUptimeMilliseconds() -> @Sendable () -> Int64 {
        let base = ProcessInfo.processInfo.systemUptime
        return { Int64(max(0, (ProcessInfo.processInfo.systemUptime - base) * 1000)) }
    }

    public init(
        stateRoot: URL,
        bookmarkCodec: any SecurityScopedBookmarkCoding = SystemSecurityScopedBookmarkCodec(),
        reader: any CoordinatedOriginalReading = SystemCoordinatedOriginalReader(),
        monotonicClock: (@Sendable () -> Int64)? = nil,
        // Wall clock for the periodic folder schedule, whose intervals are 15 minutes and 24
        // hours, and for the monitoring scan's five-minute add/change burst window. A test moves
        // it by hand rather than waiting one out; production reads `Date`.
        reconciliationClock: (@Sendable () -> Date)? = nil,
        // Core's own statement about a document's current capture, which is what lets an
        // unchanged file be called checked. Absent, no unchanged file is.
        currentCapture: (any CurrentCaptureVerifying)? = nil,
        // How a file's qualified native identity is read. Production asks macOS for the
        // volume identity and file resource identifier; a test substitutes one to prove
        // what happens when macOS will not identify a file at all.
        nativeIdentity: (@Sendable (URL) -> String?)? = nil
    ) throws {
        self.reader = reader
        let clock = monotonicClock ?? Self.processUptimeMilliseconds()
        controlledStores = [stateRoot.resolvingSymlinksInPath().standardizedFileURL]
        // Exactly one gate for the whole process: access relink/disconnect/adopt,
        // subscription register/update, queue publication and delivery claims all
        // serialise against the final validation+commit. See `PublicationGate`.
        let gate = PublicationGate()
        publicationGate = gate
        accessStore = try NativeAccessStore(
            stateDirectory: stateRoot.appendingPathComponent("native-access", isDirectory: true),
            codec: bookmarkCodec,
            gate: gate
        )
        queue = try CaptureIntentQueue(
            root: stateRoot.appendingPathComponent("capture-queue", isDirectory: true),
            gate: gate
        )
        folderSubscriptions = try FolderSubscriptionStore(
            stateDirectory: stateRoot.appendingPathComponent("folder-subscriptions", isDirectory: true),
            gate: gate
        )
        folderObservation = try FolderObservationLedger(
            stateDirectory: stateRoot.appendingPathComponent("folder-observation", isDirectory: true),
            subscriptions: folderSubscriptions,
            gate: gate
        )
        folderReconciliation = try FolderReconciliationScheduler(
            stateDirectory: stateRoot.appendingPathComponent("folder-reconciliation", isDirectory: true),
            subscriptions: folderSubscriptions,
            observation: folderObservation,
            clock: reconciliationClock ?? { Date() },
            gate: gate
        )
        automaticScheduling = try AutomaticSchedulingStateMachine(
            stateDirectory: stateRoot.appendingPathComponent("automatic-scheduling", isDirectory: true),
            gate: gate
        )
        fileSubscriptions = try FileSubscriptionStore(
            stateDirectory: stateRoot.appendingPathComponent("file-subscriptions", isDirectory: true),
            gate: gate
        )
        canonicalSources = try CanonicalSourceRegistry(
            stateDirectory: stateRoot.appendingPathComponent("canonical-sources", isDirectory: true),
            gate: gate,
            identity: nativeIdentity ?? { NativeFileIdentity.key(for: $0) }
        )
        // The publication boundary rechecks automatic consent against these
        // stores, so they exist before it does.
        preparation = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: reader,
            fileSubscriptions: fileSubscriptions,
            folderSubscriptions: folderSubscriptions,
            canonicalSources: canonicalSources,
            gate: gate
        )
        folderMonitoring = FolderMonitoringCoordinator(
            accessStore: accessStore,
            preparation: preparation,
            queue: queue,
            subscriptions: folderSubscriptions,
            reader: reader,
            canonicalSources: canonicalSources,
            controlledStores: controlledStores,
            clock: reconciliationClock ?? { Date() }
        )
        folderReconciliationExecutor = FolderReconciliationExecutor(
            accessStore: accessStore,
            subscriptions: folderSubscriptions,
            observation: folderObservation,
            scheduler: folderReconciliation,
            reader: reader,
            gate: gate,
            controlledStores: controlledStores
        )
        folderBulkRuns = try FolderBulkRunStore(
            stateDirectory: stateRoot.appendingPathComponent("folder-bulk-runs", isDirectory: true)
        )
        folderBulkCoordinator = FolderBulkRunCoordinator(
            accessStore: accessStore,
            preparation: preparation,
            queue: queue,
            runs: folderBulkRuns
        )
        captureActivity = try CaptureActivityStore(
            stateDirectory: stateRoot.appendingPathComponent("capture-activity", isDirectory: true)
        )
        fileMonitoring = FileMonitoringCoordinator(
            accessStore: accessStore,
            preparation: preparation,
            queue: queue,
            subscriptions: fileSubscriptions,
            reader: reader,
            scheduling: automaticScheduling,
            publicationGate: gate,
            currentCapture: currentCapture,
            clock: clock
        )
    }

    /// Finishes any folder establishment an earlier process left pending, and
    /// returns the submissions that are now established.
    ///
    /// Startup calls this before folder monitoring runs or a folder is
    /// submitted. Until it does, a pending establishment's root group stays
    /// `provisional`: nothing can discover through it, monitoring leaves its
    /// subscription alone, and a new submission would miscount the watches that
    /// already exist.
    @discardableResult
    public func reconcilePendingFolderEstablishment() async -> [String] {
        await FolderCaptureSubmission.reconcilePending(environment: self)
    }

    /// Records what the durable queue and run ledgers now prove about each
    /// submitted item and applies the terminal retention bound. Called before
    /// and after every delivery pass, so a published item is recorded while it
    /// is still queued and its outcome is recorded as soon as it is known.
    ///
    /// It throws rather than falling back to whatever this process already
    /// holds. A caller that cannot persist what the owners now prove has not
    /// recorded anything, and must say so instead of presenting stale facts as
    /// current: a background delivery tick ignores the failure and claims
    /// nothing, while a surface that displays these facts reports them as
    /// unavailable and offers no action over them.
    @discardableResult
    public func recordCaptureActivity() throws -> [CaptureActivityRecord] {
        try captureActivity.observe(queue: queue.allEntries(), runs: folderBulkRuns.all())
    }

    /// The Disconnect action behind design §13.3: revoke this logical source here, durably and
    /// at once, and record the intent that tells Core.
    ///
    /// The revocation and the intent are one write in the access store, so nothing below can
    /// leave a source revoked with no one owing Core the news. The watches of the source are
    /// stopped after it: they would read nothing anyway (the grant refuses), and stopping
    /// them keeps the revoked source out of automatic scheduling.
    ///
    /// Each route's intent is frozen under the workspace and caller its published work and
    /// watch were frozen under. `fallback` is the companion's own binding, used only for a
    /// route that never published anything.
    @discardableResult
    public func disconnectSource(
        linkedDocumentID: String,
        fallback: CaptureDeliveryBinding
    ) async throws -> [OriginalDisconnectIntent] {
        let (bindings, watches) = frozenBindings(of: linkedDocumentID)
        let intents = try accessStore.disconnectSource(
            linkedDocumentID: linkedDocumentID,
            bindings: bindings,
            fallback: NativeRootAccessBinding(fallback)
        )
        for watch in watches where watch.state != .stopped {
            _ = try? await fileMonitoring.stop(subscriptionID: watch.subscriptionID)
        }
        return intents
    }

    /// The workspace and caller each route of one logical source was frozen under, by access
    /// reference: the queued work that bound the route, then the watch registered for it,
    /// which wins because it is the more recent statement of the same freeze. Also the
    /// watches themselves.
    func frozenBindings(
        of linkedDocumentID: String
    ) -> (bindings: [String: NativeRootAccessBinding], watches: [FileSubscription]) {
        var bindings: [String: NativeRootAccessBinding] = [:]
        for entry in queue.allEntries() {
            guard let original = entry.intent.originalBinding,
                  original.linkedDocumentId == linkedDocumentID
            else { continue }
            bindings[original.accessRecordId] = NativeRootAccessBinding(
                targetRef: entry.intent.targetRef,
                workspaceRef: entry.intent.workspaceRef,
                targetBindingRevision: entry.intent.targetBindingRevision,
                callerContextBinding: entry.intent.callerContextBinding
            )
        }
        let watches = fileSubscriptions.all().filter { $0.linkedDocumentID == linkedDocumentID }
        for watch in watches {
            bindings[watch.accessRef] = NativeRootAccessBinding(CaptureDeliveryBinding(watch))
        }
        return (bindings, watches)
    }

    /// The reconciler for the intents `disconnectSource` records. The composition root owns
    /// the transport, so this environment stays free of any process or CLI.
    public func originalDisconnectReconciler(
        transport: any OriginalDisconnectTransport
    ) -> OriginalDisconnectReconciler {
        OriginalDisconnectReconciler(accessStore: accessStore, transport: transport)
    }

    /// The retained root groups, as projections that carry no bookmark and no
    /// absolute route. The management window reads these so a folder that was
    /// added once can be listed as retained rather than as a watch.
    public func rootAccessGroups() -> [NativeRootAccessDescriptor] {
        accessStore.rootDescriptors()
    }

    /// The document routes this Mac holds, as projections that carry no bookmark
    /// and no absolute route. The management window reads these so a child whose
    /// folder-mediated reads have stopped can be shown as still read through a
    /// route of its own.
    public func documentAccessRoutes() -> [NativeAccessDescriptor] {
        accessStore.descriptors()
    }
}
