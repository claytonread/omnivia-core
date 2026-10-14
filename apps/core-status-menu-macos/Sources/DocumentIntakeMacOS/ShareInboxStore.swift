import CryptoKit
import Darwin
import Foundation

/// One captured copy inside a published Share envelope.
public struct ShareInboxItemV1: Codable, Equatable, Sendable {
    fileprivate static let encodedFieldNames: Set<String> = [
        "slotID", "displayName", "relativePath", "kind", "byteLength", "digest",
    ]

    public let slotID: String
    /// What the person will see in review. Also the name the companion's own
    /// copy takes, so it is a plain safe filename and nothing else.
    public let displayName: String
    /// The opaque leaf name of this item's bytes inside the envelope directory.
    public let relativePath: String
    public let kind: ShareSnapshotKind
    public let byteLength: UInt64
    /// `sha256:<hex>` of the bytes as the extension wrote them. The companion
    /// proves its own copy against this before it owns it.
    public let digest: String

    public init(
        slotID: String,
        displayName: String,
        relativePath: String,
        kind: ShareSnapshotKind,
        byteLength: UInt64,
        digest: String
    ) {
        self.slotID = slotID
        self.displayName = displayName
        self.relativePath = relativePath
        self.kind = kind
        self.byteLength = byteLength
        self.digest = digest
    }
}

/// One complete Share request, durable in the App Group until the companion
/// owns its bytes.
///
/// This is snapshot-only by construction: it carries no bookmark, no grant, no
/// access reference and no authentication claim, and it names no destination,
/// workspace or caller. It has no lifetime either — it is not the
/// grant-bearing handoff offer and must never reuse that offer's 120-second
/// transfer window, because the whole point of this spool is to survive a
/// companion that is closed.
public struct ShareInboxEnvelopeV1: Codable, Equatable, Sendable {
    public static let schemaID = "omnivia.share-inbox.envelope.v1"
    fileprivate static let encodedFieldNames: Set<String> = [
        "schema", "requestID", "senderBundleID", "createdAt", "items",
    ]

    public let schema: String
    public let requestID: String
    public let senderBundleID: String
    public let createdAt: Date
    public let items: [ShareInboxItemV1]

    public init(
        requestID: String,
        senderBundleID: String,
        createdAt: Date,
        items: [ShareInboxItemV1]
    ) {
        schema = Self.schemaID
        self.requestID = requestID
        self.senderBundleID = senderBundleID
        self.createdAt = createdAt
        self.items = items
    }
}

public enum ShareInboxError: Error, Equatable {
    case appGroupUnavailable
    case invalidEnvelope
    case invalidPath
    case itemTooLarge
    case selectionTooLarge
    case tooManyItems
    /// The native side already holds as much unsent shared content as it may.
    /// Nothing is dropped to make room: a request that is already durable
    /// belongs to the person, so the new one is refused instead.
    case inboxFull
    case unavailable
}

/// A staging directory one in-progress Share request writes into.
public struct ShareInboxRequest: Equatable, Sendable {
    public let requestID: String
    public let directory: URL
}

/// The durable, snapshot-only Share spool in the App Group.
///
/// A request is written into `staging/<id>/` and becomes visible to the
/// companion only when that whole directory is renamed to `pending/<id>/` —
/// one atomic step, taken after every byte and the manifest are closed and
/// validated. So a reader never sees a half-written request, and a crash before
/// the rename leaves nothing to adopt.
///
/// Adoption is idempotent because the pending directory *is* the ledger: the
/// companion copies the bytes into its own storage, proves them against the
/// manifest, and only then removes the pending directory. A removal that is
/// lost leaves the request pending, and the next pass re-copies the same bytes
/// under the same request id rather than producing a second candidate.
public final class ShareInboxStore: @unchecked Sendable {
    /// POSIX record locks coordinate processes but are process-owned, so they
    /// do not serialize two callers in the same extension host. Use both.
    private static let processQuotaLock = NSLock()
    public static let directoryName = "ShareInboxV1"
    public static let maximumItems = OwnedSnapshotStore.maximumItems
    public static let maximumItemBytes = OwnedSnapshotStore.maximumItemBytes
    public static let maximumSelectionBytes = OwnedSnapshotStore.maximumSelectionBytes
    /// What the native side may hold for a companion that never opens.
    public static let maximumPendingRequests = 20
    public static let maximumPendingBytes: UInt64 = 1_024 * 1_024 * 1_024
    private static let maximumManifestBytes: UInt64 = 256 * 1_024
    /// Every open writer reserves its worst-case selection plus its manifest.
    /// This keeps the byte quota exact while payloads are copied outside the
    /// cross-process publication lock.
    private static let stagingReservationBytes = maximumSelectionBytes + maximumManifestBytes
    /// A staging directory older than this has no live writer: one bounded
    /// selection cannot take an hour to copy.
    ///
    /// ponytail: an age bound, because an extension host holds no lock an
    /// outside process can see. A lock file would be exact; the bound only has
    /// to be longer than the slowest bounded copy.
    public static let abandonedStagingAge: TimeInterval = 60 * 60

