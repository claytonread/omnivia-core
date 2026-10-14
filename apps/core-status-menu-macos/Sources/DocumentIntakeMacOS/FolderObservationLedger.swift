import Darwin
import Foundation

public enum FolderObservationLedgerError: Error, Equatable {
    case invalidState
    case unsupportedSchema
    case stateUnavailable
    case tooLarge
    /// The ledger was checkpointed clean; this process may no longer record events.
    case closed
    case unknownStream
    case unknownSubscription
    case duplicateSubscription
    case staleConfigGeneration
    case staleEpoch
    case streamIdentityChanged
    case eventIDRegressed
    case malformedScope
    case outOfScope
    case invalidReason
    case emptyBatch
}

/// What an event stream is watching: the device and, when FSEvents knows it, the stream UUID that
/// makes event IDs comparable across runs.
public struct FolderObservationStreamIdentity: Codable, Equatable, Sendable {
    public let deviceID: Int64
    public let streamUUID: String?

    public init(deviceID: Int64, streamUUID: String?) {
        self.deviceID = deviceID
        self.streamUUID = streamUUID
    }
}

public struct FolderObservationApplicableSubscription: Codable, Equatable, Sendable {
    public let subscriptionID: String
    public let configGeneration: UInt64

    public init(subscriptionID: String, configGeneration: UInt64) {
        self.subscriptionID = subscriptionID
        self.configGeneration = configGeneration
    }
}

public enum FolderObservationReason: String, Codable, CaseIterable, Comparable, Sendable {
    // Reported by an event batch.
    case mustScanSubDirs = "must_scan_sub_dirs"
    case eventsDropped = "events_dropped"
    case rootChanged = "root_changed"
    // Raised by the ledger itself.
    case overflow
    case uncleanRun = "unclean_run"
    case newStream = "new_stream"
    case streamChanged = "stream_changed"
    case subscriptionAdded = "subscription_added"
    case configChanged = "config_changed"

    var reportedByEvents: Bool {
        switch self {
        case .mustScanSubDirs, .eventsDropped, .rootChanged: return true
        default: return false
        }
    }

    public static func < (lhs: Self, rhs: Self) -> Bool { lhs.rawValue < rhs.rawValue }
}

/// A bounded inspection target. Components are relative to the subscription's selected root; the
/// root itself is never a scope (that is a full reconciliation).
public struct FolderObservationScope: Codable, Hashable, Sendable {
    public enum Kind: String, Codable, Sendable {
        case member
        case subtree
    }

    public let kind: Kind
    public let components: [String]

    public init(kind: Kind, components: [String]) {
        self.kind = kind
        self.components = components
    }

    func covers(_ other: Self) -> Bool {
        switch kind {
        case .subtree: return other.components.starts(with: components)
        case .member: return other.kind == .member && other.components == components
        }
    }

    fileprivate static func ordered(_ lhs: Self, _ rhs: Self) -> Bool {
        for (left, right) in zip(lhs.components, rhs.components) where left != right {
            return left.utf8.lexicographicallyPrecedes(right.utf8)
        }
        if lhs.components.count != rhs.components.count {
            return lhs.components.count < rhs.components.count
        }
        return lhs.kind == .member && rhs.kind == .subtree
    }
}

public struct FolderObservationPendingScope: Codable, Equatable, Sendable {
    public let scope: FolderObservationScope
    public fileprivate(set) var revision: UInt64
}

/// What one subscription still owes the filesystem. `fullRevision != nil` means a full
/// reconciliation is owed, which subsumes every scope. Revisions are stream-wide and only grow, so
/// an acknowledgement for revision R can never erase anything merged after R.
public struct FolderObservationObligation: Codable, Equatable, Sendable {
    public let subscriptionID: String
    public let configGeneration: UInt64
    public fileprivate(set) var fullRevision: UInt64?
    public fileprivate(set) var reasons: [FolderObservationReason]
    public fileprivate(set) var scopes: [FolderObservationPendingScope]

