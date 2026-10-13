import CoreCaptureMacOS
import DocumentIntakeMacOS
import Foundation

@MainActor
final class NativeHandoffReceiver: NSObject {
    private let mailbox: NativeHandoffMailbox
    private let resources: NativeHandoffMailbox
    private let submitter: ManualCaptureSubmission
    private let snapshotStore: OwnedSnapshotStore
    private let accept: ([ManualCaptureCandidate]) -> Void
    private var processing = Set<String>()

    init(
        mailbox: NativeHandoffMailbox,
        resources: NativeHandoffMailbox,
        submitter: ManualCaptureSubmission,
        snapshotStore: OwnedSnapshotStore,
        accept: @escaping ([ManualCaptureCandidate]) -> Void
    ) {
        self.mailbox = mailbox
        self.resources = resources
        self.submitter = submitter
        self.snapshotStore = snapshotStore
        self.accept = accept
        super.init()
    }

    func reconcile() {
        for offer in mailbox.pendingOffers()
        where mailbox.acknowledgement(nonce: offer.requestNonce) == nil
            && processing.insert(offer.requestNonce).inserted
        {
            Task { @MainActor [weak self] in
                guard let self else { return }
                defer { self.processing.remove(offer.requestNonce) }
                _ = try? await self.adopt(offer)
            }
        }
    }

    func submitAuthenticatedOffer(_ encodedOffer: Data) async throws -> Data {
        let offer = try mailbox.decodeOffer(encodedOffer)
        let acknowledgement = try await acceptAuthenticated(offer)
        return try mailbox.encodedAcknowledgement(acknowledgement)
    }

    private func acceptAuthenticated(
        _ offer: NativeHandoffOfferV1
    ) async throws -> NativeHandoffAcknowledgementV1 {
        // Always compare the complete replay before consulting its durable
        // acknowledgement. A nonce can never be rebound to different slots or
        // grant bytes, even after the first request completed.
        try mailbox.storeAuthenticatedOffer(offer)
        if let acknowledgement = mailbox.acknowledgement(nonce: offer.requestNonce) {
            return acknowledgement
        }
        guard processing.insert(offer.requestNonce).inserted else {
            return try await waitForAcknowledgement(nonce: offer.requestNonce)
        }
        defer { processing.remove(offer.requestNonce) }
        return try await adopt(offer)
    }

    private func waitForAcknowledgement(
        nonce: String
    ) async throws -> NativeHandoffAcknowledgementV1 {
        let deadline = Date().addingTimeInterval(30)
        while Date() < deadline {
            if let acknowledgement = mailbox.acknowledgement(nonce: nonce) {
                return acknowledgement
            }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        throw NativeHandoffMailboxError.unavailable
    }

    private func adopt(
        _ offer: NativeHandoffOfferV1
    ) async throws -> NativeHandoffAcknowledgementV1 {
        var candidates: [ManualCaptureCandidate] = []
        do {
            switch offer.surface {
            case .finderQuickAction:
                for slot in offer.slots {
                    guard let bookmark = slot.bookmark else {
                        throw NativeHandoffMailboxError.invalidOffer
                    }
                    var stale = false
                    let url = try URL(
                        resolvingBookmarkData: bookmark,
                        options: [.withoutUI],
                        relativeTo: nil,
                        bookmarkDataIsStale: &stale
                    )
                    guard !stale else { throw NativeAccessError.staleBookmark }
                    let accessed = url.startAccessingSecurityScopedResource()
                    defer { if accessed { url.stopAccessingSecurityScopedResource() } }
                    candidates.append(try submitter.adoptOriginal(
                        url,
                        displayName: slot.displayName,
                        surface: .finderQuickAction
                    ))
                }
            case .share:
                let inputs = try offer.slots.map { slot -> OwnedSnapshotInput in
                    guard let relativePath = slot.snapshotRelativePath else {
                        throw NativeHandoffMailboxError.invalidOffer
                    }
                    return .file(
                        try resources.snapshotURL(
                            nonce: offer.requestNonce,
                            relativePath: relativePath
                        ),
                        suggestedFilename: slot.displayName
                    )
                }
                let urls = try snapshotStore.stage(inputs)
                candidates = zip(urls, offer.slots).map { url, slot in
                    ManualCaptureCandidate(
                        url: url,
                        displayName: slot.displayName,
                        surface: .share,
                        source: .capturedSnapshot
                    )
                }
            }

            accept(candidates)
            let adoptions = zip(offer.slots, candidates).map { slot, candidate in
                let reference: String
                let revision: UInt64
                switch candidate.source {
                case let .adoptedOriginal(accessRef, generation, _):
                    reference = accessRef
                    revision = generation
                case .capturedSnapshot:
                    reference = "snapshot-\(slot.slotID)"
                    revision = 1
                case .original:
                    reference = "candidate-\(slot.slotID)"
                    revision = 1
                }
                return NativeHandoffAcknowledgementV1.Adoption(
                    slotID: slot.slotID,
                    candidateRef: reference,
                    candidateRevision: revision
                )
            }
            let acknowledgement = NativeHandoffAcknowledgementV1(
                requestNonce: offer.requestNonce,
                acknowledgedAt: Date(),
                adoptions: adoptions
            )
            try mailbox.writeAcknowledgement(acknowledgement)
            // Keep the authenticated offer through its short validity window
            // so a lost acknowledgement can be matched to the exact request.
            // Expiry cleanup removes the transfer blob; the adopted grant is
            // already owned separately by the native access store.
            resources.removeSnapshots(nonce: offer.requestNonce)
            return acknowledgement
        } catch {
            submitter.discardAdoptedCandidates(candidates)
            snapshotStore.remove(candidates.filter { $0.source.isSnapshot }.map(\.url))
            throw error
        }
    }
}