    private let root: URL
    private let staging: URL
    private let pending: URL
    private let chooserRequest: URL
    private let quotaLock: URL
    private let fileManager: FileManager
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    public init(root: URL, fileManager: FileManager = .default) throws {
        self.root = root.standardizedFileURL
        self.fileManager = fileManager
        staging = self.root.appendingPathComponent("staging", isDirectory: true)
        pending = self.root.appendingPathComponent("pending", isDirectory: true)
        chooserRequest = self.root.appendingPathComponent("chooser-request", isDirectory: false)
        quotaLock = self.root.appendingPathComponent("quota.lock", isDirectory: false)
        encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        for directory in [self.root, staging, pending] {
            try fileManager.createDirectory(
                at: directory,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        }
    }

    public static func appGroupInbox(fileManager: FileManager = .default) throws -> ShareInboxStore {
        guard let container = fileManager.containerURL(
            forSecurityApplicationGroupIdentifier: NativeHandoffMailbox.appGroupID
        ) else { throw ShareInboxError.appGroupUnavailable }
        return try ShareInboxStore(
            root: container.appendingPathComponent(directoryName, isDirectory: true),
            fileManager: fileManager
        )
    }

    // MARK: Writing (the extension)

    /// Opens a staging directory for one request, refusing it outright when the
    /// native side is already holding its bound.
    public func beginRequest() throws -> ShareInboxRequest {
        try withQuotaLock {
            try requireRoom(
                additionalRequests: 1,
                additionalReservedBytes: Self.stagingReservationBytes
            )
            let requestID = UUID().uuidString.lowercased()
            let directory = staging.appendingPathComponent(requestID, isDirectory: true)
            do {
                try fileManager.createDirectory(
                    at: directory,
                    withIntermediateDirectories: false,
                    attributes: [.posixPermissions: 0o700]
                )
            } catch {
                throw ShareInboxError.unavailable
            }
            return ShareInboxRequest(requestID: requestID, directory: directory)
        }
    }

    /// An opaque leaf name for one item's bytes. Nothing the provider suggested
    /// reaches the file system here; the suggested name travels as the
    /// manifest's `displayName` instead.
    public static func payloadName(index: Int) -> String {
        "p\(index + 1)-\(UUID().uuidString.lowercased())"
    }

    public func payloadDestination(
        _ request: ShareInboxRequest,
        relativePath: String
    ) throws -> URL {
        guard NativeHandoffMailbox.validNonce(request.requestID),
              OwnedSnapshotStore.isSafeComponent(relativePath)
        else { throw ShareInboxError.invalidPath }
        return request.directory.appendingPathComponent(relativePath, isDirectory: false)
    }

    /// Makes one complete request visible to the companion, atomically.
    ///
    /// The manifest is written and every item's bytes are proven to be a
    /// regular file of exactly the recorded length *before* the one rename that
    /// publishes the request. Nothing partial is ever visible in `pending`.
    public func publish(_ envelope: ShareInboxEnvelopeV1) throws {
        try Self.validate(envelope)
        let directory = staging.appendingPathComponent(envelope.requestID, isDirectory: true)
        try withQuotaLock {
            for item in envelope.items {
                let url = directory.appendingPathComponent(item.relativePath, isDirectory: false)
                var status = stat()
                guard Darwin.lstat(url.path, &status) == 0,
                      status.st_mode & S_IFMT == S_IFREG,
                      UInt64(status.st_size) == item.byteLength,
                      try Self.digest(ofFile: url) == item.digest
                else { throw ShareInboxError.invalidEnvelope }
                try Self.synchronize(url)
            }
            // The current request is already represented by its staging
            // reservation. Serialising this scan with beginRequest prevents a
            // second extension process from passing the same quota snapshot.
            try requireRoom(additionalRequests: 0, additionalReservedBytes: 0)
            let manifest = directory.appendingPathComponent("manifest.json", isDirectory: false)
            do {
                let encoded = try encoder.encode(envelope)
                guard encoded.count <= Self.maximumManifestBytes else {
                    throw ShareInboxError.invalidEnvelope
                }
                try encoded.write(to: manifest, options: [.atomic])
                try fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: manifest.path)
                try Self.synchronize(manifest)
                try Self.synchronize(directory)
            } catch let error as ShareInboxError {
                throw error
            } catch {
                throw ShareInboxError.unavailable
            }
            let destination = pending.appendingPathComponent(envelope.requestID, isDirectory: true)
            guard !fileManager.fileExists(atPath: destination.path),
                  Darwin.rename(directory.path, destination.path) == 0
            else { throw ShareInboxError.unavailable }
            try Self.synchronize(staging)
            try Self.synchronize(pending)
        }
    }