    public var revision: UInt64 {
        max(fullRevision ?? 0, scopes.map(\.revision).max() ?? 0)
    }

    fileprivate var isEmpty: Bool { fullRevision == nil && scopes.isEmpty }
}

public struct FolderObservationStreamStatus: Equatable, Sendable {
    public let identity: FolderObservationStreamIdentity
    public let lastCoveredEventID: UInt64
    public let observationEpoch: UInt64
    /// True while an unclean previous run still owes its conservative full reconciliations.
    public let catchUp: Bool
    public let subscriptions: [FolderObservationApplicableSubscription]
}

public enum FolderObservationEventReason: Equatable, Sendable {
    case full(FolderObservationReason)
    case scope(FolderObservationScope)
}

public struct FolderObservationEvent: Equatable, Sendable {
    public let eventID: UInt64
    public let subscriptionID: String
    public let configGeneration: UInt64
    public let reason: FolderObservationEventReason

    public init(
        eventID: UInt64,
        subscriptionID: String,
        configGeneration: UInt64,
        reason: FolderObservationEventReason
    ) {
        self.eventID = eventID
        self.subscriptionID = subscriptionID
        self.configGeneration = configGeneration
        self.reason = reason
    }
}

public struct FolderObservationBatch: Equatable, Sendable {
    public let identity: FolderObservationStreamIdentity
    public let observationEpoch: UInt64
    public let events: [FolderObservationEvent]

    public init(
        identity: FolderObservationStreamIdentity,
        observationEpoch: UInt64,
        events: [FolderObservationEvent]
    ) {
        self.identity = identity
        self.observationEpoch = observationEpoch
        self.events = events
    }
}

/// Reports exactly what a reconciliation finished. Anything merged after `throughRevision` stays.
public enum FolderObservationCompletion: Equatable, Sendable {
    case full(throughRevision: UInt64)
    case scopes([FolderObservationScope], throughRevision: UInt64)
}

private struct FolderObservationStream: Codable, Equatable {
    var deviceID: Int64
    var streamUUID: String?
    var lastCoveredEventID: UInt64
    var observationEpoch: UInt64
    var revision: UInt64
    var catchUp: Bool
    var subscriptions: [FolderObservationApplicableSubscription]
    var obligations: [FolderObservationObligation]
}

private struct FolderObservationDocument: Codable {
    var schema: String
    var cleanShutdown: Bool
    var streams: [FolderObservationStream]
}

private struct FolderObservationHeader: Codable {
    let schema: String
}

/// Owner-only durable checkpoint for native folder event streams. It records which event ID each
/// stream has fully handed to a durable obligation, so a cursor can only advance in the same
/// `PublicationGate` transaction that persists the obligations the events produced.
///
/// It stores opaque subscription IDs and root-relative components only. It never sees an absolute
/// event path; the observer maps paths to subscriptions and relative scopes before ingestion.
public final class FolderObservationLedger: @unchecked Sendable {
    static let schema = "omnivia.folder-observation.v1"
    static let maximumStateBytes = 1024 * 1024
    static let maximumStreams = 16
    static let maximumSubscriptions = 256
    static let maximumScopesPerSubscription = 64
    static let maximumComponentDepth = FolderScopeLimits.standard.maximumDepth
    static let maximumEventsPerBatch = 4096

    private let gate: PublicationGate
    private let subscriptions: FolderSubscriptionStore
    private let directory: URL
    private let stateFile: URL
    private var document: FolderObservationDocument
    private var closed = false

