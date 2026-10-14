import Darwin
import Foundation

public enum NativeAccessError: Error, Equatable {
    case invalidIdentifier
    case invalidRoute
    case duplicateConnectedAccess
    case unknownAccess
    case disconnected
    case generationMismatch
    case staleBookmark
    case accessDenied
    case memberOutsideRoot
    case stateUnavailable
    case invalidState
    /// The root group's membership is frozen and this member is not in it. A
    /// sibling that appeared later needs a new explicitly reviewed scope.
    case membershipFrozen
    /// This root group's authority does not permit discovering members.
    case discoveryNotPermitted
    /// Another owner tried to join a root group under a different destination,
    /// workspace or caller than the one it was approved for.
    case bindingMismatch
    /// A second, different establishment was presented under the identity of one
    /// already recorded. Only a byte-identical replay is a retry.
    case conflictingEstablishment
}

/// What a retained root group is allowed to do, mirroring the design's
/// `NativeRootAccessGroupV1.authority`.
public enum NativeRootAuthority: String, Codable, Sendable {
    /// A reviewed one-off plan: its frozen members may be enrolled and read.
    case importPlan = "import_plan"
    /// A watched folder: discovery may enrol newly qualifying descendants.
    case watchAndMembers = "watch_and_members"
    /// The plan finished. Manual actions on already enrolled members still
    /// work; nothing new can be discovered or added through this group.
    case frozenMembersOnly = "frozen_members_only"
}

/// Why the native owner is resolving a root. Discovery is the only purpose
/// that may enumerate descendants, and it needs standing watch authority.
public enum NativeRootAccessPurpose: Sendable {
    /// The user's own bounded scope preview, or resolving an enrolled member.
    case scopePreview
    /// Enumerating the hierarchy to find members to capture.
    case memberDiscovery
}

/// The destination, workspace and caller one root group was approved for.
/// `clientContextRef` is this companion on this Mac (the design's `deviceRef`)
/// and `principalRef` is its owner identity, so no second identity is invented.
public struct NativeRootAccessBinding: Codable, Equatable, Sendable {
    public let targetRef: String
    public let workspaceRef: String
    public let targetBindingRevision: String
    public let callerContextBinding: CallerContextBinding

    public init(
        targetRef: String,
        workspaceRef: String,
        targetBindingRevision: String,
        callerContextBinding: CallerContextBinding
    ) {
        self.targetRef = targetRef
        self.workspaceRef = workspaceRef
        self.targetBindingRevision = targetBindingRevision
        self.callerContextBinding = callerContextBinding
    }

    public init(_ binding: CaptureDeliveryBinding) {
        self.init(
            targetRef: binding.targetRef,
            workspaceRef: binding.workspaceRef,
            targetBindingRevision: binding.targetBindingRevision,
            callerContextBinding: binding.callerContextBinding
        )
    }
}

public enum NativeAccessState: String, Codable, Sendable {
    case connected
    case disconnected
}

/// One reviewed member of a pending folder establishment. Every identity the
/// durable run or subscription will carry is frozen here, before any authority
/// over the member exists, so a retry reuses them instead of minting
/// replacements. It holds the root-relative locator only: the bookmark and the
/// absolute route stay in the root group.
public struct PendingFolderMember: Codable, Equatable, Sendable {
    public let memberID: String
    public let accessRef: String
    public let linkedDocumentID: String
    public let requestedSourceIdentity: String
    public let relativeComponents: [String]
    public let displayName: String
    public let contentKind: CaptureContentKind
    public let itemID: String
    public let payloadRef: String
    public let operationKey: String
    public let declaredByteLength: UInt64
}

/// One approved folder submission whose durable run or subscription does not
/// exist yet.
///
/// This is written in the same durable write as the root grant it authorises, so
/// the first authority to exist on disk is already owned by a complete reviewed
/// intent: a crash or a failure anywhere in establishment leaves a root or
/// member grant that names exactly which plan, destination, policy and consent
/// it belongs to, rather than an orphan.
public struct PendingFolderSubmission: Codable, Equatable, Sendable {
    public let submissionID: String
    public let rootRef: String
    public let rootDisplayName: String
    /// Nil when the plan imports nothing, which is a watch of new content only.
    public let runID: String?
    public let manifestRevision: String
    /// Nil for a reviewed one-off import, which establishes no watch.
    public let subscriptionID: String?
    public let observationEpoch: String
    public let binding: NativeRootAccessBinding
    public let authority: NativeRootAuthority
    public let mode: FolderCaptureMode
    public let recursive: Bool
    public let includeHidden: Bool
    public let watchInitialContent: FolderWatchInitialContent
    public let approvedFormatPolicyRef: String
    public let exclusionsPolicyRef: String
    public let configGeneration: UInt64
    public let consentGeneration: UInt64
    public let createdAt: String
    public let members: [PendingFolderMember]

    /// The durable owners this intent's root grant is adopted for, and the only
    /// owners it ever gains.
    public var ownerRefs: [String] { [subscriptionID, runID].compactMap { $0 }.sorted() }
}

/// A retained root group's own lifecycle, which is not a file grant's two
/// states: a root is prepared before it is established, and a root whose
/// access broke is repairable rather than gone.
public enum NativeRootAccessState: String, Codable, Sendable {
    /// Bound to a destination and readable for the scope it was selected for,
    /// but no durable run or subscription owns it yet. An interrupted
    /// establishment leaves exactly this, so recovery can tell it apart from a
    /// grant that real work holds.
    case provisional
    /// A durable run or subscription intent is established on this root.
    case active
    /// The bookmark is stale or its access was revoked. The route is kept, so
    /// re-granting the same root repairs it in place.
    case needsAccess = "needs_access"
    /// The provider or root is not reachable right now — an unmounted volume,
    /// a signed-out provider. Nothing is wrong with the grant itself.
    case unavailable
    /// Explicitly released or disconnected with no valid route left. This is
    /// the only state that drops the bookmark.
    case disconnected

    /// Every state but `disconnected` still holds the retained bookmark, which
    /// is what keeps a repairable state repairable.
    public var retainsRoute: Bool { self != .disconnected }

    /// Only a prepared or established root resolves. A repairable state refuses
    /// reads without losing what it is bound to.
    public var permitsResolution: Bool { self == .provisional || self == .active }
}

public enum NativeAccessRoute: Codable, Equatable, Sendable {
    case directFile
    case rootMember(rootRef: String, memberRef: String, relativeComponents: [String])

    private enum CodingKeys: String, CodingKey {
        case kind
        case rootRef
        case memberRef
        case relativeComponents
    }

