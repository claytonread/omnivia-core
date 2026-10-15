import CryptoKit
import Foundation

public enum CaptureDeliveryOutcome: Equatable, Sendable {
    case admitted(captureID: String)
    case definiteFailure(code: String)
    case unknown
}

/// The target, workspace and caller facts a queued intent was frozen against.
/// A transport declares the binding it is actually configured to reach, and
/// delivery is refused unless the queued intent matches it exactly, so an item
/// queued for one workspace or target binding can never be delivered to
/// whichever one happens to be configured later.
public struct CaptureDeliveryBinding: Equatable, Sendable {
    private static let targetDomain = Data("omnivia.core.target.v1".utf8)
    /// Client context of this companion.
    public static let companionClientContextRef = "macos-companion"
    /// The runtime's configured local owner principal (`service/main.py`,
    /// `LOCAL_PRINCIPAL`); the only principal production authenticates for a
    /// local caller.
    public static let localPrincipalRef = "local-user"

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

    /// The binding a durable root group recorded, so work resumed from it is
    /// bound to exactly the destination it was approved for.
    public init(_ binding: NativeRootAccessBinding) {
        self.init(
            targetRef: binding.targetRef,
            workspaceRef: binding.workspaceRef,
            targetBindingRevision: binding.targetBindingRevision,
            callerContextBinding: binding.callerContextBinding
        )
    }

    /// The identity this watch froze when its consent was recorded. Comparing it
    /// with the authenticated binding is how an account or target-profile change
    /// is observed; it is never rewritten to whichever identity is current.
    public init(_ subscription: FileSubscription) {
        self.init(
            targetRef: subscription.targetRef,
            workspaceRef: subscription.workspaceRef,
            targetBindingRevision: subscription.targetBindingRevision,
            callerContextBinding: subscription.callerContextBinding
        )
    }

    /// The identity this watch froze when its consent was recorded; see the
    /// `FileSubscription` overload.
    public init(_ subscription: FolderSubscription) {
        self.init(
            targetRef: subscription.targetRef,
            workspaceRef: subscription.workspaceRef,
            targetBindingRevision: subscription.targetBindingRevision,
            callerContextBinding: subscription.callerContextBinding
        )
    }

    /// The single binding every native companion submission and the companion's
    /// own transport are built from.
    public static func companion(
        workspaceRef: String,
        targetRef: String = "local"
    ) -> CaptureDeliveryBinding {
        CaptureDeliveryBinding(
            targetRef: targetRef,
            workspaceRef: workspaceRef,
            targetBindingRevision: "1",
            callerContextBinding: CallerContextBinding(
                clientContextRef: companionClientContextRef,
                principalRef: localPrincipalRef,
                contextRevision: "1"
            )
        )
    }

    /// The exact target identity published by the CLI for an explicitly
    /// selected installation/workspace. Keep this derivation byte-for-byte in
    /// step with `omnivia_core_cli.safe_status.resolve_local_target`; it does
    /// not inspect ambient state or substitute whichever target is current.
    public static func installedCompanion(
        installationState: URL,
        workspaceRef: String
    ) -> CaptureDeliveryBinding {
        let selection = installationState.resolvingSymlinksInPath().standardizedFileURL.path
        var material = targetDomain
        material.append(0)
        material.append(contentsOf: selection.utf8)
        material.append(0)
        material.append(contentsOf: workspaceRef.utf8)
        let suffix = SHA256.hash(data: material).prefix(16).map {
            String(format: "%02x", $0)
        }.joined()
        return companion(
            workspaceRef: workspaceRef,
            targetRef: "core-target-\(suffix)"
        )
    }

    public func matches(_ intent: CaptureIntentV3) -> Bool {
        intent.targetRef == targetRef
            && intent.workspaceRef == workspaceRef
            && intent.targetBindingRevision == targetBindingRevision
            && intent.callerContextBinding == callerContextBinding
    }
}

public protocol CaptureDeliveryTransport: Sendable {
    var descriptorDigest: String { get }
    /// The binding this transport is trusted to deliver to.
    var deliveryBinding: CaptureDeliveryBinding { get }
    func deliver(input: Data, payloadURL: URL, operationKey: String) async -> CaptureDeliveryOutcome
}