    /// Opens the ledger and marks the run unclean until `markCleanShutdown()`. If the previous run
    /// never checkpointed clean, every applicable subscription owes a full reconciliation.
    public init(
        stateDirectory: URL,
        subscriptions: FolderSubscriptionStore,
        gate: PublicationGate = .processShared
    ) throws {
        self.gate = gate
        self.subscriptions = subscriptions
        directory = stateDirectory
        stateFile = stateDirectory.appendingPathComponent("folder-observation-v1.json")
        try Self.prepareDirectory(stateDirectory)
        var opened = try Self.load(stateFile) ?? FolderObservationDocument(
            schema: Self.schema,
            cleanShutdown: true,
            streams: []
        )
        if !opened.cleanShutdown {
            for index in opened.streams.indices {
                for subscription in opened.streams[index].subscriptions {
                    try Self.mergeFull(.uncleanRun, subscription, into: &opened.streams[index])
                }
                Self.normalize(&opened.streams[index])
            }
        }
        opened.cleanShutdown = false
        try Self.persist(opened, to: stateFile, directory: stateDirectory)
        document = opened
    }

    public func streams() -> [FolderObservationStreamStatus] {
        gate.withLock { document.streams.map(Self.status) }
    }

    public func stream(deviceID: Int64) -> FolderObservationStreamStatus? {
        gate.withLock { document.streams.first { $0.deviceID == deviceID }.map(Self.status) }
    }

    public func obligation(subscriptionID: String) -> FolderObservationObligation? {
        gate.withLock {
            document.streams.lazy
                .compactMap { $0.obligations.first { $0.subscriptionID == subscriptionID } }
                .first
        }
    }

    /// Declares the subscriptions a stream observes and returns its status. A new stream, a changed
    /// stream UUID, or `startNewEpoch` (event IDs wrapped) opens a new observation epoch with no
    /// comparable cursor, so every applicable subscription owes a full reconciliation. On a known
    /// stream, an added subscription or changed config generation owes one too.
    @discardableResult
    public func beginStream(
        identity: FolderObservationStreamIdentity,
        startEventID: UInt64,
        subscriptions applicable: [FolderObservationApplicableSubscription],
        startNewEpoch: Bool = false
    ) throws -> FolderObservationStreamStatus {
        try mutate { document in
            guard Self.validUUID(identity.streamUUID) else { throw FolderObservationLedgerError.invalidState }
            guard Set(applicable.map(\.subscriptionID)).count == applicable.count else {
                throw FolderObservationLedgerError.duplicateSubscription
            }
            let applicable = applicable.sorted { $0.subscriptionID < $1.subscriptionID }
            for subscription in applicable {
                guard let current = self.subscriptions.subscription(id: subscription.subscriptionID) else {
                    throw FolderObservationLedgerError.unknownSubscription
                }
                guard current.configGeneration == subscription.configGeneration else {
                    throw FolderObservationLedgerError.staleConfigGeneration
                }
                // A subscription has exactly one stream.
                guard !document.streams.contains(where: {
                    $0.deviceID != identity.deviceID
                        && $0.subscriptions.contains { $0.subscriptionID == subscription.subscriptionID }
                }) else { throw FolderObservationLedgerError.duplicateSubscription }
            }
            guard let index = document.streams.firstIndex(where: { $0.deviceID == identity.deviceID }) else {
                var stream = FolderObservationStream(
                    deviceID: identity.deviceID,
                    streamUUID: identity.streamUUID,
                    lastCoveredEventID: startEventID,
                    observationEpoch: 1,
                    revision: 0,
                    catchUp: false,
                    subscriptions: applicable,
                    obligations: []
                )
                for subscription in applicable {
                    try Self.mergeFull(.newStream, subscription, into: &stream)
                }
                Self.normalize(&stream)
                document.streams.append(stream)
                document.streams.sort { $0.deviceID < $1.deviceID }
                return Self.status(stream)
            }
            var stream = document.streams[index]
            if stream.streamUUID != identity.streamUUID || startNewEpoch {
                stream.streamUUID = identity.streamUUID
                stream.lastCoveredEventID = startEventID
                stream.observationEpoch += 1
                stream.subscriptions = applicable
                stream.obligations = []
                for subscription in applicable {
                    try Self.mergeFull(.streamChanged, subscription, into: &stream)
                }
            } else {
                let previous = stream.subscriptions
                stream.subscriptions = applicable
                for subscription in applicable {
                    let before = previous.first { $0.subscriptionID == subscription.subscriptionID }
                    if before == nil {
                        try Self.mergeFull(.subscriptionAdded, subscription, into: &stream)
                    } else if before != subscription {
                        stream.obligations.removeAll { $0.subscriptionID == subscription.subscriptionID }
                        try Self.mergeFull(.configChanged, subscription, into: &stream)
                    }
                }
                stream.obligations.removeAll { value in
                    !applicable.contains { $0.subscriptionID == value.subscriptionID }
                }
            }
            Self.normalize(&stream)
            document.streams[index] = stream
            return Self.status(stream)
        }
    }