    private enum Kind: String, Codable {
        case directFile = "direct_file"
        case rootMember = "root_member"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        switch try container.decode(Kind.self, forKey: .kind) {
        case .directFile:
            self = .directFile
        case .rootMember:
            self = .rootMember(
                rootRef: try container.decode(String.self, forKey: .rootRef),
                memberRef: try container.decode(String.self, forKey: .memberRef),
                relativeComponents: try container.decode([String].self, forKey: .relativeComponents)
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .directFile:
            try container.encode(Kind.directFile, forKey: .kind)
        case let .rootMember(rootRef, memberRef, relativeComponents):
            try container.encode(Kind.rootMember, forKey: .kind)
            try container.encode(rootRef, forKey: .rootRef)
            try container.encode(memberRef, forKey: .memberRef)
            try container.encode(relativeComponents, forKey: .relativeComponents)
        }
    }
}

public struct NativeAccessDescriptor: Equatable, Sendable {
    public let accessRef: String
    public let linkedDocumentID: String
    public let generation: UInt64
    public let rootBindingGeneration: UInt64?
    public let state: NativeAccessState
    public let route: NativeAccessRoute
}

/// The public projection of the durable root group. Bookmark bytes and
/// absolute paths never appear here.
public struct NativeRootAccessDescriptor: Equatable, Sendable {
    public let rootRef: String
    public let generation: UInt64
    public let state: NativeRootAccessState
    public let ownerRefs: [String]
    public let authority: NativeRootAuthority
    /// The enrolled member set. Frozen for a reviewed one-off plan.
    public let memberRefs: [String]
    public let membersFrozen: Bool
    /// Nil only for a record written before this schema recorded one.
    public let binding: NativeRootAccessBinding?
}

public struct NativeAccessExpectation: Equatable, Sendable {
    public let accessRef: String
    public let generation: UInt64
    public let rootBindingGeneration: UInt64?

    public init(
        accessRef: String,
        generation: UInt64,
        rootBindingGeneration: UInt64? = nil
    ) {
        self.accessRef = accessRef
        self.generation = generation
        self.rootBindingGeneration = rootBindingGeneration
    }
}

public protocol SecurityScopedBookmarkCoding: Sendable {
    func makeReadOnlyBookmark(for url: URL) throws -> Data
    func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool)
    func startAccessing(_ url: URL) -> Bool
    func stopAccessing(_ url: URL)
}

public struct SystemSecurityScopedBookmarkCodec: SecurityScopedBookmarkCoding {
    public init() {}

    public func makeReadOnlyBookmark(for url: URL) throws -> Data {
        try url.bookmarkData(
            options: [.withSecurityScope, .securityScopeAllowOnlyReadAccess],
            includingResourceValuesForKeys: [.isRegularFileKey, .isDirectoryKey],
            relativeTo: nil
        )
    }

    public func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
        var stale = false
        let url = try URL(
            resolvingBookmarkData: data,
            options: [.withSecurityScope, .withoutUI],
            relativeTo: nil,
            bookmarkDataIsStale: &stale
        )
        return (url, stale)
    }

    public func startAccessing(_ url: URL) -> Bool {
        url.startAccessingSecurityScopedResource()
    }

    public func stopAccessing(_ url: URL) {
        url.stopAccessingSecurityScopedResource()
    }
}

private struct NativeAccessRecord: Codable, Equatable {
    let accessRef: String
    let linkedDocumentID: String
    let generation: UInt64
    let rootBindingGeneration: UInt64?
    let state: NativeAccessState
    let route: NativeAccessRoute
    let bookmark: Data?
}

private struct NativeRootAccessRecord: Codable, Equatable {
    let rootRef: String
    let generation: UInt64
    let state: NativeRootAccessState
    let ownerRefs: [String]
    let bookmark: Data?
    let authority: NativeRootAuthority
    let memberRefs: [String]
    let membersFrozen: Bool
    let binding: NativeRootAccessBinding?

    func with(
        generation: UInt64? = nil,
        state: NativeRootAccessState? = nil,
        ownerRefs: [String]? = nil,
        bookmark: Data?? = nil,
        authority: NativeRootAuthority? = nil,
        memberRefs: [String]? = nil,
        membersFrozen: Bool? = nil
    ) -> NativeRootAccessRecord {
        NativeRootAccessRecord(
            rootRef: rootRef,
            generation: generation ?? self.generation,
            state: state ?? self.state,
            ownerRefs: ownerRefs ?? self.ownerRefs,
            bookmark: bookmark ?? self.bookmark,
            authority: authority ?? self.authority,
            memberRefs: memberRefs ?? self.memberRefs,
            membersFrozen: membersFrozen ?? self.membersFrozen,
            binding: binding
        )
    }
}

private struct NativeAccessDocument: Codable {
    let schema: String
    let records: [NativeAccessRecord]
    let roots: [NativeRootAccessRecord]
    /// Optional because a v3 file written before establishment was durable has
    /// no such key. An absent key is an empty set, which is what that file
    /// meant; nothing else about v3 changed, so the schema name has not.
    let pending: [PendingFolderSubmission]?
}

private struct LegacyNativeAccessRecord: Codable {
    let accessRef: String
    let linkedDocumentID: String
    let generation: UInt64
    let state: NativeAccessState
    let route: NativeAccessRoute
    let bookmark: Data?
}

private struct LegacyNativeAccessDocument: Codable {
    let schema: String
    let records: [LegacyNativeAccessRecord]
}

private struct NativeAccessSchemaEnvelope: Codable {
    let schema: String
}

/// The sole native owner of persistent original-file grants.
///
/// Bookmarks and relative member routes stay in this owner-only store. Public descriptors contain opaque
/// references only. Reads are generation checked, balanced, and constrained to the selected root.
public final class NativeAccessStore: @unchecked Sendable {
    private static let schema = "omnivia.native-access.v3"
    private static let identifierCharacters = CharacterSet(
        charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"
    )

    /// Shared with the other publication owners; see `PublicationGate`.
    private let gate: PublicationGate
    private let directory: URL
    private let stateFile: URL
    private let codec: any SecurityScopedBookmarkCoding
    private var records: [String: NativeAccessRecord]
    private var roots: [String: NativeRootAccessRecord]
    private var pending: [String: PendingFolderSubmission]

    public init(
        stateDirectory: URL,
        codec: any SecurityScopedBookmarkCoding = SystemSecurityScopedBookmarkCodec(),
        gate: PublicationGate = .processShared
    ) throws {
        self.gate = gate
        self.directory = stateDirectory
        self.stateFile = stateDirectory.appendingPathComponent("native-access-v1.json", isDirectory: false)
        self.codec = codec
        self.records = [:]
        self.roots = [:]
        self.pending = [:]
        try Self.prepareDirectory(stateDirectory)
        let loaded = try Self.load(from: stateFile, codec: codec)
        self.records = loaded.records
        self.roots = loaded.roots
        self.pending = loaded.pending
        if loaded.migrated {
            try persist(records: loaded.records, roots: loaded.roots)
        }
    }

    public func descriptors() -> [NativeAccessDescriptor] {
        gate.withLock {
            records.values.sorted { $0.accessRef < $1.accessRef }.map(Self.descriptor)
        }
    }

    public func rootDescriptors() -> [NativeRootAccessDescriptor] {
        gate.withLock {
            roots.values.sorted { $0.rootRef < $1.rootRef }.map(Self.rootDescriptor)
        }
    }

    @discardableResult
    public func adopt(
        accessRef: String,
        linkedDocumentID: String,
        selectedURL: URL,
        route: NativeAccessRoute
    ) throws -> NativeAccessDescriptor {
        try Self.validateIdentifier(accessRef)
        try Self.validateIdentifier(linkedDocumentID)
        try Self.validate(route)
        guard route == .directFile else { throw NativeAccessError.invalidRoute }
        try Self.validateSelection(selectedURL, for: route)
        let bookmark: Data
        do {
            bookmark = try codec.makeReadOnlyBookmark(for: selectedURL)
        } catch {
            throw NativeAccessError.accessDenied
        }
        return try gate.withLock {
            let existing = records[accessRef]
            if existing?.state == .connected {
                throw NativeAccessError.duplicateConnectedAccess
            }
            if let existing, existing.linkedDocumentID != linkedDocumentID || existing.route != route {
                throw NativeAccessError.invalidRoute
            }
            let generation = (existing?.generation ?? 0) + 1
            let record = NativeAccessRecord(
                accessRef: accessRef,
                linkedDocumentID: linkedDocumentID,
                generation: generation,
                rootBindingGeneration: nil,
                state: .connected,
                route: route,
                bookmark: bookmark
            )
            var replacement = records
            replacement[accessRef] = record
            try persist(records: replacement, roots: roots)
            records = replacement
            return Self.descriptor(record)
        }
    }

