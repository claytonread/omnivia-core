import Foundation

/// What one `document.original.relink` attempt established.
public enum OriginalRelinkOutcome: Equatable, Sendable {
    /// Core answered that the binding is connected at `bindingGeneration`: it committed this
    /// request just now, or replayed the stored answer to an earlier commit of the same key.
    /// The two are the same answer, which is what makes a lost acknowledgement recoverable.
    /// `currentCaptureID` is the document's head as Core held it when the relink committed.
    case acknowledged(
        documentID: String,
        ownerRef: String,
        bindingGeneration: UInt64,
        currentCaptureID: String?
    )
    case definiteFailure(code: String)
    /// Nothing reached Core (it is absent, stopped or could not be started), so the attempt
    /// provably had no effect.
    case notSent
    /// Core refused before acting and only the person can change why (access, an update).
    case held(CaptureQueuePause)
    /// The request may have reached Core and its answer is not known.
    case unknown
}

public protocol OriginalRelinkTransport: Sendable {
    /// The binding this transport is authenticated for. An intent is sent only when it is
    /// exactly the one the intent froze.
    var deliveryBinding: CaptureDeliveryBinding { get }
    func deliverRelink(input: Data, operationKey: String) async -> OriginalRelinkOutcome
}

/// The request body for one intent. It is a pure function of the intent, so every attempt
/// under its key carries the same bytes; Core refuses a key reused with a different body.
/// It names an owner, a document and a generation, and never a path, URL or bookmark.
public struct OriginalRelinkWireEncoder {
    public init() {}

    public func encode(_ intent: OriginalRelinkIntent) throws -> Data {
        let input: [String: Any] = [
            "document_id": intent.linkedDocumentID,
            "owner_ref": intent.accessRef,
            "binding_generation": intent.generation,
        ]
        guard JSONSerialization.isValidJSONObject(input) else { throw CaptureDeliveryError.invalidIntent }
        return try JSONSerialization.data(withJSONObject: input, options: [.sortedKeys])
    }
}

public enum OriginalRelinkSettlement: Equatable, Sendable {
    /// Core has the new generation; the candidate is the route's grant and the intent is retired.
    case acknowledged(operationKey: String)
    /// Core refused for good; the intent is kept, marked, and not sent again.
    case refused(operationKey: String, code: String)
    /// Still owed, under the same key: Core's answer is unknown, it was not reached, it is
    /// held for the person, or this Mac is authenticated for another identity.
    case owed(operationKey: String)
}

/// Design §9.5: reconcile each recorded relink intent with Core under its stable key, and
/// activate the new grant only on Core's canonical confirmation.
///
/// It sends exactly what the intent froze, with the intent's one key, as often as it takes.
/// An unknown answer is the ordinary case, not an error: Core may have committed, and the
/// replay of the same key returns the stored answer instead of acting twice. Nothing here
/// allocates a key, rewrites an intent, reads either file or activates a grant on anything
/// short of an answer that names this owner, this document and exactly the generation the
/// intent reserved. Core being offline, refusing access or losing the acknowledgement
/// leaves the route paused with the old grant gone and the new one not yet active.
///
/// The watches of the route learn the new generation (and Core's head) before the grant is activated, in the
/// same critical section on the publication gate, so no read can pair the new grant with the
/// old generation. They stay paused: resuming is the person's explicit decision (§9.5), and
/// nothing here reads the selected version.
public actor OriginalRelinkReconciler {
    /// Core's refusals that a retry of the same request cannot change.
    /// ponytail: every other failure is retried on every pass; add backoff if the pass
    /// cadence (the delivery tick) ever makes that noisy.
    static let permanentRefusals: Set<String> = ["conflict", "invalid_request", "refused_before_send"]

    private let accessStore: NativeAccessStore
    private let subscriptions: FileSubscriptionStore
    private let gate: PublicationGate
    private let transport: any OriginalRelinkTransport
    private let encoder = OriginalRelinkWireEncoder()

    public init(
        accessStore: NativeAccessStore,
        subscriptions: FileSubscriptionStore,
        gate: PublicationGate,
        transport: any OriginalRelinkTransport
    ) {
        self.accessStore = accessStore
        self.subscriptions = subscriptions
        self.gate = gate
        self.transport = transport
    }

    @discardableResult
    public func reconcilePending() async -> [OriginalRelinkSettlement] {
        var settlements: [OriginalRelinkSettlement] = []
        for intent in accessStore.pendingOriginalRelinks() where intent.refusalCode == nil {
            let key = intent.operationKey
            // A switch to another account or target pauses the intent where it stands. The
            // frozen binding is never rewritten to the current one.
            guard transport.deliveryBinding == CaptureDeliveryBinding(intent.binding),
                  let input = try? encoder.encode(intent)
            else {
                settlements.append(.owed(operationKey: key))
                continue
            }
            switch await transport.deliverRelink(input: input, operationKey: key) {
            case let .acknowledged(documentID, ownerRef, generation, currentCaptureID):
                // An answer about some other binding, or some other generation, proves
                // nothing about this one.
                guard documentID == intent.linkedDocumentID,
                      ownerRef == intent.accessRef,
                      generation == intent.generation,
                      activate(intent, currentCaptureID: currentCaptureID)
                else {
                    settlements.append(.owed(operationKey: key))
                    continue
                }
                settlements.append(.acknowledged(operationKey: key))
            case let .definiteFailure(code) where Self.permanentRefusals.contains(code):
                _ = try? accessStore.recordOriginalRelinkRefusal(operationKey: key, code: code)
                settlements.append(.refused(operationKey: key, code: code))
            case .definiteFailure, .notSent, .held, .unknown:
                settlements.append(.owed(operationKey: key))
            }
        }
        return settlements
    }

    /// Both writes are idempotent absolute values, so a stop between them is repaired by the
    /// next pass resending the same key and applying them again.
    private func activate(_ intent: OriginalRelinkIntent, currentCaptureID: String?) -> Bool {
        do {
            try gate.withLock {
                for watch in subscriptions.all()
                where watch.accessRef == intent.accessRef && watch.bindingGeneration < intent.generation
                {
                    // The watch moves to the new generation and is re-based on Core's own head:
                    // a refresh published before the Locate may be admitted as history, and
                    // history is never the base of the next refresh.
                    _ = try subscriptions.update(id: watch.subscriptionID) {
                        $0.bindingGeneration = intent.generation
                        if let currentCaptureID { $0.currentSuccessfulCaptureID = currentCaptureID }
                        // Normally already paused by the confirmation that reserved the
                        // generation; a stop between that write and the pause leaves the
                        // watch running, and automatic updates stay paused after a relink
                        // whichever way it got here.
                        if $0.state == .active || $0.state == .needsAccess {
                            $0.state = .paused
                            $0.needsReconciliation = true
                        }
                    }
                }
                _ = try accessStore.completeOriginalRelink(operationKey: intent.operationKey)
            }
            return true
        } catch {
            return false
        }
    }
}