    /// Merges every obligation the batch implies and advances the covered event ID in one durable
    /// write. If anything is invalid or the write fails, neither moves. Returns the new covered ID.
    @discardableResult
    public func ingest(_ batch: FolderObservationBatch) throws -> UInt64 {
        try mutate { document in
            guard !batch.events.isEmpty else { throw FolderObservationLedgerError.emptyBatch }
            guard batch.events.count <= Self.maximumEventsPerBatch else {
                throw FolderObservationLedgerError.tooLarge
            }
            guard let index = document.streams.firstIndex(where: { $0.deviceID == batch.identity.deviceID })
            else { throw FolderObservationLedgerError.unknownStream }
            var stream = document.streams[index]
            guard stream.streamUUID == batch.identity.streamUUID else {
                throw FolderObservationLedgerError.streamIdentityChanged
            }
            guard stream.observationEpoch == batch.observationEpoch else {
                throw FolderObservationLedgerError.staleEpoch
            }
            var previousID = stream.lastCoveredEventID
            for event in batch.events {
                // Equal IDs are a legitimate replay of the covered event; anything lower is a
                // wrap or an unrelated stream. UInt64.max is reserved as the "since now" sentinel.
                guard event.eventID >= previousID, event.eventID != UInt64.max else {
                    throw FolderObservationLedgerError.eventIDRegressed
                }
                previousID = event.eventID
                let applicable = FolderObservationApplicableSubscription(
                    subscriptionID: event.subscriptionID,
                    configGeneration: event.configGeneration
                )
                guard stream.subscriptions.contains(where: { $0.subscriptionID == event.subscriptionID })
                else { throw FolderObservationLedgerError.unknownSubscription }
                guard stream.subscriptions.contains(applicable),
                      let current = self.subscriptions.subscription(id: event.subscriptionID),
                      current.configGeneration == event.configGeneration
                else { throw FolderObservationLedgerError.staleConfigGeneration }
                switch event.reason {
                case let .full(reason):
                    guard reason.reportedByEvents else { throw FolderObservationLedgerError.invalidReason }
                    try Self.mergeFull(reason, applicable, into: &stream)
                case let .scope(scope):
                    try Self.validate(scope, recursive: current.recursive, includeHidden: current.includeHidden)
                    try Self.mergeScope(scope, applicable, into: &stream)
                }
            }
            stream.lastCoveredEventID = previousID
            Self.normalize(&stream)
            document.streams[index] = stream
            return previousID
        }
    }

