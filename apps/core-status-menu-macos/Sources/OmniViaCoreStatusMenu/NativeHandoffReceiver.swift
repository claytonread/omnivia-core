import CoreCaptureMacOS
import CryptoKit
import DocumentIntakeMacOS
import Foundation

/// A step of an adoption, reported to `NativeHandoffReceiver.checkpoint`
/// immediately after the step's durable effect. Production leaves the hook
/// empty; tests throw from it to fail an adoption at exactly that boundary.
enum NativeHandoffStep: Equatable {
    /// The offer is bound to durable `needs_access` records.
    case reserved
    /// One companion-owned grant exists.
    case accessEstablished(slot: Int)
    /// Every owned copy is written and hashed. Not yet recorded.
    case snapshotsStaged
    /// The records durably say `adopted`. This is the commit point.
    case adopted
    /// The candidates are in review.
    case presented
    /// The acknowledgement is durable.
    case acknowledged
}

enum NativeHandoffAdoptionError: Error, Equatable {
    /// The request was released before it was acknowledged. It is not adopted
    /// again and nothing is acknowledged for it.
    case released
    /// The request can no longer be completed — its transfer window is over, or
    /// what it adopted is gone or no longer matches what was bound. It has been
    /// released.
    case orphaned
    /// The receiver is shutting down and accepts no new request.
    case terminating
}

struct NativeHandoffReconciliation: Equatable {
    /// Requests whose durable record is invalid or unreadable. Nothing was
    /// adopted, presented or released for them, and they stay blocked.
    var failedClosed: [String] = []
    /// The record store itself could not be listed, so no request was touched:
    /// without the list a pending offer cannot be told from one already adopted.
    var storeUnavailable = false
}

/// Adopts authenticated native handoff offers exactly once.
///
/// An adoption is a small state machine over one durable record per request:
///
///     reserved ──establish──▶ adopted ──present, acknowledge──▶ …──▶ released
///   (needs_access)        (commit point)                        (terminal)
///
/// * Reserving names every grant and owned copy before any exists, so a replay
///   of the same request maps to the same ordered candidate refs and revisions.
/// * `establish` creates what the records name and then writes `adopted`. A
///   failure before that write is rolled back to the bare reservation; a write
///   that threw is re-read first, and only a record still reading `needs_access`
///   is rolled back.
/// * From the commit point nothing is rolled back. Presenting, acknowledging and
///   cleanup are idempotent and are retried by `reconcile` until they hold.
/// * Review is process memory, so a candidate is presented once per process.
///   A new process presents an adopted candidate once more; it is released when
///   it is published, discarded, or found orphaned.
@MainActor
final class NativeHandoffReceiver: NSObject {
    private let mailbox: NativeHandoffMailbox
    private let resources: NativeHandoffMailbox
    private let candidates: NativeIntakeCandidateStore
    private let submitter: ManualCaptureSubmission
    private let snapshotStore: OwnedSnapshotStore
    private let accept: ([ManualCaptureCandidate]) -> Void
    private var processing = Set<String>()
    private var presented = Set<String>()
    private var terminating = false

    /// Failure-injection seam: see `NativeHandoffStep`.
    var checkpoint: (NativeHandoffStep) throws -> Void = { _ in }

    init(
        mailbox: NativeHandoffMailbox,
        resources: NativeHandoffMailbox,
        candidates: NativeIntakeCandidateStore,
        submitter: ManualCaptureSubmission,
        snapshotStore: OwnedSnapshotStore,
        accept: @escaping ([ManualCaptureCandidate]) -> Void
    ) {
        self.mailbox = mailbox
        self.resources = resources
        self.candidates = candidates
        self.submitter = submitter
        self.snapshotStore = snapshotStore
        self.accept = accept
        super.init()
    }

