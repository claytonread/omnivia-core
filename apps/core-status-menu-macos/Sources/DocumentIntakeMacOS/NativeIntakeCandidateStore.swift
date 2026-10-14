import Foundation

public enum NativeIntakeCandidateKind: String, Codable, Sendable {
    case originalFile = "original_file"
    /// One selected folder, bound to a companion-owned root grant. The
    /// candidate is the root itself: it is never a direct file, and nothing
    /// inside it is listed or captured until its scope is reviewed.
    case folderRoot = "folder_root"
    case ownedSnapshot = "owned_snapshot"
}

public enum NativeIntakeCandidateState: String, Codable, Sendable {
    /// Reserved before any grant or byte exists. Nothing is acknowledged or shown.
    case needsAccess = "needs_access"
    /// The commit point: the grant exists, or the owned copy is written and its
    /// digest and length are bound. From here resources are retained, never
    /// rolled back, until the candidate is released.
    case adopted
    /// Terminal. The candidate was discarded, published or found orphaned and
    /// its resources are (or are being) cleaned up. Kept as a tombstone so a
    /// replay of the same request cannot adopt it a second time.
    case released
}

/// One companion-owned candidate produced by a slot of an authenticated native
/// handoff request (design §7.2).
///
/// Bounded surface: only the three kinds the Finder and Share handoffs can
/// produce today are represented — `original_file` (a companion-owned
/// persistent read-only grant on one file), `folder_root` (a companion-owned
/// root grant on one selected folder) and `owned_snapshot` (a companion-owned
/// copy of shared bytes). This is not the full §7.2 candidate union. Any other kind,
/// schema or field this code does not fully understand fails decoding or
/// validation and the record is refused as invalid; it is never guessed at.
/// The record names no target, workspace or principal.
///
/// `candidateRevision` is the revision of this candidate's own binding. A fresh
/// reservation is revision 1 and this lane never advances it, so a replay of
/// the same request always maps to the same ordered refs and revisions.
public struct NativeIntakeCandidateV1: Codable, Equatable, Sendable {
    public static let schemaID = "omnivia.macos.intake-candidate.v1"
    fileprivate static let encodedFieldNames: Set<String> = [
        "schema",
        "nativeRequestID",
        "offerDigest",
        "slotID",
        "candidateRef",
        "candidateRevision",
        "kind",
        "nativeAccessLeaseRef",
        "nativeRootRef",
        "nativeRootOwnerRef",
        "nativeRootBindingGeneration",
        "ownedContentRef",
        "ownedContentDigest",
        "ownedContentByteLength",
        "displayName",
        "state",
    ]

    public let schema: String
    public let nativeRequestID: String
    /// SHA-256 of the canonical authenticated offer. It binds the durable
    /// adoption to the complete offer (including bookmark/snapshot claims and
    /// validity fields) after the grant-bearing offer blob is deleted.
    public let offerDigest: String
    public let slotID: String
    public let candidateRef: String
    public let candidateRevision: UInt64
    public let kind: NativeIntakeCandidateKind
    /// `original_file` only: the access reference of the companion's own grant.
    public let nativeAccessLeaseRef: String?
    /// `folder_root` only: the root reference of the companion's own root grant.
    public let nativeRootRef: String?
    /// `folder_root` only: the owner reference this candidate holds that root
    /// grant under. Submitted folder work adopts its own owner, so releasing
    /// this one never drops a grant that work still holds.
    public let nativeRootOwnerRef: String?
    /// `folder_root` only: the root grant's binding generation. Bound at the
    /// adopted commit point, because a rolled-back attempt disconnects the
    /// grant and a replay re-adopts it at the next generation. Review resolves
    /// the folder only at this exact generation.
    public private(set) var nativeRootBindingGeneration: UInt64?
    /// `owned_snapshot` only: the leaf name of the owned copy inside the
    /// request's owned directory.
    public let ownedContentRef: String?
    /// `owned_snapshot` only, `sha256:<hex>` of the owned copy. Bound at the
    /// adopted commit point; an owned copy is reused only when it still matches.
    public private(set) var ownedContentDigest: String?
    /// `owned_snapshot` only: byte length bound together with the digest.
    public private(set) var ownedContentByteLength: UInt64?
    public let displayName: String
    public private(set) var state: NativeIntakeCandidateState

