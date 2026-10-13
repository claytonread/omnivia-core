import CryptoKit
import Foundation

public enum CapturePreparationError: Error, Equatable {
    case emptySelection
    case duplicateItem
    case invalidBinding
    case unsupportedFile
    case fileTooLarge
    case fileChanged
    case coordinatedReadFailed
}

public struct CaptureIntentSeed: Sendable {
    public let submissionId: String
    public let itemId: String
    public let targetRef: String
    public let workspaceRef: String
    public let targetBindingRevision: String
    public let callerContextBinding: CallerContextBinding
    public let surface: CaptureSurface
    public let contentKind: CaptureContentKind
    public let payloadRef: String
    public let requestedSourceIdentity: String
    public let operationKey: String
    public let intentKind: CaptureIntentKind
    public let basePrecondition: CaptureBasePrecondition
    public let processingProfile: ProcessingProfileBinding
    public let originalBinding: OriginalBindingIntent?
    public let consent: ConsentBinding?
    public let bulkOrigin: BulkOriginBinding?
    public let createdAt: String
    public let displayName: String?

    public init(
        submissionId: String,
        itemId: String,
        targetRef: String,
        workspaceRef: String,
        targetBindingRevision: String,
        callerContextBinding: CallerContextBinding,
        surface: CaptureSurface,
        contentKind: CaptureContentKind,
        payloadRef: String,
        requestedSourceIdentity: String,
        operationKey: String,
        intentKind: CaptureIntentKind,
        basePrecondition: CaptureBasePrecondition,
        processingProfile: ProcessingProfileBinding,
        originalBinding: OriginalBindingIntent? = nil,
        consent: ConsentBinding? = nil,
        bulkOrigin: BulkOriginBinding? = nil,
        createdAt: String,
        displayName: String? = nil
    ) {
        self.submissionId = submissionId
        self.itemId = itemId
        self.targetRef = targetRef
        self.workspaceRef = workspaceRef
        self.targetBindingRevision = targetBindingRevision
        self.callerContextBinding = callerContextBinding
        self.surface = surface
        self.contentKind = contentKind
        self.payloadRef = payloadRef
        self.requestedSourceIdentity = requestedSourceIdentity
        self.operationKey = operationKey
        self.intentKind = intentKind
        self.basePrecondition = basePrecondition
        self.processingProfile = processingProfile
        self.originalBinding = originalBinding
        self.consent = consent
        self.bulkOrigin = bulkOrigin
        self.createdAt = createdAt
        self.displayName = displayName
    }

    fileprivate func freeze(payload: Data) throws -> CaptureIntentV3 {
        let digest = "sha256:" + SHA256.hash(data: payload).map { String(format: "%02x", $0) }.joined()
        return try CaptureIntentV3(
            submissionId: submissionId,
            itemId: itemId,
            targetRef: targetRef,
            workspaceRef: workspaceRef,
            targetBindingRevision: targetBindingRevision,
            callerContextBinding: callerContextBinding,
            surface: surface,
            contentKind: contentKind,
            payloadRef: payloadRef,
            byteLength: UInt64(payload.count),
            localIntegrityDigest: digest,
            requestedSourceIdentity: requestedSourceIdentity,
            operationKey: operationKey,
            intentKind: intentKind,
            basePrecondition: basePrecondition,
            processingProfile: processingProfile,
            originalBinding: originalBinding,
            consent: consent,
            bulkOrigin: bulkOrigin,
            createdAt: createdAt,
            displayName: displayName
        )
    }
}

public struct CapturePreparationItem: Sendable {
    public let access: NativeAccessExpectation
    public let seed: CaptureIntentSeed

    public init(access: NativeAccessExpectation, seed: CaptureIntentSeed) {
        self.access = access
        self.seed = seed
    }
}

public protocol CoordinatedOriginalReading: Sendable {
    func readStableBytes(from url: URL, maximumBytes: UInt64) throws -> Data
}

public struct SystemCoordinatedOriginalReader: CoordinatedOriginalReading {
    private static let bufferSize = 1024 * 1024

    public init() {}