    /// Clears only what the reconciliation finished, for this subscription, config generation and
    /// epoch. Scopes already coalesced into a broader or newer entry stay with that entry.
    public func acknowledge(
        subscriptionID: String,
        configGeneration: UInt64,
        observationEpoch: UInt64,
        completion: FolderObservationCompletion
    ) throws {
        try mutate { document in
            guard let index = document.streams.firstIndex(where: {
                $0.subscriptions.contains { $0.subscriptionID == subscriptionID }
            }) else { throw FolderObservationLedgerError.unknownSubscription }
            var stream = document.streams[index]
            guard stream.subscriptions.contains(where: {
                $0.subscriptionID == subscriptionID && $0.configGeneration == configGeneration
            }) else { throw FolderObservationLedgerError.staleConfigGeneration }
            guard stream.observationEpoch == observationEpoch else {
                throw FolderObservationLedgerError.staleEpoch
            }
            guard let at = stream.obligations.firstIndex(where: { $0.subscriptionID == subscriptionID })
            else { return }
            var value = stream.obligations[at]
            switch completion {
            case let .full(through):
                if let full = value.fullRevision, full <= through {
                    value.fullRevision = nil
                    value.reasons = []
                }
                value.scopes.removeAll { $0.revision <= through }
            case let .scopes(done, through):
                let doneSet = Set(done)
                value.scopes.removeAll { doneSet.contains($0.scope) && $0.revision <= through }
            }
            if value.isEmpty {
                stream.obligations.remove(at: at)
            } else {
                stream.obligations[at] = value
            }
            Self.normalize(&stream)
            document.streams[index] = stream
        }
    }

    /// Checkpoints a clean shutdown. The next open will not demand catch-up reconciliation, and
    /// this instance records nothing further.
    public func markCleanShutdown() throws {
        try gate.withLock {
            guard !closed else { return }
            var next = document
            next.cleanShutdown = true
            try Self.persist(next, to: stateFile, directory: directory)
            document = next
            closed = true
        }
    }

    // MARK: - Transactions

    private func mutate<T>(_ change: (inout FolderObservationDocument) throws -> T) throws -> T {
        try gate.withLock {
            guard !closed else { throw FolderObservationLedgerError.closed }
            var next = document
            let result = try change(&next)
            try Self.validate(next)
            try Self.persist(next, to: stateFile, directory: directory)
            document = next
            return result
        }
    }

    // MARK: - Merging

    private static func nextRevision(_ stream: inout FolderObservationStream) throws -> UInt64 {
        let (value, overflow) = stream.revision.addingReportingOverflow(1)
        guard !overflow else { throw FolderObservationLedgerError.tooLarge }
        stream.revision = value
        return value
    }

    private static func mergeFull(
        _ reason: FolderObservationReason,
        _ subscription: FolderObservationApplicableSubscription,
        into stream: inout FolderObservationStream
    ) throws {
        let revision = try nextRevision(&stream)
        var value = take(subscription, from: &stream)
        value.fullRevision = revision
        value.scopes = []
        value.reasons = Array(Set(value.reasons + [reason])).sorted()
        stream.obligations.append(value)
        stream.obligations.sort { $0.subscriptionID < $1.subscriptionID }
    }

    private static func mergeScope(
        _ scope: FolderObservationScope,
        _ subscription: FolderObservationApplicableSubscription,
        into stream: inout FolderObservationStream
    ) throws {
        let revision = try nextRevision(&stream)
        var value = take(subscription, from: &stream)
        defer {
            stream.obligations.append(value)
            stream.obligations.sort { $0.subscriptionID < $1.subscriptionID }
        }
        if value.fullRevision != nil {
            // A newer event must outlive an acknowledgement of the full scan already running.
            value.fullRevision = revision
            return
        }
        if let covering = value.scopes.firstIndex(where: { $0.scope.covers(scope) }) {
            value.scopes[covering].revision = revision
            return
        }
        value.scopes.removeAll { scope.covers($0.scope) }
        value.scopes.append(FolderObservationPendingScope(scope: scope, revision: revision))
        value.scopes.sort { FolderObservationScope.ordered($0.scope, $1.scope) }
        if value.scopes.count > maximumScopesPerSubscription {
            value.fullRevision = revision
            value.scopes = []
            value.reasons = [.overflow]
        }
    }