    public init(
        nativeRequestID: String,
        offerDigest: String,
        slotID: String,
        candidateRef: String,
        candidateRevision: UInt64 = 1,
        kind: NativeIntakeCandidateKind,
        nativeAccessLeaseRef: String? = nil,
        nativeRootRef: String? = nil,
        nativeRootOwnerRef: String? = nil,
        nativeRootBindingGeneration: UInt64? = nil,
        ownedContentRef: String? = nil,
        ownedContentDigest: String? = nil,
        ownedContentByteLength: UInt64? = nil,
        displayName: String,
        state: NativeIntakeCandidateState
    ) {
        schema = Self.schemaID
        self.nativeRequestID = nativeRequestID
        self.offerDigest = offerDigest
        self.slotID = slotID
        self.candidateRef = candidateRef
        self.candidateRevision = candidateRevision
        self.kind = kind
        self.nativeAccessLeaseRef = nativeAccessLeaseRef
        self.nativeRootRef = nativeRootRef
        self.nativeRootOwnerRef = nativeRootOwnerRef
        self.nativeRootBindingGeneration = nativeRootBindingGeneration
        self.ownedContentRef = ownedContentRef
        self.ownedContentDigest = ownedContentDigest
        self.ownedContentByteLength = ownedContentByteLength
        self.displayName = displayName
        self.state = state
    }

    public func replacing(
        state: NativeIntakeCandidateState,
        content: (digest: String, byteLength: UInt64)? = nil,
        rootGeneration: UInt64? = nil
    ) -> Self {
        var copy = self
        copy.state = state
        if let content {
            copy.ownedContentDigest = content.digest
            copy.ownedContentByteLength = content.byteLength
        }
        if let rootGeneration { copy.nativeRootBindingGeneration = rootGeneration }
        return copy
    }

    /// Everything that names the candidate and what backs it. It never changes
    /// after the reservation; only the lifecycle state and the content or root
    /// binding advance.
    fileprivate var identity: [String?] {
        [
            schema, nativeRequestID, offerDigest, slotID, candidateRef, String(candidateRevision),
            kind.rawValue, nativeAccessLeaseRef, nativeRootRef, nativeRootOwnerRef,
            ownedContentRef, displayName,
        ]
    }
}

public enum NativeIntakeCandidateStoreError: Error, Equatable {
    /// A record exists for the request but is malformed or inconsistent. Callers
    /// must fail closed: this is never the same as "no adoption".
    case invalidRecord
    /// The record could not be read or written, so its state is unknown. Also
    /// fail closed.
    case unavailable
    /// A write would rewrite a candidate's identity, move its lifecycle
    /// backwards (including resurrecting a released candidate) or change a
    /// bound digest.
    case illegalTransition
}

/// Durable adoption records for authenticated native handoff requests, one
/// file per request nonce holding that request's ordered slot records. This
/// store belongs beside the protected handoff area and never in the App Group:
/// a record names the companion's own grants, so an extension must not read it.
///
/// A record's lifetime is owned by its request and review draft, not by a clock:
/// nothing here expires by age. Released records remain as compact replay
/// tombstones so a request nonce can never be adopted again with new contents.
public final class NativeIntakeCandidateStore: @unchecked Sendable {
    private let root: URL
    private let fileManager: FileManager
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder
    private let lock = NSLock()

    public init(root: URL, fileManager: FileManager = .default) throws {
        self.root = root.standardizedFileURL
        self.fileManager = fileManager
        encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        decoder = JSONDecoder()
        try fileManager.createDirectory(
            at: self.root,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: self.root.path)
    }

    /// The ordered slot records owned by one request nonce. `nil` means exactly
    /// one thing: no record file exists, so the request was never reserved. A
    /// record that exists but cannot be read or trusted throws, and the caller
    /// must not treat that as an absent adoption. The bad file is left in place
    /// so the nonce stays blocked rather than being adopted a second time.
    public func load(nativeRequestID: String) throws -> [NativeIntakeCandidateV1]? {
        try lock.withLock { try loadLocked(nativeRequestID) }
    }

    /// Every request nonce that has a record file, valid or not.
    public func nativeRequestIDs() throws -> [String] {
        let files: [URL]
        do {
            files = try fileManager.contentsOfDirectory(
                at: root,
                includingPropertiesForKeys: nil,
                options: [.skipsHiddenFiles]
            )
        } catch {
            throw NativeIntakeCandidateStoreError.unavailable
        }
        return files
            .filter { $0.pathExtension == "json" }
            .map { ($0.lastPathComponent as NSString).deletingPathExtension }
            .filter(NativeHandoffMailbox.validNonce)
            .sorted()
    }

