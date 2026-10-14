import Foundation

public enum NativeHandoffSurface: String, Codable, Sendable {
    case finderQuickAction = "finder_quick_action"
    case share
}

/// What one offered slot claims to be. Closed: a kind this build does not
/// know fails decoding, so an offer carrying one is refused whole.
public enum NativeHandoffSourceKind: String, Codable, Sendable {
    /// One selected regular file, to be adopted as a companion-owned
    /// read-only grant on that file.
    case originalBookmark = "original_bookmark"
    /// One selected folder, to be adopted as a companion-owned root grant.
    /// Nothing inside it is read, listed or captured by the handoff; its
    /// scope is reviewed separately and only on the person's request.
    case folderBookmark = "folder_bookmark"
    /// Bytes the sender handed over, to be copied into companion-owned storage.
    case capturedSnapshot = "captured_snapshot"
}

public struct NativeHandoffSlotV1: Codable, Equatable, Sendable {
    public let slotID: String
    public let displayName: String
    public let sourceKind: NativeHandoffSourceKind
    public let bookmark: Data?
    public let snapshotRelativePath: String?

    public init(
        slotID: String,
        displayName: String,
        sourceKind: NativeHandoffSourceKind,
        bookmark: Data? = nil,
        snapshotRelativePath: String? = nil
    ) {
        self.slotID = slotID
        self.displayName = displayName
        self.sourceKind = sourceKind
        self.bookmark = bookmark
        self.snapshotRelativePath = snapshotRelativePath
    }
}

public struct NativeHandoffOfferV1: Codable, Equatable, Sendable {
    public let schema: String
    public let requestNonce: String
    public let expectedOwnerBundleID: String
    public let senderBundleID: String
    public let surface: NativeHandoffSurface
    public let createdAt: Date
    public let expiresAt: Date
    public let cancelled: Bool
    public let slots: [NativeHandoffSlotV1]

    public init(
        requestNonce: String,
        expectedOwnerBundleID: String,
        senderBundleID: String,
        surface: NativeHandoffSurface,
        createdAt: Date,
        expiresAt: Date,
        cancelled: Bool = false,
        slots: [NativeHandoffSlotV1]
    ) {
        schema = "omnivia.native-handoff.offer.v1"
        self.requestNonce = requestNonce
        self.expectedOwnerBundleID = expectedOwnerBundleID
        self.senderBundleID = senderBundleID
        self.surface = surface
        self.createdAt = createdAt
        self.expiresAt = expiresAt
        self.cancelled = cancelled
        self.slots = slots
    }
}

public struct NativeHandoffAcknowledgementV1: Codable, Equatable, Sendable {
    public struct Adoption: Codable, Equatable, Sendable {
        public let slotID: String
        public let candidateRef: String
        public let candidateRevision: UInt64

        public init(slotID: String, candidateRef: String, candidateRevision: UInt64) {
            self.slotID = slotID
            self.candidateRef = candidateRef
            self.candidateRevision = candidateRevision
        }
    }

    public let schema: String
    public let requestNonce: String
    public let acknowledgedAt: Date
    public let adoptions: [Adoption]

    public init(requestNonce: String, acknowledgedAt: Date, adoptions: [Adoption]) {
        schema = "omnivia.native-handoff.ack.v1"
        self.requestNonce = requestNonce
        self.acknowledgedAt = acknowledgedAt
        self.adoptions = adoptions
    }
}

public enum NativeHandoffMailboxError: Error, Equatable {
    case appGroupUnavailable
    case invalidOffer
    case invalidAcknowledgement
    case invalidPath
    case unavailable
}

public final class NativeHandoffMailbox: @unchecked Sendable {
    public static let appGroupID = "group.com.omnivia.core"
    public static let companionBundleID = "com.omnivia.core.status"
    public static let finderBundleID = "com.omnivia.core.finder-action"
    public static let shareBundleID = "com.omnivia.core.share"
    public static let finderBundleName = "Add to OmniVia Core.appex"
    public static let shareBundleName = "Share to OmniVia Core.appex"
    public static let maximumSlots = 10
    public static let maximumLifetime: TimeInterval = 120