    private static func take(
        _ subscription: FolderObservationApplicableSubscription,
        from stream: inout FolderObservationStream
    ) -> FolderObservationObligation {
        if let index = stream.obligations.firstIndex(where: { $0.subscriptionID == subscription.subscriptionID }) {
            return stream.obligations.remove(at: index)
        }
        return FolderObservationObligation(
            subscriptionID: subscription.subscriptionID,
            configGeneration: subscription.configGeneration,
            fullRevision: nil,
            reasons: [],
            scopes: []
        )
    }

    private static func normalize(_ stream: inout FolderObservationStream) {
        stream.obligations.removeAll(where: \.isEmpty)
        stream.catchUp = stream.obligations.contains { $0.reasons.contains(.uncleanRun) }
    }

    private static func status(_ stream: FolderObservationStream) -> FolderObservationStreamStatus {
        FolderObservationStreamStatus(
            identity: FolderObservationStreamIdentity(
                deviceID: stream.deviceID,
                streamUUID: stream.streamUUID
            ),
            lastCoveredEventID: stream.lastCoveredEventID,
            observationEpoch: stream.observationEpoch,
            catchUp: stream.catchUp,
            subscriptions: stream.subscriptions
        )
    }

    // MARK: - Validation

    private static func validate(
        _ scope: FolderObservationScope,
        recursive: Bool,
        includeHidden: Bool
    ) throws {
        guard !scope.components.isEmpty, scope.components.count <= maximumComponentDepth,
              scope.components.allSatisfy(validComponent)
        else { throw FolderObservationLedgerError.malformedScope }
        // Names the subscription never approved must not reach disk.
        guard recursive || (scope.kind == .member && scope.components.count == 1),
              includeHidden || !scope.components.contains(where: { $0.hasPrefix(".") })
        else { throw FolderObservationLedgerError.outOfScope }
    }

    static func validComponent(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= 255 && value != "." && value != ".."
            && !value.unicodeScalars.contains { $0 == "/" || $0.value < 0x20 || $0.value == 0x7F }
    }

    private static func validIdentifier(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= 128
            && value.unicodeScalars.allSatisfy {
                CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
                    .contains($0)
            }
    }

    private static func validUUID(_ value: String?) -> Bool {
        value.map { UUID(uuidString: $0)?.uuidString == $0 } ?? true
    }

    /// Everything a document must satisfy, on load and before every write, so a state this code
    /// would refuse to read is never written.
    private static func validate(_ document: FolderObservationDocument) throws {
        guard document.schema == schema,
              document.streams.count <= maximumStreams,
              Set(document.streams.map(\.deviceID)).count == document.streams.count,
              document.streams.map(\.deviceID) == document.streams.map(\.deviceID).sorted(),
              document.streams.reduce(0, { $0 + $1.subscriptions.count }) <= maximumSubscriptions
        else { throw FolderObservationLedgerError.invalidState }
        var seen = Set<String>()
        for stream in document.streams {
            let ids = stream.subscriptions.map(\.subscriptionID)
            guard validUUID(stream.streamUUID),
                  stream.observationEpoch > 0,
                  stream.lastCoveredEventID != UInt64.max,
                  ids == ids.sorted(),
                  ids.allSatisfy(validIdentifier),
                  stream.subscriptions.allSatisfy({ $0.configGeneration > 0 }),
                  seen.isDisjoint(with: ids), Set(ids).count == ids.count,
                  stream.obligations.map(\.subscriptionID) == stream.obligations.map(\.subscriptionID).sorted(),
                  stream.catchUp == stream.obligations.contains(where: { $0.reasons.contains(.uncleanRun) })
            else { throw FolderObservationLedgerError.invalidState }
            seen.formUnion(ids)
            for value in stream.obligations {
                let scopes = value.scopes.map(\.scope)
                guard stream.subscriptions.contains(where: {
                          $0.subscriptionID == value.subscriptionID
                              && $0.configGeneration == value.configGeneration
                      }),
                      !value.isEmpty,
                      value.reasons == Array(Set(value.reasons)).sorted(),
                      (value.fullRevision == nil) == value.reasons.isEmpty,
                      value.fullRevision == nil || value.scopes.isEmpty,
                      value.scopes.count <= maximumScopesPerSubscription,
                      value.revision <= stream.revision,
                      value.scopes.allSatisfy({ $0.revision > 0 }),
                      (value.fullRevision ?? 1) > 0,
                      scopes.allSatisfy({ !$0.components.isEmpty && $0.components.count <= maximumComponentDepth
                          && $0.components.allSatisfy(validComponent) }),
                      scopes == scopes.sorted(by: FolderObservationScope.ordered),
                      !scopes.indices.contains(where: { first in
                          scopes.indices.contains { $0 != first && scopes[first].covers(scopes[$0]) }
                      })
                else { throw FolderObservationLedgerError.invalidState }
            }
            guard Set(stream.obligations.map(\.subscriptionID)).count == stream.obligations.count else {
                throw FolderObservationLedgerError.invalidState
            }
        }
    }