    /// Reserves a request (every record `needs_access`) or advances an existing
    /// one. The whole request is replaced atomically.
    public func write(_ records: [NativeIntakeCandidateV1]) throws {
        guard let nativeRequestID = records.first?.nativeRequestID else {
            throw NativeIntakeCandidateStoreError.invalidRecord
        }
        try lock.withLock {
            let existing = try loadLocked(nativeRequestID)
            // For an existing request, report an attempted identity/lifecycle
            // rewrite as the transition violation it is before validating the
            // proposed replacement as a standalone record. This keeps the
            // write-once contract observable without weakening validation for
            // a genuinely new reservation.
            try Self.validateTransition(
                from: existing,
                to: records
            )
            try Self.validate(records, nativeRequestID: nativeRequestID)
            try writeLocked(records)
        }
    }

    /// Marks the named candidates released (all of the request's when `nil`).
    /// Releasing is idempotent and a request that has no record is a no-op.
    public func release(
        nativeRequestID: String,
        candidateRefs: Set<String>? = nil
    ) throws {
        try lock.withLock {
            guard let records = try loadLocked(nativeRequestID) else { return }
            let updated = records.map { record -> NativeIntakeCandidateV1 in
                let selected = candidateRefs?.contains(record.candidateRef) ?? true
                return selected ? record.replacing(state: .released) : record
            }
            guard updated != records else { return }
            try writeLocked(updated)
        }
    }

    private func loadLocked(_ nativeRequestID: String) throws -> [NativeIntakeCandidateV1]? {
        guard NativeHandoffMailbox.validNonce(nativeRequestID) else {
            throw NativeIntakeCandidateStoreError.invalidRecord
        }
        let data: Data
        do {
            data = try Data(contentsOf: recordURL(nativeRequestID))
        } catch let error as CocoaError where error.code == .fileReadNoSuchFile {
            return nil
        } catch {
            throw NativeIntakeCandidateStoreError.unavailable
        }
        let records: [NativeIntakeCandidateV1]
        do {
            guard let objects = try JSONSerialization.jsonObject(with: data) as? [[String: Any]],
                  objects.allSatisfy({
                      Set($0.keys).isSubset(of: NativeIntakeCandidateV1.encodedFieldNames)
                  })
            else { throw NativeIntakeCandidateStoreError.invalidRecord }
            records = try decoder.decode([NativeIntakeCandidateV1].self, from: data)
        } catch let error as NativeIntakeCandidateStoreError {
            throw error
        } catch {
            throw NativeIntakeCandidateStoreError.invalidRecord
        }
        try Self.validate(records, nativeRequestID: nativeRequestID)
        return records
    }

    private func writeLocked(_ records: [NativeIntakeCandidateV1]) throws {
        guard let nativeRequestID = records.first?.nativeRequestID else {
            throw NativeIntakeCandidateStoreError.invalidRecord
        }
        let destination = recordURL(nativeRequestID)
        do {
            try encoder.encode(records).write(to: destination, options: [.atomic])
        } catch {
            throw NativeIntakeCandidateStoreError.unavailable
        }
        // Best effort: the directory is already owner-only, and a failure here
        // must not turn a record that was replaced into a reported failure.
        try? fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
    }

    private func recordURL(_ nativeRequestID: String) -> URL {
        root.appendingPathComponent("\(nativeRequestID).json", isDirectory: false)
    }

    private static func validate(
        _ records: [NativeIntakeCandidateV1],
        nativeRequestID: String
    ) throws {
        guard NativeHandoffMailbox.validNonce(nativeRequestID),
              !records.isEmpty,
              records.count <= NativeHandoffMailbox.maximumSlots,
              Set(records.map(\.slotID)).count == records.count,
              Set(records.map(\.candidateRef)).count == records.count,
              // Two slots never share a grant, a root grant, a root owner or an
              // owned copy.
              Set(records.compactMap(\.nativeAccessLeaseRef)).count
              == records.compactMap(\.nativeAccessLeaseRef).count,
              Set(records.compactMap(\.nativeRootRef)).count
              == records.compactMap(\.nativeRootRef).count,
              Set(records.compactMap(\.nativeRootOwnerRef)).count
              == records.compactMap(\.nativeRootOwnerRef).count,
              Set(records.compactMap(\.ownedContentRef)).count
              == records.compactMap(\.ownedContentRef).count,
              validKinds(records),
              records.allSatisfy(validRecord(nativeRequestID: nativeRequestID)),
              records.compactMap(\.ownedContentByteLength).reduce(0, +)
              <= OwnedSnapshotStore.maximumSelectionBytes
        else { throw NativeIntakeCandidateStoreError.invalidRecord }
    }