    private let root: URL
    private let offers: URL
    private let acknowledgements: URL
    private let snapshots: URL
    public let peerSocketURL: URL
    private let fileManager: FileManager
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    public init(root: URL, fileManager: FileManager = .default) throws {
        self.root = root.standardizedFileURL
        self.fileManager = fileManager
        offers = self.root.appendingPathComponent("offers", isDirectory: true)
        acknowledgements = self.root.appendingPathComponent("acknowledgements", isDirectory: true)
        snapshots = self.root.appendingPathComponent("snapshots", isDirectory: true)
        peerSocketURL = self.root.appendingPathComponent("h.sock", isDirectory: false)
        encoder = JSONEncoder()
        decoder = JSONDecoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys]
        decoder.dateDecodingStrategy = .iso8601
        for directory in [self.root, offers, acknowledgements, snapshots] {
            try fileManager.createDirectory(
                at: directory,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        }
    }

    public static func appGroupMailbox(fileManager: FileManager = .default) throws -> NativeHandoffMailbox {
        guard let container = fileManager.containerURL(
            forSecurityApplicationGroupIdentifier: appGroupID
        ) else { throw NativeHandoffMailboxError.appGroupUnavailable }
        return try NativeHandoffMailbox(
            root: container.appendingPathComponent("NativeHandoffV1", isDirectory: true),
            fileManager: fileManager
        )
    }

    /// Persists an offer only after the authenticated peer service has admitted
    /// its kernel-identified, code-signature-verified connection. Callers must
    /// keep this store outside the App Group.
    public func storeAuthenticatedOffer(
        _ offer: NativeHandoffOfferV1,
        now: Date = Date()
    ) throws {
        try Self.validate(offer, now: now)
        let destination = offerURL(nonce: offer.requestNonce)
        if fileManager.fileExists(atPath: destination.path) {
            guard let existing = try? Data(contentsOf: destination),
                  try decoder.decode(NativeHandoffOfferV1.self, from: existing) == offer
            else { throw NativeHandoffMailboxError.invalidOffer }
            return
        }
        try write(try encoder.encode(offer), to: destination)
    }

    public func encodedOffer(_ offer: NativeHandoffOfferV1, now: Date = Date()) throws -> Data {
        try Self.validate(offer, now: now)
        return try encoder.encode(offer)
    }

    public func decodeOffer(_ data: Data, now: Date = Date()) throws -> NativeHandoffOfferV1 {
        guard data.count <= 4 * 1_024 * 1_024 else { throw NativeHandoffMailboxError.invalidOffer }
        let offer = try decoder.decode(NativeHandoffOfferV1.self, from: data)
        try Self.validate(offer, now: now)
        return offer
    }

    public func pendingOffers(now: Date = Date()) -> [NativeHandoffOfferV1] {
        guard let files = try? fileManager.contentsOfDirectory(
            at: offers,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ) else { return [] }
        var pending: [NativeHandoffOfferV1] = []
        for file in files where file.pathExtension == "json" {
            guard let data = try? Data(contentsOf: file),
                  let offer = try? decoder.decode(NativeHandoffOfferV1.self, from: data)
            else {
                try? fileManager.removeItem(at: file)
                continue
            }
            guard (try? Self.validate(offer, now: now)) != nil else {
                removeOffer(nonce: offer.requestNonce, removeSnapshots: true)
                continue
            }
            pending.append(offer)
        }
        return pending.sorted { $0.createdAt < $1.createdAt }
    }