    /// Every folder establishment this owner has recorded and not yet retired.
    public func pendingFolderEstablishments() -> [PendingFolderSubmission] {
        gate.withLock { pending.values.sorted { $0.submissionID < $1.submissionID } }
    }

    /// Records one approved folder establishment and the root grant it
    /// authorises in a single durable write.
    ///
    /// This is the only way a folder root grant comes into existence, so no root
    /// or member authority can precede the plan that owns it. The grant is left
    /// `provisional` — bound to its destination, readable for its reviewed
    /// scope, but unable to discover anything — until `activateRoot` promotes it
    /// once the matching run or subscription record exists.
    ///
    /// Repeating it with a byte-identical intent is the recovery path and
    /// changes nothing. A different intent under the same submission identity,
    /// or the same intent against a root bound elsewhere, is refused.
    /// `selectedURL` is required only for the first call; a replay resolves the
    /// root through the grant that call already retained.
    @discardableResult
    public func beginFolderEstablishment(
        _ intent: PendingFolderSubmission,
        selectedURL: URL?
    ) throws -> NativeRootAccessDescriptor {
        try Self.validate(intent)
        // Selection checks and bookmark creation read the file system, so they
        // happen before the gate is taken, never under it.
        let bookmark: Data? = try selectedURL.map { url in
            try Self.validateSelection(
                url,
                for: .rootMember(
                    rootRef: intent.rootRef,
                    memberRef: "member",
                    relativeComponents: ["member"]
                )
            )
            do {
                return try codec.makeReadOnlyBookmark(for: url)
            } catch {
                throw NativeAccessError.accessDenied
            }
        }
        return try gate.withLock {
            if let recorded = pending[intent.submissionID] {
                guard recorded == intent else {
                    throw NativeAccessError.conflictingEstablishment
                }
                guard let root = roots[intent.rootRef] else {
                    throw NativeAccessError.invalidState
                }
                guard root.state.permitsResolution else { throw Self.refusal(root.state) }
                return Self.rootDescriptor(root)
            }
            if let root = roots[intent.rootRef] {
                // The intent was already retired: its root is established work,
                // and replaying it must not adopt a second grant over it.
                guard root.binding == intent.binding else {
                    throw NativeAccessError.conflictingEstablishment
                }
                guard root.state.permitsResolution else { throw Self.refusal(root.state) }
                return Self.rootDescriptor(root)
            }
            guard let bookmark else { throw NativeAccessError.unknownAccess }
            let record = NativeRootAccessRecord(
                rootRef: intent.rootRef,
                generation: 1,
                state: .provisional,
                ownerRefs: intent.ownerRefs,
                bookmark: bookmark,
                authority: intent.authority,
                memberRefs: [],
                membersFrozen: false,
                binding: intent.binding
            )
            var replacementRoots = roots
            replacementRoots[intent.rootRef] = record
            var replacementPending = pending
            replacementPending[intent.submissionID] = intent
            try persist(records: records, roots: replacementRoots, pending: replacementPending)
            roots = replacementRoots
            pending = replacementPending
            return Self.rootDescriptor(record)
        }
    }

    /// Retires one establishment, which its owner calls once the root is
    /// established and the durable run or subscription exists. Repeating it does
    /// nothing, so a completion whose acknowledgement was lost is recovered
    /// rather than repeated.
    public func completeFolderEstablishment(submissionID: String) throws {
        try Self.validateIdentifier(submissionID)
        try gate.withLock {
            guard pending[submissionID] != nil else { return }
            var replacement = pending
            replacement[submissionID] = nil
            try persist(records: records, roots: roots, pending: replacement)
            pending = replacement
        }
    }

    /// Adopts one selected folder grant and retains it for a durable owner such
    /// as a bulk run or subscription. Repeating adoption for another owner of
    /// the same root reuses the single bookmark and binding generation, and only
    /// an owner presenting the same approved destination/workspace/caller may
    /// join it.
    ///
    /// `authority` is the group's application scope, which is narrower than the
    /// operating system's recursive folder access: the broker enforces it here.
    @discardableResult
    public func adoptRoot(
        rootRef: String,
        ownerRef: String,
        selectedURL: URL,
        binding: NativeRootAccessBinding,
        authority: NativeRootAuthority = .importPlan
    ) throws -> NativeRootAccessDescriptor {
        try Self.validateIdentifier(rootRef)
        try Self.validateIdentifier(ownerRef)
        try Self.validateSelection(
            selectedURL,
            for: .rootMember(rootRef: rootRef, memberRef: "member", relativeComponents: ["member"])
        )
        return try gate.withLock {
            if let current = roots[rootRef], current.state.retainsRoute {
                if let recorded = current.binding, recorded != binding {
                    throw NativeAccessError.bindingMismatch
                }
                // A repairable root keeps its grant identity: re-granting the
                // same root replaces the bookmark bytes in place, so every
                // member route bound to this generation survives the repair.
                guard current.state.permitsResolution else {
                    let repaired: Data
                    do {
                        repaired = try codec.makeReadOnlyBookmark(for: selectedURL)
                    } catch {
                        throw NativeAccessError.accessDenied
                    }
                    let owners = current.ownerRefs.contains(ownerRef)
                        ? current.ownerRefs
                        : (current.ownerRefs + [ownerRef]).sorted()
                    let updated = current.with(
                        state: owners.isEmpty ? .provisional : .active,
                        ownerRefs: owners,
                        bookmark: repaired
                    )
                    var replacement = roots
                    replacement[rootRef] = updated
                    try persist(records: records, roots: replacement)
                    roots = replacement
                    return Self.rootDescriptor(updated)
                }
                guard let bookmark = current.bookmark else { throw NativeAccessError.invalidState }
                let resolved: (url: URL, stale: Bool)
                do {
                    resolved = try codec.resolveReadOnlyBookmark(bookmark)
                } catch {
                    throw NativeAccessError.accessDenied
                }
                guard !resolved.stale else { throw NativeAccessError.staleBookmark }
                let existing = resolved.url.resolvingSymlinksInPath().standardizedFileURL
                let selected = selectedURL.resolvingSymlinksInPath().standardizedFileURL
                guard existing == selected else { throw NativeAccessError.invalidRoute }
                guard !current.ownerRefs.contains(ownerRef) else {
                    return Self.rootDescriptor(current)
                }
                // A frozen group takes no further owners: a new reviewed scope
                // creates its own group instead of widening this one.
                guard !current.membersFrozen, current.authority != .frozenMembersOnly else {
                    throw NativeAccessError.membershipFrozen
                }
                let updated = current.with(
                    ownerRefs: (current.ownerRefs + [ownerRef]).sorted(),
                    // The broader of the two scopes wins only when a watch
                    // explicitly asks for it; nothing else widens a group.
                    authority: authority == .watchAndMembers ? .watchAndMembers : nil
                )
                var replacement = roots
                replacement[rootRef] = updated
                try persist(records: records, roots: replacement)
                roots = replacement
                return Self.rootDescriptor(updated)
            }
            let bookmark: Data
            do {
                bookmark = try codec.makeReadOnlyBookmark(for: selectedURL)
            } catch {
                throw NativeAccessError.accessDenied
            }
            let record = NativeRootAccessRecord(
                rootRef: rootRef,
                generation: (roots[rootRef]?.generation ?? 0) + 1,
                // Bound to its destination, not yet established: `activateRoot`
                // promotes it once the durable owner's record exists.
                state: .provisional,
                ownerRefs: [ownerRef],
                bookmark: bookmark,
                authority: authority,
                memberRefs: [],
                membersFrozen: false,
                binding: binding
            )
            var replacement = roots
            replacement[rootRef] = record
            try persist(records: records, roots: replacement)
            roots = replacement
            return Self.rootDescriptor(record)
        }
    }