    // MARK: - Persistence

    private static func load(_ stateFile: URL) throws -> FolderObservationDocument? {
        guard FileManager.default.fileExists(atPath: stateFile.path) else { return nil }
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: stateFile.path)
            guard (attributes[.type] as? FileAttributeType) == .typeRegular,
                  let size = attributes[.size] as? NSNumber,
                  size.uint64Value <= UInt64(maximumStateBytes),
                  let owner = attributes[.ownerAccountID] as? NSNumber,
                  owner.uint32Value == Darwin.getuid(),
                  let permissions = attributes[.posixPermissions] as? NSNumber,
                  permissions.intValue & 0o077 == 0
            else { throw FolderObservationLedgerError.invalidState }
            let data = try Data(contentsOf: stateFile)
            // A newer build's document is refused by schema before its shape is judged.
            let header = try JSONDecoder().decode(FolderObservationHeader.self, from: data)
            guard header.schema == schema else { throw FolderObservationLedgerError.unsupportedSchema }
            let document = try JSONDecoder().decode(FolderObservationDocument.self, from: data)
            try validate(document)
            return document
        } catch let error as FolderObservationLedgerError {
            throw error
        } catch {
            throw FolderObservationLedgerError.invalidState
        }
    }

    private static func persist(
        _ document: FolderObservationDocument,
        to stateFile: URL,
        directory: URL
    ) throws {
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
            let data = try encoder.encode(document)
            guard data.count <= maximumStateBytes else { throw FolderObservationLedgerError.tooLarge }
            let temporary = directory.appendingPathComponent(".folder-observation-\(UUID().uuidString).tmp")
            try data.write(to: temporary, options: [.atomic])
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: temporary.path)
            if FileManager.default.fileExists(atPath: stateFile.path) {
                _ = try FileManager.default.replaceItemAt(stateFile, withItemAt: temporary)
            } else {
                try FileManager.default.moveItem(at: temporary, to: stateFile)
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateFile.path)
            let descriptor = Darwin.open(directory.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
            if descriptor >= 0 {
                _ = Darwin.fsync(descriptor)
                Darwin.close(descriptor)
            }
        } catch let error as FolderObservationLedgerError {
            throw error
        } catch {
            throw FolderObservationLedgerError.stateUnavailable
        }
    }

    private static func prepareDirectory(_ directory: URL) throws {
        do {
            try FileManager.default.createDirectory(
                at: directory,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let values = try directory.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
            guard values.isDirectory == true, values.isSymbolicLink != true else {
                throw FolderObservationLedgerError.stateUnavailable
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        } catch let error as FolderObservationLedgerError {
            throw error
        } catch {
            throw FolderObservationLedgerError.stateUnavailable
        }
    }
}
