import CryptoKit
import Foundation

/// The one coordination primitive every publication-relevant mutation shares.
///
/// Lock order: this gate is the outermost and only lock in the publication path.
/// `NativeAccessStore`, `FileSubscriptionStore`, `FolderSubscriptionStore` and
/// `CaptureIntentQueue` each hold it for the whole of their own critical
/// sections, so the final recheck inside `NativeAccessStore
/// .withValidatedGenerations` can read the subscription stores and commit the
/// queue batch without an owner relink, a subscription pause, a reconfigure, a
/// dirty-epoch bump or a delivery claim interleaving. It is recursive because
/// that nesting all happens on one thread; nothing held under it ever waits on
/// another thread, and no second lock is taken before it, so there is no cycle
/// to deadlock on. `CapturePreparationCoordinator.ticketLock` is a leaf taken
/// under the gate and never takes it.
///
/// Production shares exactly one gate — see `DocumentIntakeEnvironment`. The
/// default argument gives a standalone owner its own, so a test can build one
/// store in isolation.
/// ponytail: one lock for all intake state. Split it per owner with an explicit
/// order only if contention ever shows up in a profile.
public final class PublicationGate: @unchecked Sendable {
    /// Safe default for owners assembled outside `DocumentIntakeEnvironment`.
    /// Production creates an explicitly scoped gate for each intake state root.
    public static let processShared = PublicationGate()

    private let lock = NSRecursiveLock()

    public init() {}

    public func withLock<T>(_ body: () throws -> T) rethrows -> T {
        try lock.withLock(body)
    }

    func lockForTesting() { lock.lock() }
    func unlockForTesting() { lock.unlock() }
}

public enum CapturePreparationError: Error, Equatable {
    case emptySelection
    case duplicateItem
    case invalidBinding
    case unsupportedFile
    case fileTooLarge
    case fileChanged
    case coordinatedReadFailed
    /// The submission was cancelled, or its preparation tokens no longer name
    /// exactly this batch, before publication committed.
    case cancelled
    /// A frozen intent does not carry the exact caller, target and workspace
    /// binding this publication was authorised for.
    case bindingMismatch
    /// The subscription an automatic item was prepared against has been
    /// suppressed, reconfigured, or has moved on to a later dirty epoch.
    case consentChanged
}

/// A caller's own post-publication update, run inside the publication critical
/// section immediately after the queue commits the batch. See
/// `CapturePreparationCoordinator.prepareAndPublish`.
public typealias CapturePublicationCompletion = @Sendable ([CaptureQueueEntry]) throws -> Void

/// Queue publication committed and the caller's completion, run in the same
/// critical section, did not.
///
/// The rename into the published namespace is the filesystem commit point, so
/// `published` exists: it is in the queue, rebuildable from its own closed batch
/// manifest at startup, and delivery will pick it up. Nothing here is rolled
/// back or deleted, because neither would be true.
///
/// What is missing is the caller's obligation, and the truthful repair is to
/// replay the same submission with the same item identities. `CaptureIntentQueue
/// .publish` reconciles such a replay to the batch already published rather than
/// duplicating it, and the completion runs again on that replay, so the
/// obligation can still be recorded. A caller that cannot reproduce those exact
/// identities must instead resolve the entries named here.
public struct CapturePublicationIncomplete: Error {
    /// The entries publication committed before the completion failed.
    public let published: [CaptureQueueEntry]
    public let cause: Error
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
    /// The persisted dirty epoch this automatic item was prepared against; 0
    /// for a member this scan discovered and has not persisted yet. Required
    /// for an item carrying consent and rejected for any other item, so an
    /// automatic publication always has something to revalidate.
    public let observedDirtyEpoch: UInt64?

