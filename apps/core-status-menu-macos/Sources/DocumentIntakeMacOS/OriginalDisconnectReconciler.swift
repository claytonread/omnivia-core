import Foundation

/// What one `document.original.disconnect` attempt established.
public enum OriginalDisconnectOutcome: Equatable, Sendable {
    /// Core answered that the binding is disconnected: it committed this request just now,
    /// or replayed the stored answer to an earlier commit of the same key. The two are the
    /// same answer, which is what makes a lost acknowledgement recoverable.
    case acknowledged(documentID: String, ownerRef: String)
    case definiteFailure(code: String)
    /// Nothing reached Core (it is absent, stopped or could not be started), so the attempt
    /// provably had no effect.
    case notSent
    /// Core refused before acting and only the person can change why (access, an update).
    case held(CaptureQueuePause)
    /// The request may have reached Core and its answer is not known.
    case unknown
}

public protocol OriginalDisconnectTransport: Sendable {
    /// The binding this transport is authenticated for. An intent is sent only when it is
    /// exactly the one the intent froze.
    var deliveryBinding: CaptureDeliveryBinding { get }
    func deliver(input: Data, operationKey: String) async -> OriginalDisconnectOutcome
}

/// The request body for one intent. It is a pure function of the intent, so every attempt
/// under its key carries the same bytes; Core refuses a key reused with a different body.
public struct OriginalDisconnectWireEncoder {
    public init() {}

    public func encode(_ intent: OriginalDisconnectIntent) throws -> Data {
        var input: [String: Any] = [
            "document_id": intent.linkedDocumentID,
            "owner_ref": intent.accessRef,
            "route_kind": intent.routeKind,
        ]
        if let root = intent.rootRef { input["root_ref"] = root }
        if let member = intent.memberRef { input["member_ref"] = member }
        guard JSONSerialization.isValidJSONObject(input) else { throw CaptureDeliveryError.invalidIntent }
        return try JSONSerialization.data(withJSONObject: input, options: [.sortedKeys])
    }
}

public enum OriginalDisconnectSettlement: Equatable, Sendable {
    /// Core has the disconnect; the intent is retired.
    case acknowledged(operationKey: String)
    /// Core refused for good; the intent is kept, marked, and not sent again.
    case refused(operationKey: String, code: String)
    /// Still owed, under the same key: Core's answer is unknown, it was not reached, it is
    /// held for the person, or this Mac is authenticated for another identity.
    case owed(operationKey: String)
}

/// Design §13.3 steps 4 and 5: reconcile each recorded disconnect intent with Core under
/// its stable key.
///
/// It sends exactly what the intent froze, with the intent's one key, as often as it takes.
/// An unknown answer is the ordinary case, not an error: Core may have committed, and the
/// replay of the same key returns the stored answer instead of acting twice. Nothing here
/// allocates a key, rewrites an intent, reads an original or restores a grant, and none of
/// it is needed for the local revocation to hold: Core being offline, refusing access or
/// losing the acknowledgement leaves the source revoked on this Mac.
public actor OriginalDisconnectReconciler {
    /// Core's refusals that a retry of the same request cannot change.
    /// ponytail: every other failure is retried on every pass; add backoff if the pass
    /// cadence (the delivery tick) ever makes that noisy.
    static let permanentRefusals: Set<String> = ["conflict", "invalid_request", "refused_before_send"]

    private let accessStore: NativeAccessStore
    private let transport: any OriginalDisconnectTransport
    private let encoder = OriginalDisconnectWireEncoder()

    public init(accessStore: NativeAccessStore, transport: any OriginalDisconnectTransport) {
        self.accessStore = accessStore
        self.transport = transport
    }

    @discardableResult
    public func reconcilePending() async -> [OriginalDisconnectSettlement] {
        var settlements: [OriginalDisconnectSettlement] = []
        for intent in accessStore.pendingOriginalDisconnects() where intent.refusalCode == nil {
            let key = intent.operationKey
            // A disconnect names no generation: Core tombstones whichever one it holds. A
            // relink of the same route that Core has not yet answered must therefore land
            // first, or this would tombstone the old generation and the relink would then
            // reconnect the route the person just revoked.
            guard !accessStore.hasUnresolvedRelink(accessRef: intent.accessRef) else {
                settlements.append(.owed(operationKey: key))
                continue
            }
            // A switch to another account or target pauses the intent where it stands. The
            // frozen binding is never rewritten to the current one.
            guard transport.deliveryBinding == CaptureDeliveryBinding(intent.binding),
                  let input = try? encoder.encode(intent)
            else {
                settlements.append(.owed(operationKey: key))
                continue
            }
            switch await transport.deliver(input: input, operationKey: key) {
            case let .acknowledged(documentID, ownerRef):
                // An answer about some other binding proves nothing about this one.
                guard documentID == intent.linkedDocumentID, ownerRef == intent.accessRef,
                      (try? accessStore.acknowledgeOriginalDisconnect(operationKey: key)) != nil
                else {
                    settlements.append(.owed(operationKey: key))
                    continue
                }
                settlements.append(.acknowledged(operationKey: key))
            case let .definiteFailure(code) where Self.permanentRefusals.contains(code):
                _ = try? accessStore.recordOriginalDisconnectRefusal(operationKey: key, code: code)
                settlements.append(.refused(operationKey: key, code: code))
            case .definiteFailure, .notSent, .held, .unknown:
                settlements.append(.owed(operationKey: key))
            }
        }
        return settlements
    }
}
