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

    public init(queue: CaptureIntentQueue, transport: any CaptureDeliveryTransport) {
        self.queue = queue
        self.transport = transport
    }

    @discardableResult
    public func deliverPending() async -> [CaptureQueueEntry] {
        var outcomes: [CaptureQueueEntry] = []
        for entry in queue.allEntries() where entry.state == .pending {
            // Refuse before any payload read, stage transition or transport
            // call: a binding the transport cannot honour leaves the item
            // pending for recovery or reconfiguration.
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
