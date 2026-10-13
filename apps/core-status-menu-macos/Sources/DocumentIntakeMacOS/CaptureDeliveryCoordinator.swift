import CryptoKit
import Foundation

public enum CaptureDeliveryOutcome: Equatable, Sendable {
    case admitted(captureID: String)
    case definiteFailure(code: String)
    case unknown
}

public protocol CaptureDeliveryTransport: Sendable {
    var descriptorDigest: String { get }
    func deliver(input: Data, operationKey: String) async -> CaptureDeliveryOutcome
}

public enum CaptureDeliveryError: Error, Equatable {
    case invalidIntent
    case missingPayload
}

public struct DocumentCaptureWireEncoder {
    public init() {}

    public func encode(entry: CaptureQueueEntry, payload: Data) throws -> Data {
        guard payload.count == entry.intent.byteLength else { throw CaptureDeliveryError.missingPayload }
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
            "content_base64": payload.base64EncodedString(),
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
            do {
                let payload = try Data(contentsOf: queue.payloadURL(for: entry.intent.itemId))
                let input = try encoder.encode(entry: entry, payload: payload)
                let requestDigest = Self.digest(input)
                let staging = try queue.beginStage(
                    itemID: entry.intent.itemId,
                    requestDigest: requestDigest,
                    descriptorDigest: transport.descriptorDigest
                )
                switch await transport.deliver(input: input, operationKey: entry.intent.operationKey) {
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

    private static func digest(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