public enum CaptureDeliveryError: Error, Equatable {
    case invalidIntent
    case missingPayload
}

/// What a changed signed-in principal or target profile has paused on this Mac
/// (design §10.2, §10.3 "Target/principal identity changed", §10.4 and §21.9.1).
///
/// A frozen intent is never rewritten, redirected or replayed under the new
/// account. It pauses: its bytes, its stable key and its whole attempt history
/// stay exactly as they are, nothing is sent, and it waits for the owning setup
/// flow to restore the identity it was authorised for. Automatic subscriptions
/// frozen against that identity pause durably, which is what leaves them armed
/// for no further automatic read at all.
public struct CaptureIdentityPause: Equatable, Sendable {
    /// Unresolved queued intents — queued, in flight and awaiting recovery
    /// alike — whose frozen identity is not the authenticated one. This is the
    /// paused state of the intent itself: it is derived from the frozen binding
    /// and the identity now authenticated, so it cannot survive as a stale flag
    /// once the owning setup flow restores that identity.
    public let pausedItemIDs: [String]
    /// Watches moved to `FolderSubscriptionState.paused` by this pass.
    public let pausedFileSubscriptionIDs: [String]
    public let pausedFolderSubscriptionIDs: [String]

    public var isEmpty: Bool {
        pausedItemIDs.isEmpty
            && pausedFileSubscriptionIDs.isEmpty
            && pausedFolderSubscriptionIDs.isEmpty
    }
}

public struct DocumentCaptureWireEncoder {
    public init() {}

    public func encode(entry: CaptureQueueEntry) throws -> Data {
        let intent = entry.intent
        let base: [String: Any]
        switch intent.basePrecondition {
        case .create:
            base = ["kind": "creation"]
        case let .expected(captureID):
            var value: [String: Any] = ["kind": "existing"]
            if let captureID { value["capture_id"] = captureID }
            base = value
        }
        var input: [String: Any] = [
            "document_id": intent.originalBinding?.linkedDocumentId ?? intent.itemId,
            "media_type": Self.mediaType(intent.contentKind, displayName: intent.displayName),
            "requested_source_identity": intent.requestedSourceIdentity,
            "intent_kind": intent.intentKind.rawValue,
            "base": base,
            "target_binding_revision": intent.targetBindingRevision,
            "processing_profile": [
                "profile_ref": intent.processingProfile.profileRef,
                "revision": intent.processingProfile.revision,
            ],
        ]
        if let binding = intent.originalBinding {
            var original: [String: Any] = [
                "owner_ref": binding.accessRecordId,
                "generation": binding.bindingGeneration,
                "route_kind": binding.rootAccessGroupId == nil ? "direct_file" : "root_member",
            ]
            if let root = binding.rootAccessGroupId { original["root_ref"] = root }
            if let member = binding.memberId { original["member_ref"] = member }
            input["original_binding"] = original
        }
        if let consent = intent.consent {
            input["consent"] = [
                "subscription_id": consent.subscriptionId,
                "consent_generation": consent.consentGeneration,
                "config_generation": consent.configGeneration,
                "dirty_epoch": consent.dirtyEpoch,
            ]
        }
        if let origin = intent.bulkOrigin {
            input["bulk_origin"] = [
                "bulk_run_id": origin.bulkRunId,
                "manifest_revision": origin.manifestRevision,
                "member_id": origin.memberId,
            ]
        }
        if let displayName = intent.displayName { input["display_name"] = displayName }
        guard JSONSerialization.isValidJSONObject(input) else { throw CaptureDeliveryError.invalidIntent }
        return try JSONSerialization.data(withJSONObject: input, options: [.sortedKeys])
    }

    private static func mediaType(_ kind: CaptureContentKind, displayName: String?) -> String {
        switch kind {
        case .text: "text/plain"
        case .markdown: "text/markdown"
        case .link: "text/uri-list"
        case .pdf: "application/pdf"
        case .docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        case .xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        case .pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        case .csv: "text/csv"
        case .tsv: "text/tab-separated-values"
        case .rtf: "application/rtf"
        case .html: "text/html"
        case .image:
            switch URL(fileURLWithPath: displayName ?? "").pathExtension.lowercased() {
            case "png": "image/png"
            case "heic": "image/heic"
            case "tif", "tiff": "image/tiff"
            case "webp": "image/webp"
            default: "image/jpeg"
            }
        }
    }
}

