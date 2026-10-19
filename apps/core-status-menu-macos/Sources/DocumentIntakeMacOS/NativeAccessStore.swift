import CryptoKit
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
    /// An explicit Refresh, Open original or Reveal in Finder was asked of a
    /// route that is still only provisional — a member whose root group is, or a
    /// direct file no published work owns. Preparing that route's own frozen
    /// capture intent is permitted; acting on the original as a linked file
    /// needs an established owner.
    case routeNotActive
    /// A Locate for this grant has reserved its next generation and Core has not yet
    /// confirmed it. The old grant reads nothing and the new one is not yet active.
    case relinkPending
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

/// Which fixed broker operation (design §9.2) is resolving one original.
///
/// The distinction is the whole of design §7.2: "A provisional route is not an
/// active linked-file capability. It may prepare only its own frozen capture
/// intent … External broker Refresh/Open/Reveal requires an active route."
/// Preparation therefore still resolves while a root group is provisional —
/// establishment reads and publishes its reviewed members before `activateRoot`
/// promotes the group — and the three explicit user actions do not.
public enum NativeAccessPurpose: Sendable {
    /// Preparing this route's own frozen capture intent, under an explicit Add,
    /// confirmed bulk-run membership or active standing consent. The default,
    /// because every automatic and submission path is one.
    case prepareCapture
    /// An explicit user Refresh, Open original or Reveal in Finder. All three
    /// resolve the original through this record and current generation (design
    /// §13.1, §13.2), so one rule gates all three.
    case originalAction
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
    /// Design §9.5: a confirmed Locate reserved this grant's next generation and Core has
    /// not yet confirmed it. The superseded bookmark is gone and the candidate's is held
    /// only by the relink intent, so nothing can be read through this route in either
    /// direction until `completeOriginalRelink` activates the new grant.
    case relinking
}

/// A direct-file grant's own lifecycle.
///
/// A member's equivalent lives on its root group (`NativeRootAccessState`): the
/// group is what is provisional until a durable owner exists. A direct file has
/// no group between it and its original, so design §7.2's distinction is
/// recorded here instead. It is orthogonal to `NativeAccessState`, which says
/// whether the grant still holds its bookmark: a disconnected grant keeps the
/// lifecycle it had, so a relink of established work repairs back into it.
public enum NativeAccessLifecycle: String, Codable, Sendable {
    /// Adopted for a reviewed candidate that no published queue entry and no
    /// registered file subscription owns yet. It may prepare its own frozen
    /// capture intent, and serves no explicit original action.
    case provisional
    /// Published work owns this grant.
    case active
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

/// The canonical half of design §13.3 Disconnect: a fixed intent to tell Core that one
/// original binding is disconnected.
///
/// It is written in the same durable write that revokes the local route, so there is no
/// state in which this Mac has revoked a source and holds nothing that will tell Core. The
/// request it describes is closed over here and never recomputed: the owner, document and
/// route are the canonical binding Core was told about, the frozen binding is the
/// workspace and caller that binding was captured under (never whichever account is signed
/// in now), and `operationKey` is the one key every attempt carries. A lost or unknown
/// answer is therefore reconciled by sending exactly the same request again.
///
/// The key is derived from the grant it closes (workspace, owner, document and the
/// generation that was revoked), not drawn at random. A second Disconnect of the same
/// grant cannot mint a second intent, and a later grant of the same owner revokes under a
/// different generation and so a different key.
public struct OriginalDisconnectIntent: Codable, Equatable, Sendable {
    /// Core's `owner_ref` for the binding: this Mac's access record.
    public let accessRef: String
    /// Core's `document_id`.
    public let linkedDocumentID: String
    /// `direct_file` or `root_member`, as the capture that created the binding sent it.
    public let routeKind: String
    public let rootRef: String?
    public let memberRef: String?
    /// The local grant generation this revocation closed.
    public let generation: UInt64
    public let binding: NativeRootAccessBinding
    public let operationKey: String
    public let createdAt: String
    /// Set once Core refused the request in a way no retry changes (a binding that is
    /// another document's, a request it cannot read). The local revocation still stands,
    /// and the intent is kept rather than dropped so the refusal stays on record.
    public var refusalCode: String?

