import CoreCaptureMacOS
import DocumentIntakeMacOS
import Foundation

@MainActor
final class NativeHandoffReceiver {
    private let mailbox: NativeHandoffMailbox
    private let submitter: ManualCaptureSubmission
    private let snapshotStore: OwnedSnapshotStore
    private let accept: ([ManualCaptureCandidate]) -> Void
    private var processing = Set<String>()

    init(
        mailbox: NativeHandoffMailbox,
        submitter: ManualCaptureSubmission,
        snapshotStore: OwnedSnapshotStore,
        accept: @escaping ([ManualCaptureCandidate]) -> Void
    ) {
        self.mailbox = mailbox
        self.submitter = submitter
        self.snapshotStore = snapshotStore
        self.accept = accept
    }

    func reconcile() {
        for offer in mailbox.pendingOffers() where processing.insert(offer.requestNonce).inserted {
            Task { @MainActor [weak self] in
                await self?.adopt(offer)
            }
        }
    }

    private func adopt(_ offer: NativeHandoffOfferV1) async {
        defer { processing.remove(offer.requestNonce) }
        if mailbox.acknowledgement(nonce: offer.requestNonce) != nil {
            mailbox.removeOffer(nonce: offer.requestNonce, removeSnapshots: true)
            return
        }
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
                        try mailbox.snapshotURL(
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
            try mailbox.writeAcknowledgement(NativeHandoffAcknowledgementV1(
                requestNonce: offer.requestNonce,
                acknowledgedAt: Date(),
                adoptions: adoptions
            ))
            mailbox.removeOffer(nonce: offer.requestNonce, removeSnapshots: true)
        } catch {
            submitter.discardAdoptedCandidates(candidates)
            snapshotStore.remove(candidates.filter { $0.source.isSnapshot }.map(\.url))
        }
    }
}