    /// The stored offer for one request while it can still be adopted. Unlike
    /// `pendingOffers`, this removes nothing, so it can answer "could this
    /// request still be replayed?" without side effects.
    public func pendingOffer(nonce: String, now: Date = Date()) -> NativeHandoffOfferV1? {
        guard Self.validNonce(nonce),
              let data = try? Data(contentsOf: offerURL(nonce: nonce)),
              let offer = try? decoder.decode(NativeHandoffOfferV1.self, from: data),
              offer.requestNonce == nonce,
              (try? Self.validate(offer, now: now)) != nil
        else { return nil }
        return offer
    }

    public func writeAcknowledgement(_ acknowledgement: NativeHandoffAcknowledgementV1) throws {
        try validateAcknowledgement(acknowledgement)
        try write(
            try encoder.encode(acknowledgement),
            to: acknowledgementURL(nonce: acknowledgement.requestNonce)
        )
    }

    public func acknowledgement(nonce: String) -> NativeHandoffAcknowledgementV1? {
        guard Self.validNonce(nonce),
              let data = try? Data(contentsOf: acknowledgementURL(nonce: nonce)),
              let value = try? decoder.decode(NativeHandoffAcknowledgementV1.self, from: data),
              value.requestNonce == nonce,
              (try? validateAcknowledgement(value)) != nil
        else { return nil }
        return value
    }

    public func encodedAcknowledgement(
        _ acknowledgement: NativeHandoffAcknowledgementV1
    ) throws -> Data {
        try validateAcknowledgement(acknowledgement)
        return try encoder.encode(acknowledgement)
    }

    public func decodeAcknowledgement(
        _ data: Data,
        expectedNonce: String
    ) throws -> NativeHandoffAcknowledgementV1 {
        guard data.count <= 256 * 1_024 else {
            throw NativeHandoffMailboxError.invalidAcknowledgement
        }
        let value = try decoder.decode(NativeHandoffAcknowledgementV1.self, from: data)
        try validateAcknowledgement(value)
        guard value.requestNonce == expectedNonce else {
            throw NativeHandoffMailboxError.invalidAcknowledgement
        }
        return value
    }

    public func snapshotURL(nonce: String, relativePath: String) throws -> URL {
        guard Self.validNonce(nonce),
              !relativePath.isEmpty,
              relativePath == (relativePath as NSString).lastPathComponent,
              !relativePath.contains("\0")
        else { throw NativeHandoffMailboxError.invalidPath }
        let directory = snapshots.appendingPathComponent(nonce, isDirectory: true)
        let candidate = directory.appendingPathComponent(relativePath, isDirectory: false)
            .standardizedFileURL
        guard candidate.deletingLastPathComponent() == directory.standardizedFileURL else {
            throw NativeHandoffMailboxError.invalidPath
        }
        return candidate
    }

    public func prepareSnapshotDirectory(nonce: String) throws -> URL {
        guard Self.validNonce(nonce) else { throw NativeHandoffMailboxError.invalidPath }
        let directory = snapshots.appendingPathComponent(nonce, isDirectory: true)
        try fileManager.createDirectory(
            at: directory,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )
        return directory
    }

    public func removeOffer(nonce: String, removeSnapshots: Bool) {
        guard Self.validNonce(nonce) else { return }
        try? fileManager.removeItem(at: offerURL(nonce: nonce))
        if removeSnapshots {
            try? fileManager.removeItem(at: snapshots.appendingPathComponent(nonce, isDirectory: true))
        }
    }

    public func removeSnapshots(nonce: String) {
        guard Self.validNonce(nonce) else { return }
        try? fileManager.removeItem(at: snapshots.appendingPathComponent(nonce, isDirectory: true))
    }

    public func removeAcknowledgement(nonce: String) {
        guard Self.validNonce(nonce) else { return }
        try? fileManager.removeItem(at: acknowledgementURL(nonce: nonce))
    }

    private func write(_ data: Data, to destination: URL) throws {
        do {
            try data.write(to: destination, options: [.atomic])
            try fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
        } catch {
            throw NativeHandoffMailboxError.unavailable
        }
    }