    public static func operationKey(
        workspaceRef: String,
        accessRef: String,
        linkedDocumentID: String,
        generation: UInt64
    ) -> String {
        let material = [workspaceRef, accessRef, linkedDocumentID, String(generation)]
            .joined(separator: "\u{0}")
        let digest = SHA256.hash(data: Data(material.utf8)).map { String(format: "%02x", $0) }
        return "disconnect-" + digest.joined().prefix(32)
    }
}

/// The canonical half of design §9.5 Relink: a fixed intent to move one original binding to
/// the generation this Mac reserved for the file the person confirmed.
///
/// It is written in the same durable write that reserves that generation and pauses the
/// route, so there is no state in which this Mac has stopped reading the old file and
/// owes Core nothing. The request it describes is closed over here and never recomputed:
/// the owner, document and generation, the frozen workspace and caller of the work the
/// route already published, and the one `operationKey` every attempt carries. A lost or
/// unknown answer is therefore reconciled by sending exactly the same request again, and
/// there is never a second candidate: a new Locate is refused until this intent is
/// answered for good.
///
/// The candidate itself is `candidateBookmark`. It is held here and nowhere else, never
/// resolved while the intent is owed, and becomes the route's grant only when Core has
/// confirmed the generation (`NativeAccessStore.completeOriginalRelink`). Like the
/// disconnect key, the operation key is derived from the generation it reserves, not drawn
/// at random.
public struct OriginalRelinkIntent: Codable, Equatable, Sendable {
    /// Core's `owner_ref` for the binding: this Mac's access record.
    public let accessRef: String
    /// Core's `document_id`.
    public let linkedDocumentID: String
    /// The binding generation reserved for the new file. The route's own generation is
    /// already this value while the relink is owed.
    public let generation: UInt64
    public let binding: NativeRootAccessBinding
    public let operationKey: String
    public let createdAt: String
    /// The file name of the confirmed candidate, for display only.
    public let candidateName: String
    let candidateBookmark: Data
    /// Set once Core refused the request in a way no retry changes. The route stays
    /// paused, the refusal stays on record, and only a refused intent may be replaced by
    /// another Locate.
    public var refusalCode: String?

    public static func operationKey(
        workspaceRef: String,
        accessRef: String,
        linkedDocumentID: String,
        generation: UInt64
    ) -> String {
        let material = [workspaceRef, accessRef, linkedDocumentID, String(generation)]
            .joined(separator: "\u{0}")
        let digest = SHA256.hash(data: Data(material.utf8)).map { String(format: "%02x", $0) }
        return "relink-" + digest.joined().prefix(32)
    }
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
    /// Direct-file only. A member's lifecycle is its root group's state, which
    /// `NativeRootAccessDescriptor` projects, so a member is always `.active`
    /// here and never gates on this field.
    public let lifecycle: NativeAccessLifecycle

    public init(
        accessRef: String,
        linkedDocumentID: String,
        generation: UInt64,
        rootBindingGeneration: UInt64?,
        state: NativeAccessState,
        route: NativeAccessRoute,
        lifecycle: NativeAccessLifecycle = .active
    ) {
        self.accessRef = accessRef
        self.linkedDocumentID = linkedDocumentID
        self.generation = generation
        self.rootBindingGeneration = rootBindingGeneration
        self.state = state
        self.route = route
        self.lifecycle = lifecycle
    }
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
    let lifecycle: NativeAccessLifecycle

    init(
        accessRef: String,
        linkedDocumentID: String,
        generation: UInt64,
        rootBindingGeneration: UInt64?,
        state: NativeAccessState,
        route: NativeAccessRoute,
        bookmark: Data?,
        lifecycle: NativeAccessLifecycle
    ) {
        self.accessRef = accessRef
        self.linkedDocumentID = linkedDocumentID
        self.generation = generation
        self.rootBindingGeneration = rootBindingGeneration
        self.state = state
        self.route = route
        self.bookmark = bookmark
        self.lifecycle = lifecycle
    }

    /// A record written before the lifecycle was recorded predates the
    /// distinction: whatever held it then was established work, so it reads back
    /// `active`. Every other field, bookmark bytes and generation included, is
    /// decoded exactly as written. An unknown *document* section still fails
    /// closed — see `NativeAccessDocument.init(from:)`.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        accessRef = try container.decode(String.self, forKey: .accessRef)
        linkedDocumentID = try container.decode(String.self, forKey: .linkedDocumentID)
        generation = try container.decode(UInt64.self, forKey: .generation)
        rootBindingGeneration = try container.decodeIfPresent(
            UInt64.self,
            forKey: .rootBindingGeneration
        )
        state = try container.decode(NativeAccessState.self, forKey: .state)
        route = try container.decode(NativeAccessRoute.self, forKey: .route)
        bookmark = try container.decodeIfPresent(Data.self, forKey: .bookmark)
        lifecycle = try container.decodeIfPresent(
            NativeAccessLifecycle.self,
            forKey: .lifecycle
        ) ?? .active
    }

    func with(lifecycle: NativeAccessLifecycle) -> NativeAccessRecord {
        NativeAccessRecord(
            accessRef: accessRef,
            linkedDocumentID: linkedDocumentID,
            generation: generation,
            rootBindingGeneration: rootBindingGeneration,
            state: state,
            route: route,
            bookmark: bookmark,
            lifecycle: lifecycle
        )
    }