    public func readStableBytes(from url: URL, maximumBytes: UInt64) throws -> Data {
        let coordinator = NSFileCoordinator(filePresenter: nil)
        var coordinationError: NSError?
        var result: Result<Data, Error>?
        coordinator.coordinate(readingItemAt: url, options: [], error: &coordinationError) { coordinatedURL in
            result = Result { try Self.readAndVerify(coordinatedURL, maximumBytes: maximumBytes) }
        }
        if let coordinationError { throw coordinationError }
        guard let result else { throw CapturePreparationError.coordinatedReadFailed }
        return try result.get()
    }

    private static func readAndVerify(_ url: URL, maximumBytes: UInt64) throws -> Data {
        let keys: Set<URLResourceKey> = [
            .isRegularFileKey,
            .fileSizeKey,
            .contentModificationDateKey,
            .fileResourceIdentifierKey,
        ]
        let before = try url.resourceValues(forKeys: keys)
        guard before.isRegularFile == true else { throw CapturePreparationError.unsupportedFile }
        guard let declaredSize = before.fileSize, declaredSize >= 0,
              UInt64(declaredSize) <= maximumBytes
        else { throw CapturePreparationError.fileTooLarge }

        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var data = Data()
        data.reserveCapacity(declaredSize)
        while let chunk = try handle.read(upToCount: bufferSize), !chunk.isEmpty {
            guard UInt64(data.count) + UInt64(chunk.count) <= maximumBytes else {
                throw CapturePreparationError.fileTooLarge
            }
            data.append(chunk)
        }
        let after = try url.resourceValues(forKeys: keys)
        guard after.isRegularFile == true,
              after.fileSize == before.fileSize,
              after.contentModificationDate == before.contentModificationDate,
              String(describing: after.fileResourceIdentifier) == String(describing: before.fileResourceIdentifier),
              data.count == declaredSize
        else { throw CapturePreparationError.fileChanged }
        return data
    }
}

/// Prepares original bytes away from the caller thread, then rechecks every route generation while
/// atomically publishing the complete selection to the durable queue.
public final class CapturePreparationCoordinator: @unchecked Sendable {
    private let accessStore: NativeAccessStore
    private let queue: CaptureIntentQueue
    private let reader: any CoordinatedOriginalReading

    public init(
        accessStore: NativeAccessStore,
        queue: CaptureIntentQueue,
        reader: any CoordinatedOriginalReading = SystemCoordinatedOriginalReader()
    ) {
        self.accessStore = accessStore
        self.queue = queue
        self.reader = reader
    }

    public func prepareAndPublish(_ items: [CapturePreparationItem]) async throws -> [CaptureQueueEntry] {
        guard !items.isEmpty else { throw CapturePreparationError.emptySelection }
        guard Set(items.map { $0.seed.itemId }).count == items.count,
              Set(items.map { $0.access.accessRef }).count == items.count
        else { throw CapturePreparationError.duplicateItem }
        for item in items {
            if let binding = item.seed.originalBinding {
                guard binding.accessRecordId == item.access.accessRef,
                      binding.bindingGeneration == item.access.generation
                else { throw CapturePreparationError.invalidBinding }
            }
        }

        let accessStore = self.accessStore
        let queue = self.queue
        let reader = self.reader
        return try await Task.detached(priority: .userInitiated) {
            var prepared: [(intent: CaptureIntentV3, payload: Data)] = []
            for item in items {
                var lastChanged = false
                var payload: Data?
                for _ in 0..<3 {
                    do {
                        payload = try accessStore.withReadAccess(
                            accessRef: item.access.accessRef,
                            expectedGeneration: item.access.generation
                        ) { url in
                            try reader.readStableBytes(
                                from: url,
                                maximumBytes: CaptureIntentV3.maximumPayloadBytes
                            )
                        }
                        break
                    } catch CapturePreparationError.fileChanged {
                        lastChanged = true
                    }
                }
                guard let payload else {
                    if lastChanged { throw CapturePreparationError.fileChanged }
                    throw CapturePreparationError.coordinatedReadFailed
                }
                prepared.append((try item.seed.freeze(payload: payload), payload))
            }
            return try accessStore.withValidatedGenerations(items.map(\.access)) {
                try queue.enqueueBatch(prepared)
            }
        }.value
    }
}