    /// Drops an unpublished request's bytes. Cancelling before the publish
    /// leaves nothing behind; after it, there is nothing here to drop, because
    /// the request now belongs to the person rather than to the share sheet.
    public func abandon(requestID: String) {
        guard NativeHandoffMailbox.validNonce(requestID) else { return }
        try? fileManager.removeItem(
            at: staging.appendingPathComponent(requestID, isDirectory: true)
        )
    }

    // MARK: Reading (the companion)

    /// Every published request, oldest first.
    public func pendingRequestIDs() -> [String] {
        guard let children = try? fileManager.contentsOfDirectory(
            at: pending,
            includingPropertiesForKeys: [.creationDateKey],
            options: [.skipsHiddenFiles]
        ) else { return [] }
        return children
            .filter { NativeHandoffMailbox.validNonce($0.lastPathComponent) }
            .sorted { first, second in
                let left = (try? first.resourceValues(forKeys: [.creationDateKey]))?
                    .creationDate ?? .distantPast
                let right = (try? second.resourceValues(forKeys: [.creationDateKey]))?
                    .creationDate ?? .distantPast
                if left != right { return left < right }
                return first.lastPathComponent < second.lastPathComponent
            }
            .map(\.lastPathComponent)
    }

    public func isPending(requestID: String) -> Bool {
        guard NativeHandoffMailbox.validNonce(requestID) else { return false }
        return fileManager.fileExists(
            atPath: pending.appendingPathComponent(requestID, isDirectory: true).path
        )
    }