    public init(
        access: NativeAccessExpectation,
        seed: CaptureIntentSeed,
        observedDirtyEpoch: UInt64? = nil
    ) {
        self.access = access
        self.seed = seed
        self.observedDirtyEpoch = observedDirtyEpoch
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

/// Prepares original bytes away from the caller thread, then revalidates the complete batch while
/// atomically publishing it to the durable queue.
///
/// Preparation and publication are separate phases. Off the gate, the originals are read and the
/// whole batch is written and verified as one queue candidate directory. Publication is then one
/// critical section on the shared `PublicationGate`, the same gate every owner relink, disconnect,
/// adopt, subscription register/update and delivery claim holds. Inside it every item is rechecked
/// together — its access generations, its frozen caller binding, its subscription facts, its
/// preparation token and the submission's cancellation — and only then is the prepared candidate
/// renamed into the published namespace, still under the same gate. No payload byte is copied and
/// nothing remote is called inside that section. A rejected batch publishes nothing and its
/// candidate is discarded, so no mutation can interleave between the final validation and the
/// commit, and no delivery claim observes a half-completed publication.
///
/// An automatic caller may also hand `prepareAndPublish` one bounded local update of its own,
/// which runs in that same critical section once the queue has committed. The queue commit is the
/// filesystem commit point and nothing after it can take it back, so a failure there is reported
/// as `CapturePublicationIncomplete` over the entries that exist, not as a failed publication.
public final class CapturePreparationCoordinator: @unchecked Sendable {
    private let accessStore: NativeAccessStore
    private let queue: CaptureIntentQueue
    private let reader: any CoordinatedOriginalReading
    private let fileSubscriptions: FileSubscriptionStore?
    private let folderSubscriptions: FolderSubscriptionStore?
    private let gate: PublicationGate

    /// Test seam: runs inside the publication critical section, after every
    /// item's consent has been revalidated and before the queue commits. Set it
    /// before the call that should observe it; production leaves it nil.
    var afterConsentValidation: (() -> Void)?

    /// Leaf under `gate`: taken while the gate is held and never takes it.
    private let ticketLock = NSLock()
    /// Live preparation tokens per submission. A submission is cancelled by
    /// name, and stays cancelled: an identity that was withdrawn must never
    /// publish, not even on a later retry.
    /// ponytail: in memory only — a crash drops the whole in-flight
    /// preparation anyway. Persist it only if cancellation has to outlive the
    /// process.
    private var openPreparations: [String: Set<String>] = [:]
    private var cancelledSubmissions: Set<String> = []

    public init(
        accessStore: NativeAccessStore,
        queue: CaptureIntentQueue,
        reader: any CoordinatedOriginalReading = SystemCoordinatedOriginalReader(),
        fileSubscriptions: FileSubscriptionStore? = nil,
        folderSubscriptions: FolderSubscriptionStore? = nil,
        gate: PublicationGate = .processShared
    ) {
        self.accessStore = accessStore
        self.queue = queue
        self.reader = reader
        self.fileSubscriptions = fileSubscriptions
        self.folderSubscriptions = folderSubscriptions
        self.gate = gate
    }

    /// Withdraws a submission. A cancellation that lands while its bytes are
    /// still being read is seen by the publication boundary, which then rejects
    /// the whole batch, including a batch of snapshot-only items.
    ///
    /// It takes the shared gate so it is ordered against publication the same
    /// way every other mutation is: it either lands before the final validation
    /// and rejects the batch, or after the commit, never between the two.
    public func cancel(submissionId: String) {
        gate.withLock {
            ticketLock.withLock {
                cancelledSubmissions.insert(submissionId)
                openPreparations[submissionId] = []
            }
        }
    }

    /// Publishes one complete prepared selection for exactly `binding`.
    ///
    /// `onPublished` is an automatic caller's own obligation — the scheduling
    /// state that has to say "this scope's candidate reached a published
    /// intent". It runs inside the publication critical section, after consent
    /// and generation validation and after the queue commit, with the same
    /// recursive gate still held, so no pause, reconfigure, relink or delivery
    /// claim can land between the validation, the rename and it. It is handed
    /// the published entries, which is where the opaque refs it must record come
    /// from. The expensive read, hash and candidate preparation all stay outside
    /// the gate, and this completion must do no more than its own bounded local
    /// commit.
    ///
    /// A completion that throws does not unpublish anything: the rename is the
    /// commit point, so the batch stays published and
    /// `CapturePublicationIncomplete` reports it with the entries that exist.
    /// Manual callers pass nothing and are unaffected.
    public func prepareAndPublish(
        _ items: [CapturePreparationItem],
        binding: CaptureDeliveryBinding,
        onPublished: CapturePublicationCompletion? = nil
    ) async throws -> [CaptureQueueEntry] {
        guard !items.isEmpty else { throw CapturePreparationError.emptySelection }
        guard Set(items.map { $0.seed.itemId }).count == items.count,
              Set(items.map { $0.access.accessRef }).count == items.count
        else { throw CapturePreparationError.duplicateItem }
        // One submission publishes as one unit, so one cancellation covers it.
        let submissionId = items[0].seed.submissionId
        guard items.allSatisfy({ $0.seed.submissionId == submissionId }) else {
            throw CapturePreparationError.invalidBinding
        }
        for item in items {
            if let original = item.seed.originalBinding {
                guard original.accessRecordId == item.access.accessRef,
                      original.bindingGeneration == item.access.generation
                else { throw CapturePreparationError.invalidBinding }
            }
            guard (item.seed.consent == nil) == (item.observedDirtyEpoch == nil) else {
                throw CapturePreparationError.invalidBinding
            }
        }

        let tokens = Set(items.map { _ in UUID().uuidString })
        try openPreparation(submissionId: submissionId, tokens: tokens)
        defer { closePreparation(submissionId: submissionId) }

        let reader = self.reader
        return try await Task.detached(priority: .userInitiated) {
            var prepared: [(intent: CaptureIntentV3, payload: Data)] = []
            for item in items {
                var lastChanged = false
                var payload: Data?
                for _ in 0..<3 {
                    do {
                        payload = try self.accessStore.withReadAccess(
                            accessRef: item.access.accessRef,
                            expectedGeneration: item.access.generation,
                            expectedRootBindingGeneration: item.access.rootBindingGeneration
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
            // Every payload byte is written and verified here, before any
            // authority is locked. Publication below is a directory rename.
            let candidate = try self.queue.prepareCandidate(prepared)
            defer { self.queue.discard(candidate) }
            return try self.accessStore.withValidatedGenerations(items.map(\.access)) {
                try self.consume(submissionId: submissionId, tokens: tokens)
                for (item, frozen) in zip(items, prepared) {
                    guard binding.matches(frozen.intent) else {
                        throw CapturePreparationError.bindingMismatch
                    }
                    try self.validateConsent(
                        frozen.intent,
                        observedDirtyEpoch: item.observedDirtyEpoch
                    )
                }
                self.afterConsentValidation?()
                let published = try self.queue.publish(candidate)
                guard let onPublished else { return published }
                do {
                    try onPublished(published)
                } catch {
                    // The rename already happened, so this is not a failed
                    // publication. Report what was published rather than an
                    // error the caller would read as "nothing committed".
                    throw CapturePublicationIncomplete(published: published, cause: error)
                }
                return published
            }
        }.value
    }

    /// Rechecks the subscription an automatic item was prepared against. The
    /// subscription stores are read inside the publication critical section and
    /// take the same shared gate, so a register/update cannot land between this
    /// recheck and the queue commit that follows it.
    private func validateConsent(
        _ intent: CaptureIntentV3,
        observedDirtyEpoch: UInt64?
    ) throws {
        guard let consent = intent.consent else { return }
        guard let observedDirtyEpoch else { throw CapturePreparationError.invalidBinding }
        switch intent.surface {
        case .automaticFile:
            let watched = fileSubscriptions?.all().first {
                $0.subscriptionID == consent.subscriptionId
            }
            guard let current = watched,
                  Self.acknowledged(current.state),
                  current.consentGeneration == consent.consentGeneration,
                  current.configGeneration == consent.configGeneration,
                  current.dirtyEpoch == observedDirtyEpoch
            else { throw CapturePreparationError.consentChanged }
        case .automaticFolder:
            guard let current = folderSubscriptions?.subscription(id: consent.subscriptionId),
                  Self.acknowledged(current.state),
                  current.consentGeneration == consent.consentGeneration,
                  current.configGeneration == consent.configGeneration,
                  let memberId = intent.originalBinding?.memberId
            else { throw CapturePreparationError.consentChanged }
            // A member this scan discovered is not persisted until the scan
            // commits, so it was prepared against no epoch at all.
            let persisted = current.members.first { $0.memberID == memberId }
            guard (persisted?.dirtyEpoch ?? 0) == observedDirtyEpoch else {
                throw CapturePreparationError.consentChanged
            }
        default:
            throw CapturePreparationError.invalidBinding
        }
    }

    /// Consent is acknowledged and unsuppressed only while the subscription is
    /// running or still waiting for Core to admit its reviewed baseline.
    private static func acknowledged(_ state: FolderSubscriptionState) -> Bool {
        state == .active || state == .waitingForCore
    }

    private func openPreparation(submissionId: String, tokens: Set<String>) throws {
        try ticketLock.withLock {
            guard !cancelledSubmissions.contains(submissionId) else {
                throw CapturePreparationError.cancelled
            }
            guard openPreparations[submissionId] == nil else {
                throw CapturePreparationError.duplicateItem
            }
            openPreparations[submissionId] = tokens
        }
    }

    /// Every token minted for this preparation has to still be live and name
    /// exactly this batch, so a cancelled or partial selection cannot publish.
    private func consume(submissionId: String, tokens: Set<String>) throws {
        try ticketLock.withLock {
            guard openPreparations[submissionId] == tokens else {
                throw CapturePreparationError.cancelled
            }
            openPreparations[submissionId] = []
        }
    }

    private func closePreparation(submissionId: String) {
        ticketLock.withLock { _ = openPreparations.removeValue(forKey: submissionId) }
    }
}