    /// Marks a prepared root as established, which the owner calls once its
    /// durable run or subscription record exists. Until then the root stays
    /// `provisional`, so a crash during establishment leaves a grant that
    /// recovery can recognise as never completed rather than as live work.
    /// Repeating it for an already established root is idempotent.
    @discardableResult
    public func activateRoot(
        rootRef: String,
        expectedGeneration: UInt64
    ) throws -> NativeRootAccessDescriptor {
        try Self.validateIdentifier(rootRef)
        return try gate.withLock {
            guard let current = roots[rootRef] else { throw NativeAccessError.unknownAccess }
            guard current.state.permitsResolution else { throw Self.refusal(current.state) }
            guard current.generation == expectedGeneration else {
                throw NativeAccessError.generationMismatch
            }
            if current.state == .active { return Self.rootDescriptor(current) }
            let updated = current.with(state: .active)
            var replacement = roots
            replacement[rootRef] = updated
            try persist(records: records, roots: replacement)
            roots = replacement
            return Self.rootDescriptor(updated)
        }
    }

    /// Freezes the exact reviewed membership of a one-off plan at approval. From
    /// here on this group resolves only these members: a sibling created later
    /// never becomes readable or capturable through it, including after a
    /// restart, without a new explicitly reviewed scope.
    @discardableResult
    public func freezeRootMembers(
        rootRef: String,
        memberRefs: [String]
    ) throws -> NativeRootAccessDescriptor {
        try Self.validateIdentifier(rootRef)
        for memberRef in memberRefs { try Self.validateIdentifier(memberRef) }
        return try gate.withLock {
            guard let current = roots[rootRef] else { throw NativeAccessError.unknownAccess }
            guard current.state.permitsResolution else { throw Self.refusal(current.state) }
            let frozen = Set(memberRefs)
            guard frozen.count == memberRefs.count, !frozen.isEmpty else {
                throw NativeAccessError.invalidRoute
            }
            if current.membersFrozen {
                // Re-freezing is idempotent recovery, never a widening.
                guard Set(current.memberRefs) == frozen else {
                    throw NativeAccessError.membershipFrozen
                }
                return Self.rootDescriptor(current)
            }
            guard Set(current.memberRefs).isSubset(of: frozen) else {
                throw NativeAccessError.membershipFrozen
            }
            let updated = current.with(memberRefs: memberRefs.sorted(), membersFrozen: true)
            var replacement = roots
            replacement[rootRef] = updated
            try persist(records: records, roots: replacement)
            roots = replacement
            return Self.rootDescriptor(updated)
        }
    }

    /// Narrows a group's authority once its plan is finished or its watch is
    /// stopped. Authority is never widened here, so a completed one-off import
    /// cannot become a discovery scope again.
    @discardableResult
    public func narrowRootAuthority(
        rootRef: String,
        to authority: NativeRootAuthority
    ) throws -> NativeRootAccessDescriptor {
        try Self.validateIdentifier(rootRef)
        guard authority == .frozenMembersOnly else { throw NativeAccessError.invalidRoute }
        return try gate.withLock {
            guard let current = roots[rootRef] else { throw NativeAccessError.unknownAccess }
            if current.authority == authority, current.membersFrozen {
                return Self.rootDescriptor(current)
            }
            // Whatever was enrolled by the time the plan ended is the final set.
            let updated = current.with(authority: authority, membersFrozen: true)
            var replacement = roots
            replacement[rootRef] = updated
            try persist(records: records, roots: replacement)
            roots = replacement
            return Self.rootDescriptor(updated)
        }
    }

    /// Releases one durable root owner. Member records independently retain the
    /// shared grant; bookmark bytes are removed only after the last owner and
    /// last connected member are both gone.
    @discardableResult
    public func releaseRoot(rootRef: String, ownerRef: String) throws -> NativeRootAccessDescriptor {
        try Self.validateIdentifier(rootRef)
        try Self.validateIdentifier(ownerRef)
        return try gate.withLock {
            guard let current = roots[rootRef] else { throw NativeAccessError.unknownAccess }
            let owners = current.ownerRefs.filter { $0 != ownerRef }
            let hasMember = records.values.contains {
                guard $0.state == .connected,
                      case let .rootMember(candidate, _, _) = $0.route
                else { return false }
                return candidate == rootRef
            }
            // Releasing one owner is not a diagnosis: a root that something
            // still routes through keeps whatever state it was in, repairable
            // included, and only the last release disconnects it.
            let held = !owners.isEmpty || hasMember
            let updated = current.with(
                state: held ? current.state : .disconnected,
                ownerRefs: owners,
                bookmark: held ? current.bookmark : Data?.none
            )
            var replacement = roots
            replacement[rootRef] = updated
            try persist(records: records, roots: replacement)
            roots = replacement
            return Self.rootDescriptor(updated)
        }
    }

    @discardableResult
    public func disconnect(accessRef: String) throws -> NativeAccessDescriptor {
        try gate.withLock {
            guard let current = records[accessRef] else { throw NativeAccessError.unknownAccess }
            if current.state == .disconnected { return Self.descriptor(current) }
            let record = NativeAccessRecord(
                accessRef: current.accessRef,
                linkedDocumentID: current.linkedDocumentID,
                generation: current.generation,
                rootBindingGeneration: current.rootBindingGeneration,
                state: .disconnected,
                route: current.route,
                bookmark: nil
            )
            var replacement = records
            replacement[accessRef] = record
            var replacementRoots = roots
            Self.pruneUnusedRoot(for: current.route, records: replacement, roots: &replacementRoots)
            try persist(records: replacement, roots: replacementRoots)
            records = replacement
            roots = replacementRoots
            return Self.descriptor(record)
        }
    }

    public func disconnect(linkedDocumentID: String) throws -> [NativeAccessDescriptor] {
        try Self.validateIdentifier(linkedDocumentID)
        return try gate.withLock {
            let matches = records.values.filter {
                $0.linkedDocumentID == linkedDocumentID && $0.state == .connected
            }
            if matches.isEmpty { return [] }
            var replacement = records
            for current in matches {
                replacement[current.accessRef] = NativeAccessRecord(
                    accessRef: current.accessRef,
                    linkedDocumentID: current.linkedDocumentID,
                    generation: current.generation,
                    rootBindingGeneration: current.rootBindingGeneration,
                    state: .disconnected,
                    route: current.route,
                    bookmark: nil
                )
            }
            var replacementRoots = roots
            for current in matches {
                Self.pruneUnusedRoot(for: current.route, records: replacement, roots: &replacementRoots)
            }
            try persist(records: replacement, roots: replacementRoots)
            records = replacement
            roots = replacementRoots
            return matches.map { Self.descriptor(replacement[$0.accessRef]!) }
                .sorted { $0.accessRef < $1.accessRef }
        }
    }

