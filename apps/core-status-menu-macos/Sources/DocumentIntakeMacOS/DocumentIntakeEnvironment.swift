import Foundation

/// One in-process owner for the bookmark store and durable preparation queue.
/// File and folder intake must share this object so two stale in-memory bookmark
/// snapshots cannot overwrite one another.
public final class DocumentIntakeEnvironment: @unchecked Sendable {
    let accessStore: NativeAccessStore
    let preparation: CapturePreparationCoordinator
    public let queue: CaptureIntentQueue
    public let folderSubscriptions: FolderSubscriptionStore
    public let folderMonitoring: FolderMonitoringCoordinator
    public let fileSubscriptions: FileSubscriptionStore
    public let fileMonitoring: FileMonitoringCoordinator

    public init(
        stateRoot: URL,
        bookmarkCodec: any SecurityScopedBookmarkCoding = SystemSecurityScopedBookmarkCodec(),
        reader: any CoordinatedOriginalReading = SystemCoordinatedOriginalReader()
    ) throws {
        accessStore = try NativeAccessStore(
            stateDirectory: stateRoot.appendingPathComponent("native-access", isDirectory: true),
            codec: bookmarkCodec
        )
        queue = try CaptureIntentQueue(
            root: stateRoot.appendingPathComponent("capture-queue", isDirectory: true)
        )
        preparation = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: queue,
            reader: reader
        )
        folderSubscriptions = try FolderSubscriptionStore(
            stateDirectory: stateRoot.appendingPathComponent("folder-subscriptions", isDirectory: true)
        )
        folderMonitoring = FolderMonitoringCoordinator(
            accessStore: accessStore,
            preparation: preparation,
            queue: queue,
            subscriptions: folderSubscriptions,
            reader: reader
        )
        fileSubscriptions = try FileSubscriptionStore(
            stateDirectory: stateRoot.appendingPathComponent("file-subscriptions", isDirectory: true)
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
