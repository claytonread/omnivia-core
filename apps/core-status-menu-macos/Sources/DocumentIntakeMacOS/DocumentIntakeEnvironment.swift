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
    public let folderBulkRuns: FolderBulkRunStore
    public let folderBulkCoordinator: FolderBulkRunCoordinator
    public let fileSubscriptions: FileSubscriptionStore
    public let fileMonitoring: FileMonitoringCoordinator

    public init(
        stateRoot: URL,
        bookmarkCodec: any SecurityScopedBookmarkCoding = SystemSecurityScopedBookmarkCodec(),
        reader: any CoordinatedOriginalReading = SystemCoordinatedOriginalReader()
    ) throws {
        self.reader = reader
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
            reader: reader
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
        fileMonitoring = FileMonitoringCoordinator(
            accessStore: accessStore,
            preparation: preparation,
            queue: queue,
            subscriptions: fileSubscriptions,
            reader: reader
        )
    }
}