    public func withReadAccess<T>(
        accessRef: String,
        expectedGeneration: UInt64,
        expectedRootBindingGeneration: UInt64? = nil,
        _ body: (URL) throws -> T
    ) throws -> T {
        let resolvedAccess = try gate.withLock { () throws -> (NativeAccessRecord, Data) in
            guard let record = records[accessRef] else { throw NativeAccessError.unknownAccess }
            guard record.state == .connected else { throw NativeAccessError.disconnected }
            guard record.generation == expectedGeneration else {
                throw NativeAccessError.generationMismatch
            }
            switch record.route {
            case .directFile:
                guard expectedRootBindingGeneration == nil, let bookmark = record.bookmark else {
                    throw NativeAccessError.generationMismatch
                }
                return (record, bookmark)
            case let .rootMember(rootRef, _, _):
                guard let root = roots[rootRef] else { throw NativeAccessError.generationMismatch }
                guard root.state.permitsResolution else { throw Self.refusal(root.state) }
                guard record.bookmark == nil,
                      let frozenRootGeneration = record.rootBindingGeneration,
                      let expectedRootBindingGeneration,
                      frozenRootGeneration == expectedRootBindingGeneration,
                      root.generation == expectedRootBindingGeneration,
                      let bookmark = root.bookmark
                else { throw NativeAccessError.generationMismatch }
                return (record, bookmark)
            }
        }
        let record = resolvedAccess.0
        let bookmark = resolvedAccess.1
        // A member resolves through the root's own bookmark, so a failure here
        // is the root group's condition, not this member's.
        let rootRef: String?
        if case let .rootMember(reference, _, _) = record.route {
            rootRef = reference
        } else {
            rootRef = nil
        }
        let resolved: (url: URL, stale: Bool)
        do {
            resolved = try codec.resolveReadOnlyBookmark(bookmark)
        } catch {
            markRoot(rootRef, .needsAccess)
            throw NativeAccessError.accessDenied
        }
        guard !resolved.stale else {
            markRoot(rootRef, .needsAccess)
            throw NativeAccessError.staleBookmark
        }
        guard codec.startAccessing(resolved.url) else {
            markRoot(rootRef, .unavailable)
            throw NativeAccessError.accessDenied
        }
        defer { codec.stopAccessing(resolved.url) }
        let target = try Self.targetURL(root: resolved.url, route: record.route)
        return try body(target)
    }

    /// Resolves the selected root behind an existing root-member route. This is available only to
    /// the native monitoring owner; callers still have to enforce the persisted subscription scope.
    public func withRootReadAccess<T>(
        rootRef: String,
        expectedGeneration: UInt64,
        purpose: NativeRootAccessPurpose = .scopePreview,
        _ body: (URL) throws -> T
    ) throws -> T {
        let root = try gate.withLock { () throws -> NativeRootAccessRecord in
            guard let root = roots[rootRef] else { throw NativeAccessError.unknownAccess }
            guard root.state.permitsResolution, root.bookmark != nil else {
                throw Self.refusal(root.state)
            }
            guard root.generation == expectedGeneration else {
                throw NativeAccessError.generationMismatch
            }
            // A retained one-off root is not an invisible ongoing subscription:
            // it resolves its frozen members, and never enumerates for new ones.
            // Discovery also belongs to an established owner, so a root that is
            // still only prepared does not enumerate either.
            guard purpose == .scopePreview
                || (root.authority == .watchAndMembers && root.state == .active)
            else {
                throw NativeAccessError.discoveryNotPermitted
            }
            return root
        }
        guard let bookmark = root.bookmark else { throw NativeAccessError.disconnected }
        let resolved: (url: URL, stale: Bool)
        do {
            resolved = try codec.resolveReadOnlyBookmark(bookmark)
        } catch {
            markRoot(rootRef, .needsAccess)
            throw NativeAccessError.accessDenied
        }
        guard !resolved.stale else {
            markRoot(rootRef, .needsAccess)
            throw NativeAccessError.staleBookmark
        }
        guard codec.startAccessing(resolved.url) else {
            markRoot(rootRef, .unavailable)
            throw NativeAccessError.accessDenied
        }
        defer { codec.stopAccessing(resolved.url) }
        let values = try resolved.url.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
        guard values.isDirectory == true, values.isSymbolicLink != true else {
            // The grant still resolves; the root itself is not usable as one
            // right now, which a remount or a re-grant can change.
            markRoot(rootRef, .unavailable)
            throw NativeAccessError.invalidRoute
        }
        return try body(resolved.url.resolvingSymlinksInPath().standardizedFileURL)
    }

    /// Adds a newly discovered child while the already-authorised root scope is active. The caller
    /// supplies only opaque root/member identities and a validated root-relative locator.
    @discardableResult
    public func adoptRootMember(
        accessRef: String,
        linkedDocumentID: String,
        rootRef: String,
        memberRef: String,
        relativeComponents: [String],
        expectedRootGeneration: UInt64
    ) throws -> NativeAccessDescriptor {
        try Self.validateIdentifier(accessRef)
        try Self.validateIdentifier(linkedDocumentID)
        let route = NativeAccessRoute.rootMember(
            rootRef: rootRef,
            memberRef: memberRef,
            relativeComponents: relativeComponents
        )
        try Self.validate(route)
        return try gate.withLock {
            guard let root = roots[rootRef], root.bookmark != nil else {
                throw NativeAccessError.unknownAccess
            }
            guard root.state.permitsResolution else { throw Self.refusal(root.state) }
            guard root.generation == expectedRootGeneration else {
                throw NativeAccessError.generationMismatch
            }
            // Finding a child is not authority to enrol it. Once a reviewed
            // plan's membership is frozen, only those members resolve — so a
            // sibling created afterwards, now or after a restart, is refused.
            if root.membersFrozen || root.authority == .frozenMembersOnly {
                guard root.memberRefs.contains(memberRef) else {
                    throw NativeAccessError.membershipFrozen
                }
            }
            let existing = records[accessRef]
            if let existing,
               existing.linkedDocumentID != linkedDocumentID || existing.route != route
            {
                throw NativeAccessError.invalidRoute
            }
            if let existing, existing.state == .connected {
                // Exact replay of an establishment that already enrolled this
                // member: the grant it holds is returned rather than a second
                // one created under a fresh generation. A different document or
                // route under the same reference was refused above, and one
                // bound to a superseded root is still a duplicate.
                guard existing.rootBindingGeneration == root.generation else {
                    throw NativeAccessError.duplicateConnectedAccess
                }
                return Self.descriptor(existing)
            }
            let record = NativeAccessRecord(
                accessRef: accessRef,
                linkedDocumentID: linkedDocumentID,
                generation: (existing?.generation ?? 0) + 1,
                rootBindingGeneration: root.generation,
                state: .connected,
                route: route,
                bookmark: nil
            )
            var replacement = records
            replacement[accessRef] = record
            var replacementRoots = roots
            if !root.memberRefs.contains(memberRef) {
                replacementRoots[rootRef] = root.with(
                    memberRefs: (root.memberRefs + [memberRef]).sorted()
                )
            }
            try persist(records: replacement, roots: replacementRoots)
            records = replacement
            roots = replacementRoots
            return Self.descriptor(record)
        }
    }

