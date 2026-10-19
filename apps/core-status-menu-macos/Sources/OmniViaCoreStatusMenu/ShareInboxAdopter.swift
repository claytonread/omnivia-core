import DocumentIntakeMacOS
import Foundation

/// A step of an inbox adoption, reported immediately after its durable effect.
/// Production leaves the hook empty; tests throw from it to interrupt an
/// adoption at exactly that boundary.
enum ShareInboxStep: Equatable {
    /// Proven copies of every byte exist in the companion's own storage. The
    /// published request is still in the inbox.
    case staged(requestID: String)
    /// The copies are in review.
    case presented(requestID: String)
    /// The inbox no longer holds the request: the adoption is acknowledged.
    case acknowledged(requestID: String)
}

struct ShareInboxReconciliation: Equatable {
    /// Requests whose bytes this companion now owns and has shown for review.
    var adopted: [String] = []
    /// Requests whose manifest this build does not understand. Left exactly as
    /// they are so a newer build can still adopt them.
    var preserved: [String] = []
    /// Requests whose bytes did not match what their own manifest bound, so
    /// nothing was shown and the request was dropped.
    var rejected: [String] = []
    /// Requests that could not be completed this pass and are retried.
    var retried: [String] = []
    var chooserOpened = false
}

/// Adopts durable Share requests into review.
///
/// This is the companion-closed path. A share writes a complete envelope into
/// the App Group spool and may ask once for this companion to come forward;
/// whether it does or not, the request waits. Adoption then:
///
/// 1. copies every one of the request's bytes into the companion's own owned
///    snapshot storage and proves each copy against the manifest's length and
///    digest;
/// 2. hands the copies to the same review coordinator every other intake
///    surface uses — never to Core, and never to the queue;
/// 3. drops the inbox request, which is the acknowledgement.
///
/// Step 1 is idempotent: the owned copies are rewritten under the request's own
/// id. So a lost acknowledgement re-copies the same bytes and lands on the same
/// candidate key review already holds, rather than producing a second
/// candidate, and a crash before the acknowledgement re-adopts on the next
/// start rather than losing the share. Nothing is added or watched here.
@MainActor
final class ShareInboxAdopter {
    private let inbox: ShareInboxStore
    private let snapshotStore: OwnedSnapshotStore
    private let accept: ([ManualCaptureCandidate]) -> Bool
    private let openChooser: () -> Void
    private var terminating = false

    /// Failure-injection seam: see `ShareInboxStep`.
    var checkpoint: (ShareInboxStep) throws -> Void = { _ in }

    init(
        inbox: ShareInboxStore,
        snapshotStore: OwnedSnapshotStore,
        openChooser: @escaping () -> Void,
        accept: @escaping ([ManualCaptureCandidate]) -> Bool
    ) {
        self.inbox = inbox
        self.snapshotStore = snapshotStore
        self.openChooser = openChooser
        self.accept = accept
    }

    /// Carries every durable Share request forward, oldest first, and clears
    /// staging directories no live share can still be writing. Safe to call on
    /// every start and on every tick: an adoption that cannot complete is left
    /// for the next pass.
    @discardableResult
    func reconcile(now: Date = Date()) -> ShareInboxReconciliation {
        var outcome = ShareInboxReconciliation()
        guard !terminating else { return outcome }
        inbox.reconcile(now: now)
        if inbox.takeChooserRequest() {
            outcome.chooserOpened = true
            openChooser()
        }
        for requestID in inbox.pendingRequestIDs() {
            guard let envelope = inbox.envelope(requestID: requestID) else {
                outcome.preserved.append(requestID)
                continue
            }
            do {
                if try adopt(envelope) {
                    outcome.adopted.append(requestID)
                } else {
                    outcome.rejected.append(requestID)
                }
            } catch {
                outcome.retried.append(requestID)
            }
            // One bounded selection per pass. Copying a selection is
            // synchronous work on the same actor that draws review, so a spool
            // that filled up while the companion was closed drains over
            // several ticks rather than freezing the first one.
            break
        }
        return outcome
    }

    /// Refuses new adoptions. Review lives in this process, so a request that
    /// has not been shown is better left durable than owned by nobody.
    func companionWillTerminate() {
        terminating = true
    }

    /// True once the request's bytes are owned and in review. False when the
    /// bytes did not match the manifest, which drops the request rather than
    /// retrying it forever. Throws when the copy itself failed, which is
    /// retried with the request still durable.
    private func adopt(_ envelope: ShareInboxEnvelopeV1) throws -> Bool {
        let requestID = envelope.requestID
        let inputs = try envelope.items.map { item in
            OwnedSnapshotInput.file(
                try inbox.payloadURL(requestID: requestID, relativePath: item.relativePath),
                suggestedFilename: item.displayName
            )
        }
        let receipts: [OwnedSnapshotReceipt]
        do {
            receipts = try snapshotStore.stage(inputs, requestID: requestID)
        } catch {
            // Nothing is owned, so nothing is shown and the request stays put.
            snapshotStore.removeRequest(requestID: requestID)
            throw error
        }
        try checkpoint(.staged(requestID: requestID))
        guard zip(receipts, envelope.items).allSatisfy({ receipt, item in
            receipt.digest == item.digest && receipt.byteLength == item.byteLength
        }) else {
            // The App Group bytes are not what the manifest bound. There is no
            // version of this request that can be adopted, so it is dropped
            // rather than re-copied on every pass.
            snapshotStore.removeRequest(requestID: requestID)
            inbox.remove(requestID: requestID)
            return false
        }
        // Ownership is established: proven copies exist outside the App Group,
        // so the published request is no longer the only holder of these bytes.
        // Review comes next, and the request is only then dropped — the
        // acknowledgement is last, so a crash before it re-adopts rather than
        // losing the share. The candidate carries a stable key per slot, so a
        // re-adoption lands on a candidate review already holds instead of
        // showing it twice.
        let candidates = zip(receipts, envelope.items).map { receipt, item in
            ManualCaptureCandidate(
                url: receipt.url,
                displayName: item.displayName,
                surface: .share,
                source: .capturedSnapshot,
                native: NativeCandidateKey(
                    nativeRequestID: requestID,
                    candidateRef: "sib-\(requestID)-\(item.slotID)"
                )
            )
        }
        guard accept(candidates) else {
            // Review is at its bounded waiting capacity. The App Group request
            // remains the durable owner and will retry after the person clears
            // a selection; these temporary companion copies own no candidate.
            snapshotStore.removeRequest(requestID: requestID)
            throw ShareInboxAdoptionError.reviewCapacity
        }
        try checkpoint(.presented(requestID: requestID))
        // Dropping the request is the acknowledgement, and it is unconditional:
        // review owns the copies now, whether it listed them or refused them.
        // Leaving the request would re-adopt the same bytes on every pass.
        inbox.remove(requestID: requestID)
        try checkpoint(.acknowledged(requestID: requestID))
        return true
    }
}

private enum ShareInboxAdoptionError: Error {
    case reviewCapacity
}