    /// Carries every durable request forward: finishes what a crash or a failed
    /// write interrupted, restores adopted candidates a new process must show
    /// again, and adopts offers that were stored but never reserved. A request
    /// whose record cannot be trusted is reported and left alone.
    @discardableResult
    func reconcile() -> NativeHandoffReconciliation {
        var outcome = NativeHandoffReconciliation()
        guard !terminating else { return outcome }
        let offers = Dictionary(
            mailbox.pendingOffers().map { ($0.requestNonce, $0) },
            uniquingKeysWith: { first, _ in first }
        )
        let nonces: [String]
        do {
            nonces = try candidates.nativeRequestIDs()
        } catch {
            outcome.storeUnavailable = true
            return outcome
        }
        for nonce in nonces {
            guard processing.insert(nonce).inserted else { continue }
            defer { processing.remove(nonce) }
            let records: [NativeIntakeCandidateV1]?
            do {
                records = try candidates.load(nativeRequestID: nonce)
            } catch {
                outcome.failedClosed.append(nonce)
                continue
            }
            guard let records else { continue }
            if hasPendingWork(records) {
                do {
                    _ = try drive(records, offer: offers[nonce])
                } catch NativeHandoffMailboxError.invalidOffer {
                    // The stored offer does not match what this request reserved.
                    outcome.failedClosed.append(nonce)
                } catch {
                    // Anything else is retried on the next pass.
                }
            }
        }
        let known = Set(nonces)
        for offer in offers.values.sorted(by: { $0.createdAt < $1.createdAt })
        where !known.contains(offer.requestNonce)
            && mailbox.acknowledgement(nonce: offer.requestNonce) == nil
            && processing.insert(offer.requestNonce).inserted
        {
            defer { processing.remove(offer.requestNonce) }
            _ = try? adopt(offer)
        }
        return outcome
    }