    /// Rechecks a complete prepared batch while preventing relink/disconnect from interleaving with
    /// its publication. The body must only perform the bounded local queue commit.
    public func withValidatedGenerations<T>(
        _ expectations: [NativeAccessExpectation],
        _ body: () throws -> T
    ) throws -> T {
        try gate.withLock {
            guard !expectations.isEmpty,
                  Set(expectations.map(\.accessRef)).count == expectations.count
            else { throw NativeAccessError.invalidRoute }
            for expectation in expectations {
                guard let record = records[expectation.accessRef] else {
                    throw NativeAccessError.unknownAccess
                }
                guard record.state == .connected else { throw NativeAccessError.disconnected }
                guard record.generation == expectation.generation else {
                    throw NativeAccessError.generationMismatch
                }
                switch record.route {
                case .directFile:
                    guard record.bookmark != nil, expectation.rootBindingGeneration == nil else {
                        throw NativeAccessError.generationMismatch
                    }
                case let .rootMember(rootRef, _, _):
                    guard let root = roots[rootRef] else {
                        throw NativeAccessError.generationMismatch
                    }
                    guard root.state.permitsResolution else { throw Self.refusal(root.state) }
                    guard record.bookmark == nil,
                          let frozen = record.rootBindingGeneration,
                          let expected = expectation.rootBindingGeneration,
                          frozen == expected,
                          root.generation == expected,
                          root.bookmark != nil
                    else { throw NativeAccessError.generationMismatch }
                }
            }
            return try body()
        }
    }

    /// Records a root-level access failure against the group, so the next read
    /// refuses it without re-resolving while the route it is bound to is kept
    /// for a repair. Already-refusing and released roots are left alone.
    private func markRoot(_ rootRef: String?, _ state: NativeRootAccessState) {
        guard let rootRef else { return }
        try? gate.withLock {
            guard let current = roots[rootRef], current.state.permitsResolution else { return }
            var replacement = roots
            replacement[rootRef] = current.with(state: state)
            try persist(records: records, roots: replacement)
            roots = replacement
        }
    }

    /// How a root that cannot be resolved right now refuses: the condition that
    /// put it in that state, so a caller handles it as it would the live
    /// failure.
    private static func refusal(_ state: NativeRootAccessState) -> NativeAccessError {
        switch state {
        case .needsAccess: .staleBookmark
        case .unavailable: .accessDenied
        case .disconnected: .disconnected
        case .provisional, .active: .invalidState
        }
    }

