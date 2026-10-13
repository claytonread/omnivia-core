import Foundation

/// One in-process owner for the bookmark store and durable preparation queue.
/// File and folder intake must share this object so two stale in-memory bookmark
/// snapshots cannot overwrite one another.
public final class DocumentIntakeEnvironment: @unchecked Sendable {
    let accessStore: NativeAccessStore
    let preparation: CapturePreparationCoordinator

    public init(
        stateRoot: URL,
        bookmarkCodec: any SecurityScopedBookmarkCoding = SystemSecurityScopedBookmarkCodec(),
        reader: any CoordinatedOriginalReading = SystemCoordinatedOriginalReader()
    ) throws {
        accessStore = try NativeAccessStore(
            stateDirectory: stateRoot.appendingPathComponent("native-access", isDirectory: true),
            codec: bookmarkCodec
        )
        preparation = CapturePreparationCoordinator(
            accessStore: accessStore,
            queue: try CaptureIntentQueue(
                root: stateRoot.appendingPathComponent("capture-queue", isDirectory: true)
            ),
            reader: reader
        )
    }
}