public actor CaptureDeliveryCoordinator {
    private let queue: CaptureIntentQueue
    private let transport: any CaptureDeliveryTransport
    private let encoder = DocumentCaptureWireEncoder()
    /// The watches whose consent froze an identity. Present so an account or
    /// target-profile change pauses them where it is observed, rather than being
    /// noticed only at the send boundary; a standalone coordinator has neither.
    private let fileSubscriptions: FileSubscriptionStore?
    private let folderSubscriptions: FolderSubscriptionStore?
    /// Shared with the other publication owners; see `PublicationGate`. Held for
    /// the whole of one identity pass, so the queue snapshot the pass judges and
    /// the watches it pauses cannot be interleaved by a publication.
    private let gate: PublicationGate

    public init(
        queue: CaptureIntentQueue,
        transport: any CaptureDeliveryTransport,
        fileSubscriptions: FileSubscriptionStore? = nil,
        folderSubscriptions: FolderSubscriptionStore? = nil,
        gate: PublicationGate = .processShared
    ) {
        self.queue = queue
        self.transport = transport
        self.fileSubscriptions = fileSubscriptions
        self.folderSubscriptions = folderSubscriptions
        self.gate = gate
    }

    /// Pauses everything the authenticated identity no longer authorises, and
    /// names it.
    ///
    /// The transport's own binding is the identity this companion is actually
    /// configured and authenticated for. Design §10.2 requires that comparison
    /// before the first possible send and before every recovery attempt, and
    /// §21.9.1 makes the same changed identity pause subscriptions and existing
    /// pending delivery rather than redirect either. So one pass does both:
    ///
    /// - every watch frozen against a different principal, target, target
    ///   binding revision or workspace moves to `paused` durably, which is the
    ///   state the monitors, the reconciliation schedule and its executor all
    ///   require to be `active` before they arm or run any automatic read;
    /// - every unresolved queued intent frozen against a different identity is
    ///   reported paused, and `deliverPending` sends none of them.
    ///
    /// Nothing is rewritten, retargeted or deleted, and a watch already stopped
    /// or paused is left alone. A watch still `review_required` is left alone
    /// too: it arms no automatic read while it waits for its owner, and moving
    /// it would discard the pending review signature that owning flow needs.
    ///
    /// Call it at the start of every pass that may read a watched source, not
    /// only before delivery: an identity change observed between two passes must
    /// not let one more automatic read through.
    ///
    /// It throws when a pause it owes could not be made durable, because the
    /// watch is then still `active` and the automatic read paths will still run
    /// it. A caller that was about to read a watched source must abandon that
    /// pass on a throw rather than treat the pause as done; delivery is
    /// unaffected either way, since its own send boundary refuses the mismatch
    /// whatever this pass managed to persist.
    @discardableResult
    public func pauseWorkUnderChangedIdentity() throws -> CaptureIdentityPause {
        let authenticated = transport.deliveryBinding
        return try gate.withLock {
            var files: [String] = []
            for value in fileSubscriptions?.all() ?? []
                where Self.pausable(value.state) && CaptureDeliveryBinding(value) != authenticated
            {
                try fileSubscriptions?.update(id: value.subscriptionID) {
                    $0.state = .paused
                    $0.needsReconciliation = true
                }
                files.append(value.subscriptionID)
            }
            var folders: [String] = []
            for value in folderSubscriptions?.all() ?? []
                where Self.pausable(value.state) && CaptureDeliveryBinding(value) != authenticated
            {
                try folderSubscriptions?.update(id: value.subscriptionID) {
                    $0.state = .paused
                    $0.needsFullReconciliation = true
                }
                folders.append(value.subscriptionID)
            }
            return CaptureIdentityPause(
                pausedItemIDs: queue.allEntries().filter {
                    [.pending, .staging, .unknownOutcome].contains($0.state)
                        && !authenticated.matches($0.intent)
                }.map(\.intent.itemId).sorted(),
                pausedFileSubscriptionIDs: files,
                pausedFolderSubscriptionIDs: folders
            )
        }
    }

    /// Automatic work is armed from `active`, and both `waiting_for_core` and
    /// `needs_access` can still reach it without a new reviewed consent, so all
    /// three pause. This is the transition `FileMonitoringCoordinator.pause` and
    /// `FolderMonitoringCoordinator.pause` make for an owner who asks.
    private static func pausable(_ state: FolderSubscriptionState) -> Bool {
        state == .active || state == .waitingForCore || state == .needsAccess
    }

    @discardableResult
    public func deliverPending() async -> [CaptureQueueEntry] {
        // A pause this cannot persist does not stop delivery: the send boundary
        // below refuses the same mismatch on its own, and the pass is owed again
        // on the next tick.
        try? pauseWorkUnderChangedIdentity()
        var outcomes: [CaptureQueueEntry] = []
        for entry in queue.allEntries() where entry.state == .pending {
            // The same comparison the pass above reported, applied again here as
            // the send boundary itself: paused before any payload read, stage
            // transition or transport call, including for an intent published
            // since that pass. The frozen intent is left exactly as it is for
            // the owning setup flow, and the current account is never
            // substituted for the one it was authorised for.
            guard transport.deliveryBinding.matches(entry.intent) else { continue }
            do {
                let payloadURL = try queue.payloadURL(for: entry.intent.itemId)
                let attributes = try FileManager.default.attributesOfItem(atPath: payloadURL.path)
                guard (attributes[.size] as? NSNumber)?.uint64Value == entry.intent.byteLength,
                      try Self.digestPayload(at: payloadURL) == entry.intent.localIntegrityDigest
                else {
                    throw CaptureDeliveryError.missingPayload
                }
                let input = try encoder.encode(entry: entry)
                let requestDigest = Self.digest(
                    input,
                    payloadDigest: entry.intent.localIntegrityDigest
                )
                let staging = try queue.beginStage(
                    itemID: entry.intent.itemId,
                    requestDigest: requestDigest,
                    descriptorDigest: transport.descriptorDigest
                )
                guard let attempt = staging.attempts.last,
                      attempt.generation == staging.attemptGeneration,
                      attempt.disposition == .active
                else { throw CaptureDeliveryError.invalidIntent }
                switch await transport.deliver(
                    input: input,
                    payloadURL: payloadURL,
                    operationKey: attempt.operationKey
                ) {
                case let .admitted(captureID):
                    outcomes.append(try queue.markAdmitted(
                        itemID: entry.intent.itemId,
                        attemptGeneration: staging.attemptGeneration,
                        captureID: captureID
                    ))
                case let .definiteFailure(code):
                    outcomes.append(try queue.markFailed(itemID: entry.intent.itemId, failureCode: code))
                case .unknown:
                    outcomes.append(try queue.markUnknownOutcome(
                        itemID: entry.intent.itemId,
                        attemptGeneration: staging.attemptGeneration
                    ))
                }
            } catch {
                if let current = queue.allEntries().first(where: { $0.intent.itemId == entry.intent.itemId }),
                   current.state == .staging,
                   let changed = try? queue.markUnknownOutcome(
                       itemID: current.intent.itemId,
                       attemptGeneration: current.attemptGeneration
                   ) {
                    outcomes.append(changed)
                } else if let failed = try? queue.markFailed(
                    itemID: entry.intent.itemId,
                    failureCode: "local_delivery_error"
                ) {
                    outcomes.append(failed)
                }
            }
        }
        return outcomes
    }

    private static func digest(_ data: Data, payloadDigest: String) -> String {
        var bound = data
        bound.append(0)
        bound.append(contentsOf: payloadDigest.utf8)
        return "sha256:" + SHA256.hash(data: bound).map { String(format: "%02x", $0) }.joined()
    }

    private static func digestPayload(at url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let chunk = try handle.read(upToCount: 1024 * 1024), !chunk.isEmpty {
            hasher.update(data: chunk)
        }
        return "sha256:" + hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