    /// One Quick Action selection is an ordered mix of files and folders, so
    /// those two kinds share a request. Owned copies come only from Share,
    /// which offers nothing else, so they never share a request with either.
    private static func validKinds(_ records: [NativeIntakeCandidateV1]) -> Bool {
        let kinds = Set(records.map(\.kind))
        return kinds == [.ownedSnapshot] || kinds.isSubset(of: [.originalFile, .folderRoot])
    }

    private static func validRecord(
        nativeRequestID: String
    ) -> (NativeIntakeCandidateV1) -> Bool {
        { record in
            guard record.schema == NativeIntakeCandidateV1.schemaID,
                  record.nativeRequestID == nativeRequestID,
                  OwnedSnapshotStore.isDigest(record.offerDigest),
                  NativeHandoffMailbox.validNonce(record.slotID),
                  !record.candidateRef.isEmpty,
                  record.candidateRef.utf8.count <= 256,
                  record.candidateRevision > 0,
                  !record.displayName.isEmpty,
                  record.displayName.utf8.count <= 255
            else { return false }
            // No kind carries another kind's backing. A root reference on a
            // file candidate, or a grant on a folder candidate, is a record
            // this code does not understand, so it is refused rather than
            // read as the kind it claims.
            let rootFields = [record.nativeRootRef, record.nativeRootOwnerRef]
            switch record.kind {
            case .originalFile:
                return record.nativeAccessLeaseRef.map { !$0.isEmpty && $0.utf8.count <= 128 } == true
                    && rootFields.allSatisfy { $0 == nil }
                    && record.nativeRootBindingGeneration == nil
                    && record.ownedContentRef == nil
                    && record.ownedContentDigest == nil
                    && record.ownedContentByteLength == nil
            case .folderRoot:
                guard record.nativeAccessLeaseRef == nil,
                      record.ownedContentRef == nil,
                      record.ownedContentDigest == nil,
                      record.ownedContentByteLength == nil,
                      rootFields.allSatisfy({ ($0?.utf8.count).map { $0 > 0 && $0 <= 128 } == true }),
                      // The root grant's generation is bound together with the
                      // adopted state: that binding is what lets review prove
                      // it is resolving the same grant this candidate created.
                      record.state != .adopted || record.nativeRootBindingGeneration != nil
                else { return false }
                return (record.nativeRootBindingGeneration ?? 1) > 0
            case .ownedSnapshot:
                guard record.nativeAccessLeaseRef == nil,
                      rootFields.allSatisfy({ $0 == nil }),
                      record.nativeRootBindingGeneration == nil,
                      let name = record.ownedContentRef,
                      OwnedSnapshotStore.isSafeComponent(name),
                      // The digest and the length are bound together, and an
                      // adopted copy must carry them: that binding is what
                      // lets a later reuse prove it is the same bytes.
                      (record.ownedContentDigest == nil) == (record.ownedContentByteLength == nil),
                      record.state != .adopted || record.ownedContentDigest != nil
                else { return false }
                if let digest = record.ownedContentDigest, !OwnedSnapshotStore.isDigest(digest) {
                    return false
                }
                return (record.ownedContentByteLength ?? 0) <= OwnedSnapshotStore.maximumItemBytes
            }
        }
    }

    /// A request is reserved once, advances `needs_access` → `adopted` →
    /// `released` and never moves back. Identity and a bound digest are
    /// write-once, so a stale writer cannot rebind or resurrect a candidate.
    private static func validateTransition(
        from existing: [NativeIntakeCandidateV1]?,
        to records: [NativeIntakeCandidateV1]
    ) throws {
        guard let existing else {
            guard records.allSatisfy({ $0.state == .needsAccess }) else {
                throw NativeIntakeCandidateStoreError.illegalTransition
            }
            return
        }
        guard existing.count == records.count else {
            throw NativeIntakeCandidateStoreError.illegalTransition
        }
        for (old, new) in zip(existing, records) {
            guard old.identity == new.identity else {
                throw NativeIntakeCandidateStoreError.illegalTransition
            }
            switch (old.state, new.state) {
            case (.needsAccess, _), (.adopted, .adopted), (.adopted, .released), (.released, .released):
                break
            default:
                throw NativeIntakeCandidateStoreError.illegalTransition
            }
            if old.ownedContentDigest != nil,
               old.ownedContentDigest != new.ownedContentDigest
                || old.ownedContentByteLength != new.ownedContentByteLength
            {
                throw NativeIntakeCandidateStoreError.illegalTransition
            }
            if old.nativeRootBindingGeneration != nil,
               old.nativeRootBindingGeneration != new.nativeRootBindingGeneration
            {
                throw NativeIntakeCandidateStoreError.illegalTransition
            }
        }
    }
}