    /// The same direct-file route at another generation, state and grant. The lifecycle is
    /// what owns the route and a relink does not change it.
    func with(generation: UInt64, state: NativeAccessState, bookmark: Data?) -> NativeAccessRecord {
        NativeAccessRecord(
            accessRef: accessRef,
            linkedDocumentID: linkedDocumentID,
            generation: generation,
            rootBindingGeneration: rootBindingGeneration,
            state: state,
            route: route,
            bookmark: bookmark,
            lifecycle: lifecycle
        )
    }
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
    /// Optional for the same reason as `pending`: a file written before source-level
    /// Disconnect reached Core has no such key, and that meant an empty set.
    let originalDisconnects: [OriginalDisconnectIntent]?
    /// Optional for the same reason: a file written before Locate reached Core has no such
    /// key, and that meant an empty set.
    let originalRelinks: [OriginalRelinkIntent]?

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schema
        case records
        case roots
        case pending
        case originalDisconnects
        case originalRelinks
    }

    init(
        schema: String,
        records: [NativeAccessRecord],
        roots: [NativeRootAccessRecord],
        pending: [PendingFolderSubmission]?,
        originalDisconnects: [OriginalDisconnectIntent]?,
        originalRelinks: [OriginalRelinkIntent]?
    ) {
        self.schema = schema
        self.records = records
        self.roots = roots
        self.pending = pending
        self.originalDisconnects = originalDisconnects
        self.originalRelinks = originalRelinks
    }

    init(from decoder: Decoder) throws {
        // `pending` was added to v3 without a new schema name, so a later build
        // can add a section of its own the same way. Reading such a document as
        // the subset this build understands would drop that section on the next
        // write, so it is refused instead and its bytes are left for the build
        // that wrote them. This is the document's own key set: a field added
        // inside a record is still read as a subset.
        try rejectUnknownKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schema = try container.decode(String.self, forKey: .schema)
        records = try container.decode([NativeAccessRecord].self, forKey: .records)
        roots = try container.decode([NativeRootAccessRecord].self, forKey: .roots)
        pending = try container.decodeIfPresent([PendingFolderSubmission].self, forKey: .pending)
        originalDisconnects = try container.decodeIfPresent(
            [OriginalDisconnectIntent].self,
            forKey: .originalDisconnects
        )
        originalRelinks = try container.decodeIfPresent(
            [OriginalRelinkIntent].self,
            forKey: .originalRelinks
        )
    }
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
    private var originalDisconnects: [String: OriginalDisconnectIntent]
    private var originalRelinks: [String: OriginalRelinkIntent]

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
        self.originalDisconnects = [:]
        self.originalRelinks = [:]
        try Self.prepareDirectory(stateDirectory)
        let loaded = try Self.load(from: stateFile, codec: codec)
        self.records = loaded.records
        self.roots = loaded.roots
        self.pending = loaded.pending
        self.originalDisconnects = loaded.disconnects
        self.originalRelinks = loaded.relinks
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
            // A route owed to Core for a relink is repaired by that relink and nothing else.
            if existing?.state == .relinking { throw NativeAccessError.relinkPending }
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
                bookmark: bookmark,
                // A newly adopted candidate is provisional: nothing published
                // owns it yet. A re-grant of a reference this store already
                // holds is a repair of that grant, not a new candidate, so it
                // keeps the lifecycle it had — which is what lets a relink of
                // established work serve original actions again.
                lifecycle: existing?.lifecycle ?? .provisional
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

    /// Promotes the direct-file grants that published work now owns.
    ///
    /// The counterpart of `activateRoot` for the route that has no root group.
    /// Its caller is the publication critical section
    /// (`CapturePreparationCoordinator.prepareAndPublish`), immediately after
    /// the queue commit that is the moment durable ownership starts to exist,
    /// and it names the grants the published entries are bound to — which are
    /// also the grants the watches registered beside them are bound to, because
    /// a watch is registered for the entry that froze its baseline. Nothing
    /// before that commit promotes anything, so a batch that fails or is
    /// discarded leaves every grant it adopted provisional.
    ///
    /// A member reference is passed over, because a member's lifecycle is its
    /// root group's and there is nothing of its own to promote. Everything else
    /// must be a connected direct grant: a reference this owner does not hold,
    /// or holds disconnected, means the active route the committed entry
    /// promises does not exist, and passing it over would leave published work
    /// permanently unable to serve Refresh/Open/Reveal with nothing on record
    /// to say why. It is refused instead, before anything is written, so the
    /// batch promotes either all of its grants or none of them and its caller
    /// reports an incomplete publication over the entries that did commit.
    public func activateGrants(accessRefs: [String]) throws {
        guard !accessRefs.isEmpty else { return }
        try gate.withLock {
            var replacement = records
            var promoted = false
            for accessRef in Set(accessRefs) {
                guard let current = replacement[accessRef] else {
                    throw NativeAccessError.unknownAccess
                }
                guard current.route == .directFile else { continue }
                guard current.state == .connected else {
                    throw NativeAccessError.disconnected
                }
                guard current.lifecycle == .provisional else { continue }
                replacement[accessRef] = current.with(lifecycle: .active)
                promoted = true
            }
            guard promoted else { return }
            try persist(records: replacement, roots: roots)
            records = replacement
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
                bookmark: nil,
                // Dropping the bytes is not a demotion: the lifecycle says what
                // owns this grant, and a relink repairs back into it.
                lifecycle: current.lifecycle
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

    /// The local half of a source-level Disconnect alone: routes revoked, nothing recorded for
    /// Core. Production uses `disconnectSource`, which makes this same revocation and the
    /// intent that tells Core in one durable write.
    func disconnect(linkedDocumentID: String) throws -> [NativeAccessDescriptor] {
        try Self.validateIdentifier(linkedDocumentID)
        return try gate.withLock {
            let matches = connectedRecords(of: linkedDocumentID)
            if matches.isEmpty { return [] }
            let revoked = Self.revoking(matches, records: records, roots: roots)
            try persist(records: revoked.records, roots: revoked.roots)
            records = revoked.records
            roots = revoked.roots
            return matches.map { Self.descriptor(revoked.records[$0.accessRef]!) }
        }
    }

    /// Design §13.3 steps 1 to 4 as one atomic decision. Every connected route of this
    /// logical source is marked disconnected and loses its bookmark (a shared root group
    /// keeps its own while another member or owner needs it), and a fixed intent for the
    /// canonical binding of each is recorded, all in one durable write under the same gate
    /// as final queue publication. A preparation that has not yet published therefore finds
    /// its route disconnected and cannot publish under the old generation, and there is no
    /// moment at which a route is revoked but nothing owes Core the news.
    ///
    /// Repeating it for a source with no connected route changes nothing and records
    /// nothing, so a second click cannot mint a second intent.
    ///
    /// `bindings` is the workspace and caller each route's work was frozen under, by access
    /// reference. A member's root group already records its own; `fallback` is used only for
    /// a route that has neither (a source that never published anything), because a
    /// tombstone Core records before the first capture still needs a workspace to live in.
    @discardableResult
    public func disconnectSource(
        linkedDocumentID: String,
        bindings: [String: NativeRootAccessBinding] = [:],
        fallback: NativeRootAccessBinding,
        now: Date = Date()
    ) throws -> [OriginalDisconnectIntent] {
        try Self.validateIdentifier(linkedDocumentID)
        return try gate.withLock {
            let matches = connectedRecords(of: linkedDocumentID)
            if matches.isEmpty { return [] }
            let revoked = Self.revoking(matches, records: records, roots: roots)
            let createdAt = ISO8601DateFormatter().string(from: now)
            var replacement = originalDisconnects
            var intents: [OriginalDisconnectIntent] = []
            for current in matches {
                let binding: NativeRootAccessBinding
                let routeKind: String
                let rootRef: String?
                let memberRef: String?
                switch current.route {
                case .directFile:
                    binding = bindings[current.accessRef] ?? fallback
                    (routeKind, rootRef, memberRef) = ("direct_file", nil, nil)
                case let .rootMember(root, member, _):
                    binding = roots[root]?.binding ?? bindings[current.accessRef] ?? fallback
                    (routeKind, rootRef, memberRef) = ("root_member", root, member)
                }
                let key = OriginalDisconnectIntent.operationKey(
                    workspaceRef: binding.workspaceRef,
                    accessRef: current.accessRef,
                    linkedDocumentID: current.linkedDocumentID,
                    generation: current.generation
                )
                let intent = replacement[key] ?? OriginalDisconnectIntent(
                    accessRef: current.accessRef,
                    linkedDocumentID: current.linkedDocumentID,
                    routeKind: routeKind,
                    rootRef: rootRef,
                    memberRef: memberRef,
                    generation: current.generation,
                    binding: binding,
                    operationKey: key,
                    createdAt: createdAt,
                    refusalCode: nil
                )
                replacement[key] = intent
                intents.append(intent)
            }
            try persist(
                records: revoked.records,
                roots: revoked.roots,
                originalDisconnects: replacement
            )
            records = revoked.records
            roots = revoked.roots
            originalDisconnects = replacement
            return intents
        }
    }

    /// The disconnect intents Core has not yet acknowledged, in a stable order. An intent
    /// Core refused for good is still listed, with its `refusalCode`, so that it is visible
    /// and not retried.
    public func pendingOriginalDisconnects() -> [OriginalDisconnectIntent] {
        gate.withLock { originalDisconnects.values.sorted { $0.operationKey < $1.operationKey } }
    }

    /// Core committed the disconnect, so nothing is owed any more. Repeating it, or naming an
    /// intent that is already gone, does nothing: an acknowledgement that is itself replayed
    /// is not an error.
    public func acknowledgeOriginalDisconnect(operationKey: String) throws {
        try gate.withLock {
            guard originalDisconnects[operationKey] != nil else { return }
            var replacement = originalDisconnects
            replacement[operationKey] = nil
            try persist(records: records, roots: roots, originalDisconnects: replacement)
            originalDisconnects = replacement
        }
    }

    /// Core refused the request and no retry can change that. The intent stays, marked.
    public func recordOriginalDisconnectRefusal(operationKey: String, code: String) throws {
        try gate.withLock {
            guard var intent = originalDisconnects[operationKey], intent.refusalCode != code else {
                return
            }
            intent.refusalCode = code
            var replacement = originalDisconnects
            replacement[operationKey] = intent
            try persist(records: records, roots: roots, originalDisconnects: replacement)
            originalDisconnects = replacement
        }
    }

    // MARK: - Locate (design §9.5)

    /// Design §9.5, on confirmation: reserve the route's next binding generation, drop the
    /// grant for the file being replaced, and record the fixed intent that tells Core, all
    /// in one durable write under the gate every final queue publication takes.
    ///
    /// From this write on, the route is `relinking`. A preparation that read the old file
    /// under the old generation finds the generation changed at its final validation and
    /// publishes nothing; one that starts now is refused before it reads. Neither the old
    /// grant nor the candidate's can read anything until Core has confirmed the generation
    /// and `completeOriginalRelink` activates the candidate.
    ///
    /// Only an established direct-file route can be relinked, and only one relink may be
    /// owed at a time: the person does not alternate between candidate originals. A relink
    /// Core refused for good is the one exception, because it is finished and has left the
    /// route paused with nothing else to try.
    ///
    /// `binding` is the workspace and caller the route's published work was frozen under.
    @discardableResult
    public func reserveRelink(
        accessRef: String,
        selectedURL: URL,
        binding: NativeRootAccessBinding,
        now: Date = Date()
    ) throws -> OriginalRelinkIntent {
        try Self.validateIdentifier(accessRef)
        try Self.validateSelection(selectedURL, for: .directFile)
        // Bookmark creation reads the file system, so it happens before the gate is taken.
        let candidate: Data
        do {
            candidate = try codec.makeReadOnlyBookmark(for: selectedURL)
        } catch {
            throw NativeAccessError.accessDenied
        }
        let name = String(selectedURL.lastPathComponent.replacingOccurrences(of: "\0", with: "").prefix(255))
        return try gate.withLock {
            guard let current = records[accessRef] else { throw NativeAccessError.unknownAccess }
            guard current.route == .directFile else { throw NativeAccessError.invalidRoute }
            switch current.state {
            case .disconnected:
                throw NativeAccessError.disconnected
            case .relinking:
                guard originalRelinks.values.contains(where: {
                    $0.accessRef == accessRef && $0.refusalCode != nil
                }) else { throw NativeAccessError.relinkPending }
            case .connected:
                break
            }
            // A grant adopted for a candidate nothing has published yet is not established
            // work (design §7.2), so there is no binding of Core's to move.
            guard current.lifecycle == .active else { throw NativeAccessError.routeNotActive }
            guard current.generation < UInt64(Int64.max) else { throw NativeAccessError.generationMismatch }
            let generation = current.generation + 1
            let key = OriginalRelinkIntent.operationKey(
                workspaceRef: binding.workspaceRef,
                accessRef: accessRef,
                linkedDocumentID: current.linkedDocumentID,
                generation: generation
            )
            let intent = OriginalRelinkIntent(
                accessRef: accessRef,
                linkedDocumentID: current.linkedDocumentID,
                generation: generation,
                binding: binding,
                operationKey: key,
                createdAt: ISO8601DateFormatter().string(from: now),
                candidateName: name.isEmpty ? "file" : name,
                candidateBookmark: candidate,
                refusalCode: nil
            )
            var replacementRelinks = originalRelinks.filter { $0.value.accessRef != accessRef }
            replacementRelinks[key] = intent
            var replacement = records
            replacement[accessRef] = current.with(generation: generation, state: .relinking, bookmark: nil)
            try persist(records: replacement, roots: roots, originalRelinks: replacementRelinks)
            records = replacement
            originalRelinks = replacementRelinks
            return intent
        }
    }

    /// The relink intents Core has not yet confirmed, in a stable order. One Core refused
    /// for good is still listed, with its `refusalCode`, so that it is visible and not resent.
    public func pendingOriginalRelinks() -> [OriginalRelinkIntent] {
        gate.withLock { originalRelinks.values.sorted { $0.operationKey < $1.operationKey } }
    }

    /// Whether a relink for this route still has to be settled with Core. A refused one is
    /// settled: nothing more will be sent for it.
    public func hasUnresolvedRelink(accessRef: String) -> Bool {
        gate.withLock {
            originalRelinks.values.contains { $0.accessRef == accessRef && $0.refusalCode == nil }
        }
    }

    /// Core confirmed the generation, so the confirmed candidate becomes the route's grant
    /// and the intent is retired, in one durable write. Repeating it, or naming an intent
    /// that is already gone, does nothing.
    ///
    /// A route that was disconnected while the relink was owed stays disconnected: the
    /// person's revocation outranks a confirmation that arrives afterwards, and the
    /// candidate's bookmark is dropped with the intent.
    @discardableResult
    public func completeOriginalRelink(operationKey: String) throws -> NativeAccessDescriptor? {
        try gate.withLock {
            guard let intent = originalRelinks[operationKey] else { return nil }
            var replacementRelinks = originalRelinks
            replacementRelinks[operationKey] = nil
            var replacement = records
            if let current = records[intent.accessRef],
               current.state == .relinking,
               current.generation == intent.generation
            {
                replacement[intent.accessRef] = current.with(
                    generation: intent.generation,
                    state: .connected,
                    bookmark: intent.candidateBookmark
                )
            }
            try persist(records: replacement, roots: roots, originalRelinks: replacementRelinks)
            records = replacement
            originalRelinks = replacementRelinks
            return replacement[intent.accessRef].map(Self.descriptor)
        }
    }

    /// Core refused the request and no retry can change that. The intent stays, marked, and
    /// the route stays paused.
    public func recordOriginalRelinkRefusal(operationKey: String, code: String) throws {
        try gate.withLock {
            guard var intent = originalRelinks[operationKey], intent.refusalCode != code else {
                return
            }
            intent.refusalCode = code
            var replacementRelinks = originalRelinks
            replacementRelinks[operationKey] = intent
            try persist(records: records, roots: roots, originalRelinks: replacementRelinks)
            originalRelinks = replacementRelinks
        }
    }

    /// The routes of one logical source that still hold, or are about to hold, a grant. A
    /// route in the middle of a relink counts: Disconnect revokes it, and the relink intent
    /// is settled with Core before the disconnect one is sent.
    ///
    /// Call inside the gate.
    private func connectedRecords(of linkedDocumentID: String) -> [NativeAccessRecord] {
        records.values
            .filter { $0.linkedDocumentID == linkedDocumentID && $0.state != .disconnected }
            .sorted { $0.accessRef < $1.accessRef }
    }

    private static func revoking(
        _ matches: [NativeAccessRecord],
        records: [String: NativeAccessRecord],
        roots: [String: NativeRootAccessRecord]
    ) -> (records: [String: NativeAccessRecord], roots: [String: NativeRootAccessRecord]) {
        var replacement = records
        for current in matches {
            replacement[current.accessRef] = NativeAccessRecord(
                accessRef: current.accessRef,
                linkedDocumentID: current.linkedDocumentID,
                generation: current.generation,
                rootBindingGeneration: current.rootBindingGeneration,
                state: .disconnected,
                route: current.route,
                bookmark: nil,
                lifecycle: current.lifecycle
            )
        }
        var replacementRoots = roots
        for current in matches {
            pruneUnusedRoot(for: current.route, records: replacement, roots: &replacementRoots)
        }
        return (replacement, replacementRoots)
    }

    /// Resolves one original behind a file or member route and hands `body` its
    /// URL.
    ///
    /// `purpose` is the broker operation being served. `.prepareCapture`, the
    /// default, is every submission, bulk-run and automatic read. `.originalAction`
    /// is an explicit Refresh, Open original or Reveal in Finder, and design §7.2
    /// gives those three no capability over a provisional route: a root group
    /// whose durable run or subscription does not exist yet, or a direct-file
    /// grant no published entry or registered watch owns yet. Either may serve
    /// only its own preparation.
    public func withReadAccess<T>(
        accessRef: String,
        expectedGeneration: UInt64,
        expectedRootBindingGeneration: UInt64? = nil,
        purpose: NativeAccessPurpose = .prepareCapture,
        _ body: (URL) throws -> T
    ) throws -> T {
        let resolvedAccess = try gate.withLock { () throws -> (NativeAccessRecord, Data) in
            guard let record = records[accessRef] else { throw NativeAccessError.unknownAccess }
            guard record.state != .relinking else { throw NativeAccessError.relinkPending }
            guard record.state == .connected else { throw NativeAccessError.disconnected }
            guard record.generation == expectedGeneration else {
                throw NativeAccessError.generationMismatch
            }
            switch record.route {
            case .directFile:
                guard expectedRootBindingGeneration == nil, let bookmark = record.bookmark else {
                    throw NativeAccessError.generationMismatch
                }
                // Design §7.2 for the route with no group: a grant adopted for a
                // candidate serves that candidate's own preparation, and serves
                // Refresh/Open/Reveal only once published work owns it.
                guard purpose == .prepareCapture || record.lifecycle == .active else {
                    throw NativeAccessError.routeNotActive
                }
                return (record, bookmark)
            case let .rootMember(rootRef, _, _):
                guard let root = roots[rootRef] else { throw NativeAccessError.generationMismatch }
                guard root.state.permitsResolution else { throw Self.refusal(root.state) }
                // Design §7.2: external broker Refresh/Open/Reveal requires an
                // active route. The member's own preparation is what a
                // provisional group exists to serve, so only the explicit user
                // actions are refused here.
                guard purpose == .prepareCapture || root.state == .active else {
                    throw NativeAccessError.routeNotActive
                }
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
                bookmark: nil,
                // A member's own lifecycle is its root group's state, which is
                // where its provisional period is recorded and gated.
                lifecycle: .active
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

    /// Abandons a child that was adopted for its first capture and then left the
    /// root before that capture published (design §21.8.2). The adoption is the
    /// only durable trace such a refusal leaves: no membership and no queue entry
    /// owns it, so the grant and its enrolment in the group go together, in one
    /// write, and the root is left as if the child had never been found.
    ///
    /// Everything the caller adopted under is named again and must still match
    /// exactly, so this can only ever remove that one unowned adoption: the
    /// generation it was given, the root generation it was bound to, and the whole
    /// locator. A direct-file route has no root to leave and is not expressible
    /// here. A record re-adopted, relinked or disconnected since carries another
    /// generation or state and is refused, as is a member of a frozen reviewed
    /// plan, whose enrolment that plan owns rather than this adoption. A mismatch
    /// — including the exact replay that finds the adoption already gone — changes
    /// nothing and reports `false` rather than failing the pass.
    @discardableResult
    public func abandonUnownedRootMember(
        accessRef: String,
        rootRef: String,
        memberRef: String,
        relativeComponents: [String],
        expectedGeneration: UInt64,
        expectedRootGeneration: UInt64
    ) throws -> Bool {
        try Self.validateIdentifier(accessRef)
        let route = NativeAccessRoute.rootMember(
            rootRef: rootRef,
            memberRef: memberRef,
            relativeComponents: relativeComponents
        )
        try Self.validate(route)
        return try gate.withLock {
            guard let record = records[accessRef],
                  record.route == route,
                  record.state == .connected,
                  record.bookmark == nil,
                  record.generation == expectedGeneration,
                  record.rootBindingGeneration == expectedRootGeneration,
                  let root = roots[rootRef],
                  root.generation == expectedRootGeneration,
                  !root.membersFrozen,
                  root.authority != .frozenMembersOnly
            else { return false }
            var replacement = records
            replacement[accessRef] = nil
            var replacementRoots = roots
            replacementRoots[rootRef] = root.with(
                memberRefs: root.memberRefs.filter { $0 != memberRef }
            )
            try persist(records: replacement, roots: replacementRoots)
            records = replacement
            roots = replacementRoots
            return true
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
                guard record.state != .relinking else { throw NativeAccessError.relinkPending }
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
                          let bookmark = root.bookmark
                    else { throw NativeAccessError.generationMismatch }
                    // The bytes were read while the member was inside this root,
                    // but publication is the commit, so containment is proved
                    // again here. A child that left the grant between the read
                    // and this moment publishes nothing through the root it has
                    // left (design §21.8.1, §21.8.2 "child moved outside its
                    // watched root"): the candidate is abandoned rather than
                    // committed against a locator that no longer resolves.
                    try resolveMemberTarget(record: record, rootBookmark: bookmark)
                }
            }
            return try body()
        }
    }

    /// Walks a member's locator under its root again and throws if it no longer
    /// resolves. Called while the gate is held, so it records nothing against
    /// the group: this is a check of one member, not a statement about the root.
    private func resolveMemberTarget(record: NativeAccessRecord, rootBookmark: Data) throws {
        guard let resolved = try? codec.resolveReadOnlyBookmark(rootBookmark),
              !resolved.stale
        else { throw NativeAccessError.accessDenied }
        guard codec.startAccessing(resolved.url) else { throw NativeAccessError.accessDenied }
        defer { codec.stopAccessing(resolved.url) }
        _ = try Self.targetURL(root: resolved.url, route: record.route)
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
        pending: [String: PendingFolderSubmission]? = nil,
        originalDisconnects: [String: OriginalDisconnectIntent]? = nil,
        originalRelinks: [String: OriginalRelinkIntent]? = nil
    ) throws {
        let document = NativeAccessDocument(
            schema: Self.schema,
            records: records.values.sorted { $0.accessRef < $1.accessRef },
            roots: roots.values.sorted { $0.rootRef < $1.rootRef },
            pending: (pending ?? self.pending).values
                .sorted { $0.submissionID < $1.submissionID },
            originalDisconnects: (originalDisconnects ?? self.originalDisconnects).values
                .sorted { $0.operationKey < $1.operationKey },
            originalRelinks: (originalRelinks ?? self.originalRelinks).values
                .sorted { $0.operationKey < $1.operationKey }
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
        disconnects: [String: OriginalDisconnectIntent],
        relinks: [String: OriginalRelinkIntent],
        migrated: Bool
    ) {
        guard FileManager.default.fileExists(atPath: stateFile.path) else {
            return ([:], [:], [:], [:], [:], false)
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
                    // A member's provisional period belongs to its root group,
                    // so a member record carrying one of its own is a document
                    // this build did not write and cannot interpret.
                    guard record.rootBindingGeneration != nil,
                          record.bookmark == nil,
                          record.lifecycle == .active
                    else {
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
            var loadedDisconnects: [String: OriginalDisconnectIntent] = [:]
            for intent in document.originalDisconnects ?? [] {
                try validate(intent)
                guard loadedDisconnects[intent.operationKey] == nil else {
                    throw NativeAccessError.invalidState
                }
                loadedDisconnects[intent.operationKey] = intent
            }
            var loadedRelinks: [String: OriginalRelinkIntent] = [:]
            for intent in document.originalRelinks ?? [] {
                try validate(intent)
                guard loadedRelinks[intent.operationKey] == nil,
                      !loadedRelinks.values.contains(where: { $0.accessRef == intent.accessRef })
                else { throw NativeAccessError.invalidState }
                loadedRelinks[intent.operationKey] = intent
            }
            // A route is `relinking` exactly while an intent owes Core the generation it
            // reserved. A record that says so without one could never be activated, and an
            // intent against a connected record would activate a second time.
            for record in result.values where record.state == .relinking {
                guard record.route == .directFile,
                      loadedRelinks.values.contains(where: {
                          $0.accessRef == record.accessRef && $0.generation == record.generation
                      })
                else { throw NativeAccessError.invalidState }
            }
            for intent in loadedRelinks.values {
                guard let record = result[intent.accessRef],
                      record.route == .directFile,
                      record.linkedDocumentID == intent.linkedDocumentID,
                      record.state != .connected,
                      record.generation == intent.generation
                else { throw NativeAccessError.invalidState }
            }
            return (result, loadedRoots, loadedPending, loadedDisconnects, loadedRelinks, false)
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
        disconnects: [String: OriginalDisconnectIntent],
        relinks: [String: OriginalRelinkIntent],
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
                    bookmark: old.bookmark,
                    // A v1 grant predates the distinction, so it is established
                    // work: it keeps its bytes, its generation and its reach.
                    lifecycle: .active
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
                    bookmark: nil,
                    lifecycle: .active
                )
            }
        }
        for record in records.values where record.state == .connected {
            guard case let .rootMember(rootRef, _, _) = record.route else { continue }
            guard roots[rootRef]?.state == .active else { throw NativeAccessError.invalidState }
        }
        // A v1 file predates durable establishment, so there is nothing pending.
        return (records, roots, [:], [:], [:], true)
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

    /// An intent is replayed under its key until Core answers, so a record that could not
    /// have been written by `disconnectSource` is refused rather than sent.
    private static func validate(_ intent: OriginalDisconnectIntent) throws {
        for identifier in [
            intent.accessRef, intent.linkedDocumentID, intent.operationKey,
            intent.binding.targetRef, intent.binding.workspaceRef,
            intent.binding.targetBindingRevision,
        ] + [intent.rootRef, intent.memberRef].compactMap({ $0 }) {
            try validateIdentifier(identifier)
        }
        let routed: Bool
        switch intent.routeKind {
        case "direct_file": routed = intent.rootRef == nil && intent.memberRef == nil
        case "root_member": routed = intent.rootRef != nil && intent.memberRef != nil
        default: routed = false
        }
        guard routed,
              intent.generation > 0,
              !intent.createdAt.isEmpty,
              intent.operationKey == OriginalDisconnectIntent.operationKey(
                  workspaceRef: intent.binding.workspaceRef,
                  accessRef: intent.accessRef,
                  linkedDocumentID: intent.linkedDocumentID,
                  generation: intent.generation
              )
        else { throw NativeAccessError.invalidState }
    }

    /// A relink intent is replayed under its key until Core answers, so a record that could
    /// not have been written by `reserveRelink` is refused rather than sent.
    private static func validate(_ intent: OriginalRelinkIntent) throws {
        for identifier in [
            intent.accessRef, intent.linkedDocumentID, intent.operationKey,
            intent.binding.targetRef, intent.binding.workspaceRef,
            intent.binding.targetBindingRevision,
        ] {
            try validateIdentifier(identifier)
        }
        guard intent.generation > 1,
              intent.generation <= UInt64(Int64.max),
              !intent.createdAt.isEmpty,
              !intent.candidateName.isEmpty,
              intent.candidateName.count <= 255,
              !intent.candidateName.contains("\0"),
              !intent.candidateBookmark.isEmpty,
              intent.operationKey == OriginalRelinkIntent.operationKey(
                  workspaceRef: intent.binding.workspaceRef,
                  accessRef: intent.accessRef,
                  linkedDocumentID: intent.linkedDocumentID,
                  generation: intent.generation
              )
        else { throw NativeAccessError.invalidState }
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
            route: record.route,
            lifecycle: record.lifecycle
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