    /// `pending` defaults to what is already recorded, so only the two calls
    /// that change an establishment have to name it — and both of those write it
    /// in the same durable write as the authority it covers.
    private func persist(
        records: [String: NativeAccessRecord],
        roots: [String: NativeRootAccessRecord],
        pending: [String: PendingFolderSubmission]? = nil
    ) throws {
        let document = NativeAccessDocument(
            schema: Self.schema,
            records: records.values.sorted { $0.accessRef < $1.accessRef },
            roots: roots.values.sorted { $0.rootRef < $1.rootRef },
            pending: (pending ?? self.pending).values
                .sorted { $0.submissionID < $1.submissionID }
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        do {
            let data = try encoder.encode(document)
            let temporary = directory.appendingPathComponent(".native-access-(UUID().uuidString).tmp")
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
        } catch {
            throw NativeAccessError.stateUnavailable
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
                throw NativeAccessError.stateUnavailable
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        } catch {
            throw NativeAccessError.stateUnavailable
        }
    }

    private static func load(
        from stateFile: URL,
        codec: any SecurityScopedBookmarkCoding
    ) throws -> (
        records: [String: NativeAccessRecord],
        roots: [String: NativeRootAccessRecord],
        pending: [String: PendingFolderSubmission],
        migrated: Bool
    ) {
        guard FileManager.default.fileExists(atPath: stateFile.path) else {
            return ([:], [:], [:], false)
        }
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: stateFile.path)
            guard (attributes[.type] as? FileAttributeType) == .typeRegular else {
                throw NativeAccessError.invalidState
            }
            guard let size = attributes[.size] as? NSNumber,
                  size.uint64Value <= 16 * 1024 * 1024,
                  let owner = attributes[.ownerAccountID] as? NSNumber,
                  owner.uint32Value == Darwin.getuid(),
                  let permissions = attributes[.posixPermissions] as? NSNumber,
                  permissions.intValue & 0o077 == 0
            else { throw NativeAccessError.invalidState }
            let raw = try Data(contentsOf: stateFile, options: [.mappedIfSafe])
            let envelope = try JSONDecoder().decode(NativeAccessSchemaEnvelope.self, from: raw)
            if envelope.schema == "omnivia.native-access.v1" {
                return try migrateLegacy(raw, codec: codec)
            }
            // A v2 file predates recorded root-group authority. Its roots keep
            // the behaviour they already had; an owner that finishes its plan
            // narrows them on the next reconcile. An unknown later schema is
            // preserved on disk and refused here rather than reinterpreted.
            let migrated = envelope.schema == "omnivia.native-access.v2"
            let data = migrated ? try upgradeRootGroups(raw) : raw
            let document = try JSONDecoder().decode(NativeAccessDocument.self, from: data)
            guard document.schema == schema, document.records.count <= 100_000 else {
                throw NativeAccessError.invalidState
            }
            var result: [String: NativeAccessRecord] = [:]
            for record in document.records {
                try validateIdentifier(record.accessRef)
                try validateIdentifier(record.linkedDocumentID)
                try validate(record.route)
                guard record.generation > 0, result[record.accessRef] == nil else {
                    throw NativeAccessError.invalidState
                }
                switch record.route {
                case .directFile:
                    guard record.rootBindingGeneration == nil,
                          (record.state == .connected) == (record.bookmark != nil)
                    else { throw NativeAccessError.invalidState }
                case .rootMember:
                    guard record.rootBindingGeneration != nil, record.bookmark == nil else {
                        throw NativeAccessError.invalidState
                    }
                }
                result[record.accessRef] = record
            }
            guard document.roots.count <= 100_000 else { throw NativeAccessError.invalidState }
            var loadedRoots: [String: NativeRootAccessRecord] = [:]
            for root in document.roots {
                try validateIdentifier(root.rootRef)
                guard root.generation > 0,
                      loadedRoots[root.rootRef] == nil,
                      root.ownerRefs == root.ownerRefs.sorted(),
                      Set(root.ownerRefs).count == root.ownerRefs.count,
                      root.ownerRefs.allSatisfy({ (try? validateIdentifier($0)) != nil }),
                      root.memberRefs == root.memberRefs.sorted(),
                      Set(root.memberRefs).count == root.memberRefs.count,
                      root.memberRefs.count <= FolderScopeLimits.standard.maximumEligibleFiles,
                      root.memberRefs.allSatisfy({ (try? validateIdentifier($0)) != nil }),
                      // A frozen set cannot survive as an open one, and an open
                      // set cannot be labelled completed.
                      root.authority != .frozenMembersOnly || root.membersFrozen,
                      // Only an explicit release drops the route, so every
                      // other state — a repairable one included — must still
                      // hold its bookmark rather than be collapsed into one
                      // that cannot be repaired.
                      root.state.retainsRoute == (root.bookmark != nil)
                else { throw NativeAccessError.invalidState }
                loadedRoots[root.rootRef] = root
            }
            for record in result.values {
                guard case let .rootMember(rootRef, _, _) = record.route else { continue }
                guard let root = loadedRoots[rootRef] else { throw NativeAccessError.invalidState }
                if record.state == .connected {
                    // A connected member needs a root that still holds the
                    // route it bound. A root waiting to be repaired does, so
                    // the whole store is not refused over one that can be.
                    guard root.state.retainsRoute,
                          root.generation == record.rootBindingGeneration
                    else { throw NativeAccessError.invalidState }
                }
            }
            var loadedPending: [String: PendingFolderSubmission] = [:]
            var pendingRoots: Set<String> = []
            for intent in document.pending ?? [] {
                try validate(intent)
                // An intent is written with its root grant and retired before
                // it, so one without a root is a torn file, not a recoverable
                // establishment. The grant must also still be the one this
                // intent authorised: a binding is never mutated after the write
                // that created it, and one establishment never shares its root
                // with another.
                guard loadedPending[intent.submissionID] == nil,
                      pendingRoots.insert(intent.rootRef).inserted,
                      let root = loadedRoots[intent.rootRef],
                      root.binding == intent.binding
                else { throw NativeAccessError.invalidState }
                loadedPending[intent.submissionID] = intent
            }
            return (result, loadedRoots, loadedPending, false)
        } catch let error as NativeAccessError {
            throw error
        } catch {
            throw NativeAccessError.invalidState
        }
    }

    /// Fills the root-group fields a v2 file never recorded, leaving every other
    /// key exactly as written. A v2 root had only the file grant's two states,
    /// so a connected one is an established root: it migrates to `active`, and
    /// a disconnected one keeps the same meaning under the same name.
    private static func upgradeRootGroups(_ data: Data) throws -> Data {
        guard var document = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw NativeAccessError.invalidState
        }
        var roots = document["roots"] as? [[String: Any]] ?? []
        for index in roots.indices {
            if roots[index]["state"] as? String == NativeAccessState.connected.rawValue {
                roots[index]["state"] = NativeRootAccessState.active.rawValue
            }
            if roots[index]["authority"] == nil {
                roots[index]["authority"] = NativeRootAuthority.watchAndMembers.rawValue
            }
            if roots[index]["memberRefs"] == nil { roots[index]["memberRefs"] = [String]() }
            if roots[index]["membersFrozen"] == nil { roots[index]["membersFrozen"] = false }
        }
        document["roots"] = roots
        document["schema"] = schema
        return try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
    }

    private static func migrateLegacy(
        _ data: Data,
        codec: any SecurityScopedBookmarkCoding
    ) throws -> (
        records: [String: NativeAccessRecord],
        roots: [String: NativeRootAccessRecord],
        pending: [String: PendingFolderSubmission],
        migrated: Bool
    ) {
        let legacy = try JSONDecoder().decode(LegacyNativeAccessDocument.self, from: data)
        guard legacy.schema == "omnivia.native-access.v1", legacy.records.count <= 100_000 else {
            throw NativeAccessError.invalidState
        }
        var records: [String: NativeAccessRecord] = [:]
        var roots: [String: NativeRootAccessRecord] = [:]
        var canonicalRoots: [String: URL] = [:]
        for old in legacy.records {
            try validateIdentifier(old.accessRef)
            try validateIdentifier(old.linkedDocumentID)
            try validate(old.route)
            guard old.generation > 0, records[old.accessRef] == nil else {
                throw NativeAccessError.invalidState
            }
            switch old.route {
            case .directFile:
                guard (old.state == .connected) == (old.bookmark != nil) else {
                    throw NativeAccessError.invalidState
                }
                records[old.accessRef] = NativeAccessRecord(
                    accessRef: old.accessRef,
                    linkedDocumentID: old.linkedDocumentID,
                    generation: old.generation,
                    rootBindingGeneration: nil,
                    state: old.state,
                    route: old.route,
                    bookmark: old.bookmark
                )
            case let .rootMember(rootRef, memberRef, _):
                guard (old.state == .connected) == (old.bookmark != nil) else {
                    throw NativeAccessError.invalidState
                }
                if let bookmark = old.bookmark {
                    let resolved = try codec.resolveReadOnlyBookmark(bookmark)
                    guard !resolved.stale else { throw NativeAccessError.staleBookmark }
                    let canonical = resolved.url.resolvingSymlinksInPath().standardizedFileURL
                    if let existing = canonicalRoots[rootRef], existing != canonical {
                        throw NativeAccessError.invalidState
                    }
                    canonicalRoots[rootRef] = canonical
                    if let root = roots[rootRef] {
                        if root.bookmark == nil {
                            roots[rootRef] = root.with(state: .active, bookmark: bookmark)
                        }
                    } else {
                        roots[rootRef] = Self.migratedRoot(
                            rootRef: rootRef,
                            state: .active,
                            bookmark: bookmark
                        )
                    }
                } else if roots[rootRef] == nil {
                    roots[rootRef] = Self.migratedRoot(
                        rootRef: rootRef,
                        state: .disconnected,
                        bookmark: nil
                    )
                }
                if let root = roots[rootRef], !root.memberRefs.contains(memberRef) {
                    roots[rootRef] = root.with(
                        memberRefs: (root.memberRefs + [memberRef]).sorted()
                    )
                }
                records[old.accessRef] = NativeAccessRecord(
                    accessRef: old.accessRef,
                    linkedDocumentID: old.linkedDocumentID,
                    generation: old.generation,
                    rootBindingGeneration: 1,
                    state: old.state,
                    route: old.route,
                    bookmark: nil
                )
            }
        }
        for record in records.values where record.state == .connected {
            guard case let .rootMember(rootRef, _, _) = record.route else { continue }
            guard roots[rootRef]?.state == .active else { throw NativeAccessError.invalidState }
        }
        // A v1 file predates durable establishment, so there is nothing pending.
        return (records, roots, [:], true)
    }

    /// A v1 file has no root group, only per-member bookmarks. The migrated
    /// group keeps exactly the members it already had, under frozen authority:
    /// an upgrade neither loses a retained manual route nor gains the right to
    /// discover anything new.
    private static func migratedRoot(
        rootRef: String,
        state: NativeRootAccessState,
        bookmark: Data?
    ) -> NativeRootAccessRecord {
        NativeRootAccessRecord(
            rootRef: rootRef,
            generation: 1,
            state: state,
            ownerRefs: [],
            bookmark: bookmark,
            authority: .frozenMembersOnly,
            memberRefs: [],
            membersFrozen: true,
            binding: nil
        )
    }

    private static func validateIdentifier(_ value: String) throws {
        guard !value.isEmpty,
              value.utf8.count <= 128,
              value.first?.isASCII == true,
              value.first?.isLetter == true || value.first?.isNumber == true,
              value.unicodeScalars.allSatisfy({ identifierCharacters.contains($0) })
        else { throw NativeAccessError.invalidIdentifier }
    }

    private static func validate(_ route: NativeAccessRoute) throws {
        switch route {
        case .directFile:
            return
        case let .rootMember(rootRef, memberRef, components):
            try validateIdentifier(rootRef)
            try validateIdentifier(memberRef)
            guard !components.isEmpty, components.count <= 256 else { throw NativeAccessError.invalidRoute }
            for component in components {
                guard !component.isEmpty,
                      component != ".",
                      component != "..",
                      !component.contains("/"),
                      !component.contains("\0")
                else { throw NativeAccessError.invalidRoute }
            }
        }
    }

    /// A pending establishment is only as trustworthy as the identities it
    /// freezes, so every one of them is checked before it is durable and again
    /// when it is read back.
    private static func validate(_ intent: PendingFolderSubmission) throws {
        for identifier in [
            intent.submissionID, intent.rootRef, intent.manifestRevision,
            intent.observationEpoch, intent.approvedFormatPolicyRef,
            intent.exclusionsPolicyRef, intent.binding.targetRef,
            intent.binding.workspaceRef, intent.binding.targetBindingRevision,
        ] + [intent.runID, intent.subscriptionID].compactMap({ $0 }) + intent.ownerRefs {
            try validateIdentifier(identifier)
        }
        // The mode decides which durable owners this intent will ever have and
        // the authority its root grant is created under, so a combination an
        // approved submission cannot produce is refused rather than recorded: a
        // watch always has a subscription and discovery authority, a reviewed
        // one-off never has either, a finished group's authority is never an
        // establishment's starting point, and a run exists exactly when there
        // is reviewed existing content to import.
        let importsExisting = intent.mode == .importOnce
            || intent.watchInitialContent != .onlyNewAndChanged
        guard (intent.subscriptionID != nil) == (intent.mode == .keepUpdated),
              intent.authority == (intent.mode == .keepUpdated ? .watchAndMembers : .importPlan),
              (intent.runID != nil) == (importsExisting && !intent.members.isEmpty)
        else { throw NativeAccessError.invalidRoute }
        guard !intent.ownerRefs.isEmpty,
              !intent.rootDisplayName.isEmpty,
              intent.rootDisplayName.count <= 255,
              !intent.rootDisplayName.contains("\0"),
              !intent.createdAt.isEmpty,
              intent.configGeneration > 0,
              intent.consentGeneration > 0,
              intent.members.count <= FolderScopeLimits.standard.maximumEligibleFiles,
              // Every identity and locator a member carries is its own across
              // the whole reviewed set. Two members sharing one would make a
              // single durable row, grant, queued item or payload serve two
              // files, which no later check could tell apart.
              Set(intent.members.map(\.memberID)).count == intent.members.count,
              Set(intent.members.map(\.accessRef)).count == intent.members.count,
              Set(intent.members.map(\.itemID)).count == intent.members.count,
              Set(intent.members.map(\.linkedDocumentID)).count == intent.members.count,
              Set(intent.members.map(\.requestedSourceIdentity)).count == intent.members.count,
              Set(intent.members.map(\.payloadRef)).count == intent.members.count,
              Set(intent.members.map(\.operationKey)).count == intent.members.count,
              Set(intent.members.map(\.relativeComponents)).count == intent.members.count
        else { throw NativeAccessError.invalidRoute }
        for member in intent.members {
            for identifier in [
                member.memberID, member.accessRef, member.linkedDocumentID,
                member.requestedSourceIdentity, member.itemID, member.payloadRef,
                member.operationKey,
            ] {
                try validateIdentifier(identifier)
            }
            try validate(
                .rootMember(
                    rootRef: intent.rootRef,
                    memberRef: member.memberID,
                    relativeComponents: member.relativeComponents
                )
            )
            guard member.declaredByteLength > 0,
                  member.declaredByteLength <= CaptureIntentV3.maximumPayloadBytes,
                  !member.displayName.isEmpty
            else { throw NativeAccessError.invalidRoute }
        }
    }

    private static func validateSelection(_ url: URL, for route: NativeAccessRoute) throws {
        guard url.isFileURL,
              let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .isDirectoryKey])
        else { throw NativeAccessError.accessDenied }
        switch route {
        case .directFile:
            guard values.isRegularFile == true else { throw NativeAccessError.accessDenied }
        case .rootMember:
            guard values.isDirectory == true else { throw NativeAccessError.accessDenied }
        }
    }

    /// Resolves a member by walking real native relationships one component at a
    /// time. A string-prefix pathname comparison is not containment, so none is
    /// used: every component must exist directly under the previous one, be
    /// neither a symbolic link nor a Finder alias, stay on the root's own
    /// volume, and — for the intermediate components — be an ordinary directory
    /// rather than a package. The member itself must be an ordinary file. A
    /// concurrent move out of scope therefore fails the read instead of
    /// following the file to its new location.
    private static func targetURL(root: URL, route: NativeAccessRoute) throws -> URL {
        switch route {
        case .directFile:
            return root
        case let .rootMember(_, _, components):
            let canonicalRoot = root.resolvingSymlinksInPath().standardizedFileURL
            guard let rootValues = try? canonicalRoot.resourceValues(
                forKeys: [.isDirectoryKey, .isSymbolicLinkKey, .volumeIdentifierKey]
            ),
                rootValues.isDirectory == true,
                rootValues.isSymbolicLink != true,
                let volume = rootValues.volumeIdentifier,
                !components.isEmpty
            else { throw NativeAccessError.memberOutsideRoot }
            var candidate = canonicalRoot
            for (index, component) in components.enumerated() {
                let isMember = index == components.count - 1
                candidate.appendPathComponent(component, isDirectory: !isMember)
                guard let values = try? candidate.resourceValues(forKeys: [
                    .isSymbolicLinkKey,
                    .isAliasFileKey,
                    .isDirectoryKey,
                    .isRegularFileKey,
                    .isPackageKey,
                    .volumeIdentifierKey,
                ]),
                    values.isSymbolicLink != true,
                    values.isAliasFile != true,
                    let componentVolume = values.volumeIdentifier,
                    componentVolume.isEqual(volume),
                    isMember
                        ? values.isRegularFile == true
                        : (values.isDirectory == true && values.isPackage != true)
                else { throw NativeAccessError.memberOutsideRoot }
            }
            return candidate.standardizedFileURL
        }
    }

    private static func descriptor(_ record: NativeAccessRecord) -> NativeAccessDescriptor {
        NativeAccessDescriptor(
            accessRef: record.accessRef,
            linkedDocumentID: record.linkedDocumentID,
            generation: record.generation,
            rootBindingGeneration: record.rootBindingGeneration,
            state: record.state,
            route: record.route
        )
    }

    private static func rootDescriptor(_ record: NativeRootAccessRecord) -> NativeRootAccessDescriptor {
        NativeRootAccessDescriptor(
            rootRef: record.rootRef,
            generation: record.generation,
            state: record.state,
            ownerRefs: record.ownerRefs,
            authority: record.authority,
            memberRefs: record.memberRefs,
            membersFrozen: record.membersFrozen,
            binding: record.binding
        )
    }

    private static func pruneUnusedRoot(
        for route: NativeAccessRoute,
        records: [String: NativeAccessRecord],
        roots: inout [String: NativeRootAccessRecord]
    ) {
        guard case let .rootMember(rootRef, _, _) = route,
              let root = roots[rootRef],
              root.ownerRefs.isEmpty
        else { return }
        let hasMember = records.values.contains {
            guard $0.state == .connected,
                  case let .rootMember(candidate, _, _) = $0.route
            else { return false }
            return candidate == rootRef
        }
        guard !hasMember else { return }
        roots[rootRef] = root.with(state: .disconnected, ownerRefs: [], bookmark: Data?.none)
    }
}