    /// A graceful quit ends review, which lives in process memory, so nothing
    /// adopted may outlive it. The panel has already released what it holds;
    /// this releases whatever never reached it — a request still being
    /// established, or adopted but not yet shown — and refuses new requests.
    func companionWillTerminate() {
        terminating = true
        guard let nonces = try? candidates.nativeRequestIDs() else { return }
        for nonce in nonces {
            guard let records = try? candidates.load(nativeRequestID: nonce) else { continue }
            let live = records.filter { $0.state != .released }
            if !live.isEmpty { release(live) }
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
        guard !terminating else { throw NativeHandoffAdoptionError.terminating }
        // Always compare the complete replay before consulting its durable
        // acknowledgement. A nonce can never be rebound to different slots or
        // grant bytes, even after the first request completed.
        try mailbox.storeAuthenticatedOffer(offer)
        if let acknowledgement = mailbox.acknowledgement(nonce: offer.requestNonce) {
            if let records = try candidates.load(nativeRequestID: offer.requestNonce) {
                try Self.requireBinding(
                    records,
                    to: offer,
                    offerDigest: try Self.offerDigest(offer, mailbox: mailbox)
                )
            } else {
                // Compatibility with acknowledgements created before durable
                // offer fingerprints existed. This path cannot adopt anything;
                // it can only replay the already-durable acknowledgement.
                guard acknowledgement.adoptions.map(\.slotID) == offer.slots.map(\.slotID) else {
                    throw NativeHandoffMailboxError.invalidOffer
                }
            }
            return acknowledgement
        }
        guard processing.insert(offer.requestNonce).inserted else {
            return try await waitForAcknowledgement(nonce: offer.requestNonce)
        }
        defer { processing.remove(offer.requestNonce) }
        return try adopt(offer)
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

    /// Binds the offer to its durable reservation, creating it on first sight.
    /// A record that exists but cannot be trusted throws and nothing is adopted.
    private func adopt(_ offer: NativeHandoffOfferV1) throws -> NativeHandoffAcknowledgementV1 {
        let digest = try Self.offerDigest(offer, mailbox: mailbox)
        let records: [NativeIntakeCandidateV1]
        if let existing = try candidates.load(nativeRequestID: offer.requestNonce) {
            try Self.requireBinding(existing, to: offer, offerDigest: digest)
            records = existing
        } else {
            records = try offer.slots.enumerated().map { index, slot in
                try Self.reservation(
                    offer: offer,
                    offerDigest: digest,
                    slot: slot,
                    index: index
                )
            }
            try candidates.write(records)
            try checkpoint(.reserved)
        }
        return try drive(records, offer: offer)
    }

    private func hasPendingWork(_ records: [NativeIntakeCandidateV1]) -> Bool {
        if records.contains(where: { $0.state == .needsAccess }) { return true }
        let live = records.filter { $0.state == .adopted }
        if live.contains(where: { !presented.contains($0.candidateRef) }) { return true }
        return !live.isEmpty && mailbox.acknowledgement(nonce: records[0].nativeRequestID) == nil
    }

    private func drive(
        _ initial: [NativeIntakeCandidateV1],
        offer: NativeHandoffOfferV1?
    ) throws -> NativeHandoffAcknowledgementV1 {
        var records = initial
        // A request is only ever driven by the offer it was reserved from. A
        // different offer under the same nonce is a conflicting replay.
        if let offer {
            try Self.requireBinding(
                records,
                to: offer,
                offerDigest: try Self.offerDigest(offer, mailbox: mailbox)
            )
        }
        if records.contains(where: { $0.state == .needsAccess }) {
            guard let offer else {
                // Its transfer window is over: nothing can complete the request.
                release(records)
                throw NativeHandoffAdoptionError.orphaned
            }
            records = try establish(records, offer: offer)
            try checkpoint(.adopted)
        }
        return try finish(records)
    }

    // MARK: Establishing (before the commit point)

    /// Creates what the reserved records name and durably marks them adopted.
    private func establish(
        _ reserved: [NativeIntakeCandidateV1],
        offer: NativeHandoffOfferV1
    ) throws -> [NativeIntakeCandidateV1] {
        do {
            // The offer's bookmark and transfer blobs may only be used inside
            // its validity window. After it, a record cannot be completed.
            try NativeHandoffMailbox.validate(offer)
            // Owned copies are staged as one batch under the request's quotas;
            // grants are established slot by slot, in the order they were
            // selected. The store keeps the two apart, so the first record's
            // kind decides which of the two this request is.
            let adopted = reserved[0].kind == .ownedSnapshot
                ? try stageSnapshots(reserved, offer: offer)
                : try establishGrants(reserved, offer: offer)
            try candidates.write(adopted)
            return adopted
        } catch {
            return try recover(from: error, reserved: reserved)
        }
    }

    /// A write that threw may still have landed, so the durable record decides.
    /// Only a record that reads back as still reserved is rolled back. One that
    /// reads back adopted is past the commit point and is carried forward. One
    /// that cannot be read is left alone: destroying resources under a record
    /// whose state is unknown is worse than leaking them until it can be read.
    private func recover(
        from error: Error,
        reserved: [NativeIntakeCandidateV1]
    ) throws -> [NativeIntakeCandidateV1] {
        guard let current = try? candidates.load(nativeRequestID: reserved[0].nativeRequestID) else {
            throw error
        }
        if current.allSatisfy({ $0.state == .adopted }) { return current }
        if current.allSatisfy({ $0.state == .needsAccess }) {
            // Back to the bare reservation: the record stays so a replay maps to
            // the same refs, but nothing it names is left behind.
            releaseResources(of: current)
        }
        throw error
    }

    /// Establishes one companion-owned grant per reserved slot, in the order
    /// the slots were offered, and returns the records the adoption binds. A
    /// file slot takes a read-only grant on the file; a folder slot takes a
    /// root grant on the folder and binds its generation. A mixed selection is
    /// established in its own order, and the whole request is written adopted
    /// in one atomic write, so nothing is acknowledged until every slot holds
    /// what its record names.
    private func establishGrants(
        _ reserved: [NativeIntakeCandidateV1],
        offer: NativeHandoffOfferV1
    ) throws -> [NativeIntakeCandidateV1] {
        guard offer.slots.count == reserved.count else { throw NativeHandoffMailboxError.invalidOffer }
        return try reserved.enumerated().map { index, record in
            let slot = offer.slots[index]
            guard slot.slotID == record.slotID else { throw NativeHandoffMailboxError.invalidOffer }
            switch record.kind {
            case .originalFile:
                return try establishOriginal(record, slot: slot, index: index)
            case .folderRoot:
                return try establishFolderRoot(record, slot: slot, index: index)
            case .ownedSnapshot:
                throw NativeHandoffMailboxError.invalidOffer
            }
        }
    }

    private func establishOriginal(
        _ record: NativeIntakeCandidateV1,
        slot: NativeHandoffSlotV1,
        index: Int
    ) throws -> NativeIntakeCandidateV1 {
        guard let accessRef = record.nativeAccessLeaseRef else {
            throw NativeHandoffMailboxError.invalidOffer
        }
        // A grant an interrupted attempt already adopted is reused before the
        // offer's implicit transfer bookmark is touched; that bookmark dies
        // with the extension host.
        if try submitter.adoptedCandidate(
            accessRef: accessRef,
            linkedDocumentID: Self.documentID(record),
            displayName: record.displayName,
            surface: .finderQuickAction
        ) != nil { return record.replacing(state: .adopted) }
        try Self.withResolvedSlot(slot) { url in
            _ = try submitter.adoptOriginal(
                url,
                displayName: record.displayName,
                surface: .finderQuickAction,
                accessRef: accessRef,
                linkedDocumentID: Self.documentID(record)
            )
        }
        try checkpoint(.accessEstablished(slot: index))
        return record.replacing(state: .adopted)
    }

    /// Binds one selected folder to a companion-owned root grant. The folder is
    /// bound, never walked: nothing inside it is listed, read or captured here,
    /// and no plan is made or submitted. Its scope is reviewed later, only when
    /// the person asks for it.
    private func establishFolderRoot(
        _ record: NativeIntakeCandidateV1,
        slot: NativeHandoffSlotV1,
        index: Int
    ) throws -> NativeIntakeCandidateV1 {
        guard let rootRef = record.nativeRootRef,
              let ownerRef = record.nativeRootOwnerRef
        else { throw NativeHandoffMailboxError.invalidOffer }
        // As for a file: a root grant an interrupted attempt already adopted is
        // reused before the offer's implicit bookmark is touched.
        let adopted: ManualCaptureCandidate
        if let existing = try submitter.adoptedFolderCandidate(
            rootRef: rootRef,
            ownerRef: ownerRef,
            displayName: record.displayName,
            surface: .finderQuickAction
        ) {
            adopted = existing
        } else {
            adopted = try Self.withResolvedSlot(slot) { url in
                try submitter.adoptFolderRoot(
                    url,
                    displayName: record.displayName,
                    surface: .finderQuickAction,
                    rootRef: rootRef,
                    ownerRef: ownerRef
                )
            }
            try checkpoint(.accessEstablished(slot: index))
        }
        guard let generation = adopted.source.adoptedRootGeneration else {
            throw NativeAccessError.unknownAccess
        }
        return record.replacing(state: .adopted, rootGeneration: generation)
    }

    /// Resolves one slot's implicit handoff bookmark and runs `body` inside its
    /// security scope, balanced on every path out. That bookmark dies with the
    /// extension host, so it is touched exactly here and only while the offer
    /// is still valid.
    private static func withResolvedSlot<T>(
        _ slot: NativeHandoffSlotV1,
        _ body: (URL) throws -> T
    ) throws -> T {
        guard let bookmark = slot.bookmark else { throw NativeHandoffMailboxError.invalidOffer }
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
        return try body(url)
    }

    /// Copies this request's transfer blobs into the companion's own storage
    /// under the reserved names and binds each copy's length and digest into
    /// the adopted records. Anything already at those names was written under
    /// no recorded binding, so it is replaced rather than reused.
    private func stageSnapshots(
        _ reserved: [NativeIntakeCandidateV1],
        offer: NativeHandoffOfferV1
    ) throws -> [NativeIntakeCandidateV1] {
        let nonce = reserved[0].nativeRequestID
        guard offer.slots.count == reserved.count else { throw NativeHandoffMailboxError.invalidOffer }
        let inputs = try zip(offer.slots, reserved).map { slot, record -> OwnedSnapshotInput in
            guard slot.slotID == record.slotID, let relativePath = slot.snapshotRelativePath else {
                throw NativeHandoffMailboxError.invalidOffer
            }
            return .file(
                try resources.snapshotURL(nonce: nonce, relativePath: relativePath),
                suggestedFilename: slot.displayName
            )
        }
        let receipts = try snapshotStore.stage(inputs, requestID: nonce)
        try checkpoint(.snapshotsStaged)
        guard receipts.map({ $0.url.lastPathComponent }) == reserved.compactMap(\.ownedContentRef) else {
            throw NativeHandoffMailboxError.invalidOffer
        }
        return zip(reserved, receipts).map { record, receipt in
            record.replacing(state: .adopted, content: (receipt.digest, receipt.byteLength))
        }
    }

    // MARK: Finishing (from the commit point on; never rolled back)

    /// Shows the adopted candidates once per process and makes the
    /// acknowledgement durable. Every step is idempotent.
    private func finish(
        _ records: [NativeIntakeCandidateV1]
    ) throws -> NativeHandoffAcknowledgementV1 {
        let nonce = records[0].nativeRequestID
        let pending = records.filter {
            $0.state == .adopted && !presented.contains($0.candidateRef)
        }
        // A grant a queued capture already names belongs to a published
        // candidate whose record only missed being released.
        let published = pending.filter {
            $0.nativeAccessLeaseRef.map(submitter.isBound(accessRef:)) ?? false
        }
        if !published.isEmpty {
            try? candidates.release(
                nativeRequestID: nonce,
                candidateRefs: Set(published.map(\.candidateRef))
            )
        }
        let publishedRefs = Set(published.map(\.candidateRef))
        let toShow = pending.filter { !publishedRefs.contains($0.candidateRef) }
        if !toShow.isEmpty {
            let ready: [ManualCaptureCandidate]
            do {
                ready = try reviewable(toShow)
            } catch {
                // What the records bound is gone or no longer matches: fail
                // closed. Anything else may be transient and is retried.
                if error is NativeAccessError || error is OwnedSnapshotStoreError {
                    release(toShow)
                }
                throw error
            }
            presented.formUnion(toShow.map(\.candidateRef))
            accept(ready)
            try checkpoint(.presented)
        }
        if let acknowledgement = mailbox.acknowledgement(nonce: nonce) { return acknowledgement }
        // A request released before it was acknowledged is not acknowledged now:
        // the extension must not be told it was adopted.
        guard records.contains(where: {
            $0.state != .released && !publishedRefs.contains($0.candidateRef)
        }) else { throw NativeHandoffAdoptionError.released }
        let acknowledgement = Self.acknowledgement(for: records)
        try mailbox.writeAcknowledgement(acknowledgement)
        try checkpoint(.acknowledged)
        // The owned copies are the companion's own now; the transfer blobs and
        // the extension's bookmark are no longer needed.
        resources.removeSnapshots(nonce: nonce)
        return acknowledgement
    }

    /// Rebuilds the review candidates from what the records bound, proving it
    /// is still there: a connected grant, or owned copies that still match
    /// their recorded length and digest.
    private func reviewable(
        _ records: [NativeIntakeCandidateV1]
    ) throws -> [ManualCaptureCandidate] {
        switch records[0].kind {
        case .originalFile, .folderRoot:
            return try records.map { record in
                let owned = try reviewableGrant(record)
                return ManualCaptureCandidate(
                    url: owned.url,
                    displayName: record.displayName,
                    surface: .finderQuickAction,
                    source: owned.source,
                    native: Self.key(record)
                )
            }
        case .ownedSnapshot:
            let expected = try records.map { record -> OwnedSnapshotExpectation in
                guard let name = record.ownedContentRef,
                      let digest = record.ownedContentDigest,
                      let length = record.ownedContentByteLength
                else { throw OwnedSnapshotStoreError.integrityMismatch }
                return OwnedSnapshotExpectation(name: name, byteLength: length, digest: digest)
            }
            let urls = try snapshotStore.verifiedURLs(
                requestID: records[0].nativeRequestID,
                expecting: expected
            )
            return zip(urls, records).map { url, record in
                ManualCaptureCandidate(
                    url: url,
                    displayName: record.displayName,
                    surface: .share,
                    source: .capturedSnapshot,
                    native: Self.key(record)
                )
            }
        }
    }

    /// Rebuilds one grant-backed candidate from what its record bound, proving
    /// the grant is still connected under the identity the record names. The
    /// folder case resolves only at the generation the record bound, so a root
    /// grant that was rolled back and re-adopted since cannot be mistaken for
    /// the one this candidate created.
    private func reviewableGrant(
        _ record: NativeIntakeCandidateV1
    ) throws -> ManualCaptureCandidate {
        let owned: ManualCaptureCandidate?
        switch record.kind {
        case .originalFile:
            guard let accessRef = record.nativeAccessLeaseRef else {
                throw NativeAccessError.unknownAccess
            }
            owned = try submitter.adoptedCandidate(
                accessRef: accessRef,
                linkedDocumentID: Self.documentID(record),
                displayName: record.displayName,
                surface: .finderQuickAction
            )
        case .folderRoot:
            guard let rootRef = record.nativeRootRef,
                  let ownerRef = record.nativeRootOwnerRef,
                  let generation = record.nativeRootBindingGeneration
            else { throw NativeAccessError.unknownAccess }
            owned = try submitter.adoptedFolderCandidate(
                rootRef: rootRef,
                ownerRef: ownerRef,
                generation: generation,
                displayName: record.displayName,
                surface: .finderQuickAction
            )
        case .ownedSnapshot:
            throw NativeAccessError.unknownAccess
        }
        guard let owned else { throw NativeAccessError.unknownAccess }
        return owned
    }

    // MARK: Releasing

    /// Drops what these records name, then marks them released. Resources go
    /// first: a crash between the two leaves an adopted record whose resources
    /// are gone, which the next start releases, rather than a released record
    /// that still holds access.
    private func release(_ records: [NativeIntakeCandidateV1]) {
        releaseResources(of: records)
        try? candidates.release(
            nativeRequestID: records[0].nativeRequestID,
            candidateRefs: Set(records.map(\.candidateRef))
        )
    }

    /// Never disconnects a grant that submitted work still names: a file grant
    /// a queued capture or subscription is bound to is left alone, and a root
    /// grant is released only for this candidate's own owner, so one a
    /// submitted bulk run or watched folder still owns survives.
    private func releaseResources(of records: [NativeIntakeCandidateV1]) {
        for accessRef in records.compactMap(\.nativeAccessLeaseRef) {
            submitter.releaseGrant(accessRef: accessRef)
        }
        for record in records where record.kind == .folderRoot {
            guard let rootRef = record.nativeRootRef,
                  let ownerRef = record.nativeRootOwnerRef
            else { continue }
            submitter.releaseRootGrant(rootRef: rootRef, ownerRef: ownerRef)
        }
        if records.contains(where: { $0.kind == .ownedSnapshot }) {
            snapshotStore.removeRequest(requestID: records[0].nativeRequestID)
        }
    }

    // MARK: Records

    private static func requireBinding(
        _ records: [NativeIntakeCandidateV1],
        to offer: NativeHandoffOfferV1,
        offerDigest: String
    ) throws {
        guard records.count == offer.slots.count,
              records.allSatisfy({ $0.offerDigest == offerDigest }),
              zip(records, offer.slots).allSatisfy({ record, slot in
                  record.slotID == slot.slotID
                      && record.displayName == slot.displayName
                      && record.kind == kind(of: slot)
              })
        else { throw NativeHandoffMailboxError.invalidOffer }
    }

    private static func kind(of slot: NativeHandoffSlotV1) -> NativeIntakeCandidateKind {
        switch slot.sourceKind {
        case .originalBookmark: .originalFile
        case .folderBookmark: .folderRoot
        case .capturedSnapshot: .ownedSnapshot
        }
    }

    private static func reservation(
        offer: NativeHandoffOfferV1,
        offerDigest: String,
        slot: NativeHandoffSlotV1,
        index: Int
    ) throws -> NativeIntakeCandidateV1 {
        let identity = "\(offer.requestNonce)-\(slot.slotID)"
        switch slot.sourceKind {
        case .originalBookmark:
            return NativeIntakeCandidateV1(
                nativeRequestID: offer.requestNonce,
                offerDigest: offerDigest,
                slotID: slot.slotID,
                candidateRef: "nic-\(identity)",
                kind: .originalFile,
                nativeAccessLeaseRef: "candidate-access-\(identity)",
                displayName: slot.displayName,
                state: .needsAccess
            )
        case .folderBookmark:
            // One root grant and one owner per slot. Two slots naming the same
            // folder reserve two independent roots, because the reservation is
            // made before any bookmark is resolved and so cannot know they are
            // the same folder.
            return NativeIntakeCandidateV1(
                nativeRequestID: offer.requestNonce,
                offerDigest: offerDigest,
                slotID: slot.slotID,
                candidateRef: "nic-\(identity)",
                kind: .folderRoot,
                nativeRootRef: "candidate-root-\(identity)",
                nativeRootOwnerRef: "candidate-owner-\(identity)",
                displayName: slot.displayName,
                state: .needsAccess
            )
        case .capturedSnapshot:
            return NativeIntakeCandidateV1(
                nativeRequestID: offer.requestNonce,
                offerDigest: offerDigest,
                slotID: slot.slotID,
                candidateRef: "nic-\(identity)",
                kind: .ownedSnapshot,
                ownedContentRef: try OwnedSnapshotStore.stagedFilename(
                    slot.displayName,
                    index: index
                ),
                displayName: slot.displayName,
                state: .needsAccess
            )
        }
    }

    /// The acknowledgement is a function of the records alone, so every attempt
    /// to write it states the same refs and revisions.
    private static func acknowledgement(
        for records: [NativeIntakeCandidateV1]
    ) -> NativeHandoffAcknowledgementV1 {
        NativeHandoffAcknowledgementV1(
            requestNonce: records[0].nativeRequestID,
            acknowledgedAt: Date(),
            adoptions: records.map {
                NativeHandoffAcknowledgementV1.Adoption(
                    slotID: $0.slotID,
                    candidateRef: $0.candidateRef,
                    candidateRevision: $0.candidateRevision
                )
            }
        )
    }

    private static func key(_ record: NativeIntakeCandidateV1) -> NativeCandidateKey {
        NativeCandidateKey(
            nativeRequestID: record.nativeRequestID,
            candidateRef: record.candidateRef
        )
    }

    private static func offerDigest(
        _ offer: NativeHandoffOfferV1,
        mailbox: NativeHandoffMailbox
    ) throws -> String {
        let data = try mailbox.encodedOffer(offer)
        return "sha256:" + SHA256.hash(data: data)
            .map { String(format: "%02x", $0) }
            .joined()
    }

    private static func documentID(_ record: NativeIntakeCandidateV1) -> String {
        "candidate-doc-\(record.nativeRequestID)-\(record.slotID)"
    }
}