    private func offerURL(nonce: String) -> URL {
        offers.appendingPathComponent("\(nonce).json", isDirectory: false)
    }

    private func acknowledgementURL(nonce: String) -> URL {
        acknowledgements.appendingPathComponent("\(nonce).json", isDirectory: false)
    }

    public static func validate(_ offer: NativeHandoffOfferV1, now: Date = Date()) throws {
        let validSender = (offer.surface == .finderQuickAction && offer.senderBundleID == finderBundleID)
            || (offer.surface == .share && offer.senderBundleID == shareBundleID)
        guard offer.schema == "omnivia.native-handoff.offer.v1",
              validNonce(offer.requestNonce),
              offer.expectedOwnerBundleID == companionBundleID,
              validSender,
              !offer.cancelled,
              offer.expiresAt > now,
              offer.expiresAt.timeIntervalSince(offer.createdAt) > 0,
              offer.expiresAt.timeIntervalSince(offer.createdAt) <= maximumLifetime,
              !offer.slots.isEmpty,
              offer.slots.count <= maximumSlots,
              Set(offer.slots.map(\.slotID)).count == offer.slots.count
        else { throw NativeHandoffMailboxError.invalidOffer }
        for slot in offer.slots {
            guard validNonce(slot.slotID),
                  !slot.displayName.isEmpty,
                  slot.displayName.utf8.count <= 255
            else { throw NativeHandoffMailboxError.invalidOffer }
            switch slot.sourceKind {
            // A file and a folder claim are carried the same way — one
            // bookmark, no transfer blob — and only the Quick Action may make
            // either. What the bookmark actually resolves to is checked by the
            // owner that adopts it, never guessed from the claim.
            case .originalBookmark, .folderBookmark:
                guard offer.surface == .finderQuickAction,
                      let bookmark = slot.bookmark,
                      !bookmark.isEmpty,
                      slot.snapshotRelativePath == nil
                else { throw NativeHandoffMailboxError.invalidOffer }
            case .capturedSnapshot:
                guard offer.surface == .share,
                      slot.bookmark == nil,
                      let path = slot.snapshotRelativePath,
                      path == (path as NSString).lastPathComponent,
                      !path.isEmpty
                else { throw NativeHandoffMailboxError.invalidOffer }
            }
        }
    }

    private func validateAcknowledgement(
        _ acknowledgement: NativeHandoffAcknowledgementV1
    ) throws {
        guard acknowledgement.schema == "omnivia.native-handoff.ack.v1",
              Self.validNonce(acknowledgement.requestNonce),
              !acknowledgement.adoptions.isEmpty,
              acknowledgement.adoptions.count <= Self.maximumSlots,
              Set(acknowledgement.adoptions.map(\.slotID)).count == acknowledgement.adoptions.count,
              acknowledgement.adoptions.allSatisfy({ adoption in
                  Self.validNonce(adoption.slotID)
                      && !adoption.candidateRef.isEmpty
                      && adoption.candidateRef.utf8.count <= 256
                      && adoption.candidateRevision > 0
              })
        else { throw NativeHandoffMailboxError.invalidAcknowledgement }
    }

    /// True when the companion acknowledged adopting exactly the slots that
    /// were offered, in the order they were offered. An extension completes its
    /// request only on this: a partial, reordered, padded or empty
    /// acknowledgement is not adoption, so the extension keeps its items and
    /// stays open for a retry instead of reporting success.
    public static func adoptionSatisfiesOffer(
        offeredSlotIDs: [String],
        acknowledgedSlotIDs: [String]
    ) -> Bool {
        !offeredSlotIDs.isEmpty
            && offeredSlotIDs.count <= maximumSlots
            && Set(offeredSlotIDs).count == offeredSlotIDs.count
            && acknowledgedSlotIDs == offeredSlotIDs
    }

    static func validNonce(_ value: String) -> Bool {
        UUID(uuidString: value) != nil && value.count == 36
    }
}