    /// One published request this build fully understands.
    ///
    /// `nil` means "not for this build": the manifest is missing, unreadable,
    /// carries a schema or a field this build does not know, or does not
    /// validate. The caller must then leave the whole directory exactly as it
    /// is — byte for byte — so a newer companion can still adopt it.
    ///
    /// ponytail: that also preserves a genuinely corrupt envelope forever.
    /// Preserving one unreadable directory is cheaper than a rule that could
    /// delete a newer build's valid request.
    public func envelope(requestID: String) -> ShareInboxEnvelopeV1? {
        guard NativeHandoffMailbox.validNonce(requestID),
              Self.regularFileSize(manifestURL(requestID)) <= Self.maximumManifestBytes,
              let data = try? Data(contentsOf: manifestURL(requestID)),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys).isSubset(of: ShareInboxEnvelopeV1.encodedFieldNames),
              object["schema"] as? String == ShareInboxEnvelopeV1.schemaID,
              let rawItems = object["items"] as? [[String: Any]],
              rawItems.allSatisfy({ Set($0.keys).isSubset(of: ShareInboxItemV1.encodedFieldNames) }),
              let envelope = try? decoder.decode(ShareInboxEnvelopeV1.self, from: data),
              envelope.requestID == requestID,
              (try? Self.validate(envelope)) != nil
        else { return nil }
        return envelope
    }

    public func payloadURL(requestID: String, relativePath: String) throws -> URL {
        guard NativeHandoffMailbox.validNonce(requestID),
              OwnedSnapshotStore.isSafeComponent(relativePath)
        else { throw ShareInboxError.invalidPath }
        return pending
            .appendingPathComponent(requestID, isDirectory: true)
            .appendingPathComponent(relativePath, isDirectory: false)
    }

    /// Drops one published request. The companion calls this only once it owns
    /// proven copies of every one of the request's bytes.
    public func remove(requestID: String) {
        guard NativeHandoffMailbox.validNonce(requestID) else { return }
        try? fileManager.removeItem(
            at: pending.appendingPathComponent(requestID, isDirectory: true)
        )
    }

    /// Clears staging directories no live request can still be writing. A
    /// published request is never touched here.
    @discardableResult
    public func reconcile(now: Date = Date()) -> Int {
        guard let children = try? fileManager.contentsOfDirectory(
            at: staging,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: []
        ) else { return 0 }
        var removed = 0
        for child in children {
            let modified = (try? child.resourceValues(forKeys: [.contentModificationDateKey]))?
                .contentModificationDate ?? .distantPast
            guard now.timeIntervalSince(modified) >= Self.abandonedStagingAge else { continue }
            if (try? fileManager.removeItem(at: child)) != nil { removed += 1 }
        }
        return removed
    }

    // MARK: The chooser continuation

    /// Asks the companion to open its own reviewed chooser. This carries no
    /// content and binds nothing: it is the truthful continuation for someone
    /// who wants to link an original rather than share a copy of it.
    public func requestChooser() throws {
        do {
            try Data().write(to: chooserRequest, options: [.atomic])
            try fileManager.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: chooserRequest.path
            )
        } catch {
            throw ShareInboxError.unavailable
        }
    }

    /// True once, for one request to open the chooser.
    public func takeChooserRequest() -> Bool {
        guard fileManager.fileExists(atPath: chooserRequest.path) else { return false }
        try? fileManager.removeItem(at: chooserRequest)
        return true
    }

    // MARK: Bounds

    /// Refuses a new request when the native side already holds its bound of
    /// unsent content. Both what is published and what is being staged counts:
    /// the bound is on bytes this companion has not taken ownership of yet.
    ///
    /// Staging requests reserve their maximum possible size because their
    /// providers copy outside the lock. Published requests count their actual
    /// on-disk size, including unknown future-schema bytes.
    private func requireRoom(additionalRequests: Int, additionalReservedBytes: UInt64) throws {
        var count = additionalRequests
        var total = additionalReservedBytes
        let pendingChildren = (try? fileManager.contentsOfDirectory(
            at: pending,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        )) ?? []
        count += pendingChildren.count
        for child in pendingChildren {
            let (next, overflow) = total.addingReportingOverflow(
                Self.bytes(in: child, fileManager: fileManager)
            )
            guard !overflow else { throw ShareInboxError.inboxFull }
            total = next
        }
        let stagingChildren = (try? fileManager.contentsOfDirectory(
            at: staging,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        )) ?? []
        count += stagingChildren.count
        for _ in stagingChildren {
            let (next, overflow) = total.addingReportingOverflow(Self.stagingReservationBytes)
            guard !overflow else { throw ShareInboxError.inboxFull }
            total = next
        }
        guard count <= Self.maximumPendingRequests else { throw ShareInboxError.inboxFull }
        guard total <= Self.maximumPendingBytes else { throw ShareInboxError.inboxFull }
    }

    /// Serialises quota decisions across the Share extension and companion.
    /// An in-process mutex is insufficient because several extension hosts can
    /// publish into the same App Group at once.
    private func withQuotaLock<T>(_ body: () throws -> T) throws -> T {
        try Self.processQuotaLock.withLock {
            let descriptor = Darwin.open(quotaLock.path, O_CREAT | O_RDWR | O_CLOEXEC, 0o600)
            guard descriptor >= 0 else { throw ShareInboxError.unavailable }
            defer { _ = Darwin.close(descriptor) }
            _ = Darwin.fchmod(descriptor, 0o600)
            var writeLock = Darwin.flock()
            writeLock.l_type = Int16(F_WRLCK)
            writeLock.l_whence = Int16(SEEK_SET)
            writeLock.l_start = 0
            writeLock.l_len = 0
            guard Darwin.fcntl(descriptor, F_SETLKW, &writeLock) == 0 else {
                throw ShareInboxError.unavailable
            }
            defer {
                var unlock = Darwin.flock()
                unlock.l_type = Int16(F_UNLCK)
                unlock.l_whence = Int16(SEEK_SET)
                unlock.l_start = 0
                unlock.l_len = 0
                _ = Darwin.fcntl(descriptor, F_SETLK, &unlock)
            }
            return try body()
        }
    }

    /// On-disk size of one request directory, read from the file system rather
    /// than from any manifest, so an envelope this build cannot read still
    /// counts against the bound.
    private static func bytes(in directory: URL, fileManager: FileManager) -> UInt64 {
        guard let enumerator = fileManager.enumerator(
            at: directory,
            includingPropertiesForKeys: [.isRegularFileKey, .fileSizeKey],
            options: [.skipsHiddenFiles]
        ) else { return 0 }
        var total: UInt64 = 0
        for case let child as URL in enumerator {
            guard let values = try? child.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey]),
                  values.isRegularFile == true
            else { continue }
            let size = UInt64(max(0, values.fileSize ?? 0))
            let (next, overflow) = total.addingReportingOverflow(size)
            if overflow { return UInt64.max }
            total = next
        }
        return total
    }

    private static func digest(ofFile url: URL) throws -> String {
        let handle: FileHandle
        do { handle = try FileHandle(forReadingFrom: url) }
        catch { throw ShareInboxError.invalidEnvelope }
        defer { try? handle.close() }
        var hasher = SHA256()
        while true {
            let chunk = try handle.read(upToCount: 1_024 * 1_024) ?? Data()
            if chunk.isEmpty { break }
            hasher.update(data: chunk)
        }
        return "sha256:" + hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    private static func regularFileSize(_ url: URL) -> UInt64 {
        var status = stat()
        guard Darwin.lstat(url.path, &status) == 0,
              status.st_mode & S_IFMT == S_IFREG,
              status.st_size >= 0
        else { return UInt64.max }
        return UInt64(status.st_size)
    }

    /// Flushes a file or directory before the atomic publication boundary.
    private static func synchronize(_ url: URL) throws {
        let descriptor = Darwin.open(url.path, O_RDONLY | O_CLOEXEC)
        guard descriptor >= 0 else { throw ShareInboxError.unavailable }
        defer { _ = Darwin.close(descriptor) }
        guard Darwin.fcntl(descriptor, F_FULLFSYNC) == 0 || Darwin.fsync(descriptor) == 0 else {
            throw ShareInboxError.unavailable
        }
    }

    private func manifestURL(_ requestID: String) -> URL {
        pending
            .appendingPathComponent(requestID, isDirectory: true)
            .appendingPathComponent("manifest.json", isDirectory: false)
    }

    public static func validate(_ envelope: ShareInboxEnvelopeV1) throws {
        guard envelope.schema == ShareInboxEnvelopeV1.schemaID,
              NativeHandoffMailbox.validNonce(envelope.requestID),
              envelope.senderBundleID == NativeHandoffMailbox.shareBundleID,
              !envelope.items.isEmpty,
              envelope.items.count <= maximumItems,
              Set(envelope.items.map(\.slotID)).count == envelope.items.count,
              Set(envelope.items.map(\.relativePath)).count == envelope.items.count
        else { throw ShareInboxError.invalidEnvelope }
        var total: UInt64 = 0
        for item in envelope.items {
            guard NativeHandoffMailbox.validNonce(item.slotID),
                  OwnedSnapshotStore.isSafeComponent(item.displayName),
                  item.displayName.utf8.count <= 255,
                  OwnedSnapshotStore.isSafeComponent(item.relativePath),
                  item.relativePath != "manifest.json",
                  OwnedSnapshotStore.isDigest(item.digest),
                  item.byteLength <= maximumItemBytes
            else { throw ShareInboxError.invalidEnvelope }
            let (next, overflow) = total.addingReportingOverflow(item.byteLength)
            guard !overflow else { throw ShareInboxError.selectionTooLarge }
            total = next
        }
        guard total <= maximumSelectionBytes else { throw ShareInboxError.selectionTooLarge }
    }

    /// `sha256:<hex>` of bytes, in the one spelling every store here uses.
    public static func digest(of data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
