import Foundation

/// One in-process owner for the bookmark store and durable preparation queue.
/// File and folder intake must share this object so two stale in-memory bookmark
/// snapshots cannot overwrite one another.
public final class DocumentIntakeEnvironment: @unchecked Sendable {
    let accessStore: NativeAccessStore
    let preparation: CapturePreparationCoordinator
    /// The single shared publication gate every owner above was built with.
    let publicationGate: PublicationGate
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
        monotonicClock: (@Sendable () -> Int64)? = nil
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
        // The publication boundary rechecks automatic consent against these
        // stores, so they exist before it does.
        preparation = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: reader,
            fileSubscriptions: fileSubscriptions,
            folderSubscriptions: folderSubscriptions,
            gate: gate
        )
        folderMonitoring = FolderMonitoringCoordinator(
            accessStore: accessStore,
            preparation: preparation,
            queue: queue,
            subscriptions: folderSubscriptions,
            reader: reader,
            controlledStores: controlledStores
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

    /// The retained root groups, as projections that carry no bookmark and no
    /// absolute route. The management window reads these so a folder that was
    /// added once can be listed as retained rather than as a watch.
    public func rootAccessGroups() -> [NativeRootAccessDescriptor] {
        accessStore.rootDescriptors()
    }
}
