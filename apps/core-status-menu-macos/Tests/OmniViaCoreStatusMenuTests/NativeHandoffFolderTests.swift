import Foundation
import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

/// The Finder Quick Action's activation rule admits folders as well as files,
/// so a selected folder arrives as its own closed source kind and is adopted as
/// a companion-owned root grant. These tests cover that path end to end:
/// adoption, replay, mixed selections, review, and every way a folder candidate
/// leaves review.
@MainActor
final class NativeHandoffFolderTests: NativeHandoffTestCase {

    // MARK: Cold adoption and replay

    func testAFolderQuickActionAdoptsOneRootGrantAndAReplayAdoptsNoOther() async throws {
        let root = try temporaryDirectory()
        let folder = try makeFolder(root, name: "Briefs", files: ["one.pdf", "two.pdf"])
        let nonce = newID()
        let slot = newID()
        let companion = try launch(root: root)
        let offer = try finderOffer(nonce: nonce, slotIDs: [slot], urls: [folder])
        XCTAssertEqual(offer.slots.map(\.sourceKind), [.folderBookmark])

        let acknowledgement = try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(offer)
        )

        let records = try XCTUnwrap(companion.candidates.load(nativeRequestID: nonce))
        XCTAssertEqual(records.map(\.kind), [.folderRoot])
        XCTAssertEqual(records.map(\.state), [.adopted])
        // The folder candidate binds a root grant and its own owner, and is
        // never represented as a direct file or as owned bytes.
        XCTAssertEqual(records[0].nativeRootRef, "candidate-root-\(nonce)-\(slot)")
        XCTAssertEqual(records[0].nativeRootOwnerRef, "candidate-owner-\(nonce)-\(slot)")
        XCTAssertEqual(records[0].nativeRootBindingGeneration, 1)
        XCTAssertNil(records[0].nativeAccessLeaseRef)
        XCTAssertNil(records[0].ownedContentRef)
        XCTAssertEqual(companion.connectedAccessRefs, [])
        XCTAssertEqual(
            companion.connectedRoots.map(\.rootRef),
            ["candidate-root-\(nonce)-\(slot)"]
        )
        XCTAssertEqual(
            companion.connectedRoots.map(\.ownerRefs),
            [["candidate-owner-\(nonce)-\(slot)"]]
        )
        let batch = try XCTUnwrap(companion.accepted.batches.first)
        XCTAssertEqual(companion.accepted.batches.count, 1)
        XCTAssertTrue(batch[0].source.isAdoptedFolderRoot)
        XCTAssertEqual(batch[0].source.adoptedRootGeneration, 1)
        XCTAssertEqual(
            batch[0].url.resolvingSymlinksInPath().standardizedFileURL,
            folder.resolvingSymlinksInPath().standardizedFileURL
        )

        // An exact replay returns the same acknowledgement and adopts nothing
        // more: one root grant, one owner, the same generation.
        let replayed = try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(offer)
        )
        XCTAssertEqual(replayed, acknowledgement)
        XCTAssertEqual(companion.connectedRoots.count, 1)
        XCTAssertEqual(companion.connectedRoots[0].ownerRefs.count, 1)
        XCTAssertEqual(companion.accepted.batches.count, 1)
    }

    func testAnAdoptedFolderIsRestoredOnceAfterARestart() async throws {
        let root = try temporaryDirectory()
        let folder = try makeFolder(root, name: "Briefs", files: ["one.pdf"])
        let nonce = newID()
        let slot = newID()
        let first = try launch(root: root)
        let offer = try finderOffer(nonce: nonce, slotIDs: [slot], urls: [folder])
        _ = try await first.receiver.submitAuthenticatedOffer(
            try first.mailbox.encodedOffer(offer)
        )

        // Review is process memory, so a new process shows the adopted folder
        // once more — rebuilt from the root grant at the generation the record
        // bound, without adopting a second grant.
        let restarted = try launch(root: root)
        restarted.receiver.reconcile()
        restarted.receiver.reconcile()

        XCTAssertEqual(restarted.accepted.batches.count, 1)
        let candidate = try XCTUnwrap(restarted.accepted.batches.first?.first)
        XCTAssertEqual(candidate.source.adoptedRootGeneration, 1)
        XCTAssertEqual(
            candidate.url.resolvingSymlinksInPath().standardizedFileURL,
            folder.resolvingSymlinksInPath().standardizedFileURL
        )
        XCTAssertEqual(restarted.connectedRoots.count, 1)
        XCTAssertEqual(
            try restarted.candidates.load(nativeRequestID: nonce)?.map(\.state),
            [.adopted]
        )
    }

    func testAFailedFolderAdoptionRollsBackAndTheReplayAdoptsExactlyOneRootGrant() async throws {
        let root = try temporaryDirectory()
        let folder = try makeFolder(root, name: "Briefs")
        let nonce = newID()
        let slot = newID()
        let companion = try launch(root: root, bookmarkFailures: 1)
        let offer = try finderOffer(nonce: nonce, slotIDs: [slot], urls: [folder])

        await failure { try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(offer)
        ) }

        // Back to the bare reservation: the record stays so the replay maps to
        // the same refs, and no root grant is left behind.
        XCTAssertEqual(
            try companion.candidates.load(nativeRequestID: nonce)?.map(\.state),
            [.needsAccess]
        )
        XCTAssertEqual(companion.connectedRoots.map(\.rootRef), [])
        XCTAssertEqual(companion.accepted.batches, [])
        XCTAssertNil(companion.mailbox.acknowledgement(nonce: nonce))

        _ = try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(offer)
        )
        XCTAssertEqual(companion.connectedRoots.map(\.rootRef), ["candidate-root-\(nonce)-\(slot)"])
        let records = try XCTUnwrap(companion.candidates.load(nativeRequestID: nonce))
        XCTAssertEqual(records.map(\.state), [.adopted])
        // Nothing was adopted before the failure, so the replay is this root
        // grant's first generation.
        XCTAssertEqual(records[0].nativeRootBindingGeneration, 1)
        XCTAssertEqual(
            companion.accepted.batches.first?.first?.source.adoptedRootGeneration,
            1
        )
    }

    func testARollbackAfterTheRootGrantExistsMakesTheReplayBindTheNextGeneration() async throws {
        let root = try temporaryDirectory()
        let folder = try makeFolder(root, name: "Briefs")
        let nonce = newID()
        let slot = newID()
        let companion = try launch(root: root)
        let offer = try finderOffer(nonce: nonce, slotIDs: [slot], urls: [folder])
        // Fail immediately after the root grant exists but before the record
        // says adopted, so the rollback has a live grant to disconnect.
        companion.receiver.checkpoint = {
            if $0 == .accessEstablished(slot: 0) { throw InjectedFailure() }
        }
        await failure { try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(offer)
        ) }
        companion.receiver.checkpoint = { _ in }
        XCTAssertEqual(companion.connectedRoots.map(\.rootRef), [])
        XCTAssertEqual(
            try companion.candidates.load(nativeRequestID: nonce)?.map(\.state),
            [.needsAccess]
        )

        _ = try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(offer)
        )

        // The rolled-back attempt disconnected the grant, so the replay bound
        // the next generation — and review resolves only at that one.
        let records = try XCTUnwrap(companion.candidates.load(nativeRequestID: nonce))
        XCTAssertEqual(records.map(\.state), [.adopted])
        XCTAssertEqual(records[0].nativeRootBindingGeneration, 2)
        XCTAssertEqual(companion.connectedRoots.map(\.generation), [2])
        XCTAssertEqual(
            companion.accepted.batches.first?.first?.source.adoptedRootGeneration,
            2
        )
        // The stale generation is not accepted as this candidate's grant.
        XCTAssertNil(try companion.submitter.adoptedFolderCandidate(
            rootRef: try XCTUnwrap(records[0].nativeRootRef),
            ownerRef: try XCTUnwrap(records[0].nativeRootOwnerRef),
            generation: 1,
            displayName: "Briefs",
            surface: .finderQuickAction
        ))
    }

    func testAFolderReservationWithNoRemainingOfferIsReleasedWithNoRootGrant() async throws {
        let root = try temporaryDirectory()
        let folder = try makeFolder(root, name: "Briefs")
        let nonce = newID()
        let companion = try launch(root: root)
        let offer = try finderOffer(nonce: nonce, slotIDs: [newID()], urls: [folder])
        companion.receiver.checkpoint = { if $0 == .reserved { throw InjectedFailure() } }
        await failure { try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(offer)
        ) }
        companion.receiver.checkpoint = { _ in }

        // Its transfer window is over, so nothing can complete the request.
        companion.mailbox.removeOffer(nonce: nonce, removeSnapshots: true)
        companion.receiver.reconcile()

        XCTAssertEqual(
            try companion.candidates.load(nativeRequestID: nonce)?.map(\.state),
            [.released]
        )
        XCTAssertEqual(companion.connectedRoots.map(\.rootRef), [])
        XCTAssertEqual(companion.accepted.batches, [])
        XCTAssertNil(companion.mailbox.acknowledgement(nonce: nonce))
    }

    // MARK: Mixed selections

    func testAMixedFileAndFolderSelectionKeepsItsOrderAndAcknowledgesOnlyAfterEveryAdoption() async throws {
        let root = try temporaryDirectory()
        let first = try writeOriginal(root, name: "brief.pdf")
        let folder = try makeFolder(root, name: "Briefs", files: ["member.pdf"])
        let last = try writeOriginal(root, name: "notes.txt")
        let nonce = newID()
        let slots = [newID(), newID(), newID()]
        let companion = try launch(root: root)
        let offer = try finderOffer(
            nonce: nonce,
            slotIDs: slots,
            urls: [first, folder, last]
        )
        XCTAssertEqual(
            offer.slots.map(\.sourceKind),
            [.originalBookmark, .folderBookmark, .originalBookmark]
        )

        _ = try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(offer)
        )

        let records = try XCTUnwrap(companion.candidates.load(nativeRequestID: nonce))
        XCTAssertEqual(records.map(\.kind), [.originalFile, .folderRoot, .originalFile])
        XCTAssertEqual(records.map(\.slotID), slots)
        XCTAssertEqual(records.map(\.state), [.adopted, .adopted, .adopted])
        // Acknowledged only once every slot holds what its record names, in the
        // order the person selected them.
        let acknowledgement = try XCTUnwrap(companion.mailbox.acknowledgement(nonce: nonce))
        XCTAssertEqual(acknowledgement.adoptions.map(\.slotID), slots)
        XCTAssertEqual(acknowledgement.adoptions.map(\.candidateRef), records.map(\.candidateRef))
        XCTAssertEqual(companion.connectedAccessRefs.count, 2)
        XCTAssertEqual(companion.connectedRoots.count, 1)
        let batch = try XCTUnwrap(companion.accepted.batches.first)
        XCTAssertEqual(batch.map(\.displayName), ["brief.pdf", "Briefs", "notes.txt"])
        XCTAssertEqual(
            batch.map(\.source.isAdoptedFolderRoot),
            [false, true, false]
        )
        XCTAssertEqual(batch.map(\.source.isAdoptedOriginal), [true, false, true])
    }

    func testAMixedSelectionIsRestoredInOrderAfterARestart() async throws {
        let root = try temporaryDirectory()
        let file = try writeOriginal(root, name: "brief.pdf")
        let folder = try makeFolder(root, name: "Briefs")
        let nonce = newID()
        let slots = [newID(), newID()]
        let first = try launch(root: root)
        _ = try await first.receiver.submitAuthenticatedOffer(
            try first.mailbox.encodedOffer(
                try finderOffer(nonce: nonce, slotIDs: slots, urls: [folder, file])
            )
        )

        let restarted = try launch(root: root)
        restarted.receiver.reconcile()

        let batch = try XCTUnwrap(restarted.accepted.batches.first)
        XCTAssertEqual(batch.map(\.displayName), ["Briefs", "brief.pdf"])
        XCTAssertEqual(batch.map(\.source.isAdoptedFolderRoot), [true, false])
    }

    // MARK: Review holds the folder; nothing is read inside it

    func testAnAdoptedFolderIsNeitherEnumeratedNorCapturedBeforeItIsReviewed() async throws {
        let root = try temporaryDirectory()
        let folder = try makeFolder(root, name: "Briefs", files: ["one.pdf", "two.pdf"])
        let nonce = newID()
        let companion = try launch(root: root, withPanel: true)
        let panel = try XCTUnwrap(companion.panel)

        _ = try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(
                try finderOffer(nonce: nonce, slotIDs: [newID()], urls: [folder])
            )
        )

        // Adoption binds the folder and stops. Nothing inside it is listed,
        // read, planned or queued until the person reviews its scope.
        XCTAssertEqual(panel.reviewItemCount, 1)
        XCTAssertEqual(companion.environment.queue.allEntries(), [])
        XCTAssertEqual(companion.environment.folderBulkRuns.all(), [])
        XCTAssertEqual(companion.environment.folderSubscriptions.all(), [])
        XCTAssertEqual(companion.environment.fileSubscriptions.all(), [])
        XCTAssertEqual(try ownedRequests(root), [])
        // The folder row carries no publishable file candidate, so Add stays
        // unavailable while it is listed.
        XCTAssertNil(panel.beginSubmit())
    }

    func testAFolderCandidateIsNeverPublishedAsADirectFile() async throws {
        let root = try temporaryDirectory()
        // A folder whose name looks like a supported document is still a folder.
        let folder = try makeFolder(root, name: "Briefs.pdf", files: ["one.pdf"])
        let nonce = newID()
        let companion = try launch(root: root)
        _ = try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(
                try finderOffer(nonce: nonce, slotIDs: [newID()], urls: [folder])
            )
        )
        let candidate = try XCTUnwrap(companion.accepted.batches.first?.first)

        var draft = CaptureDraft()
        try draft.add([candidate])
        XCTAssertEqual(draft.items.map(\.kind), [.folder])
        XCTAssertEqual(draft.fileCandidates, [])
        XCTAssertFalse(draft.canSubmitFiles)

        // And the file publication path refuses a root grant outright.
        let error = await failure { try await companion.submitter.submit([candidate]) }
        XCTAssertEqual(error as? ManualCaptureSubmissionError, .unsupportedType)
        XCTAssertEqual(companion.environment.queue.allEntries(), [])
    }

    // MARK: Leaving review

    func testRemovingAFolderRowReleasesItsRootGrantAndRecord() async throws {
        let root = try temporaryDirectory()
        let folder = try makeFolder(root, name: "Briefs")
        let nonce = newID()
        let companion = try launch(root: root)
        _ = try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(
                try finderOffer(nonce: nonce, slotIDs: [newID()], urls: [folder])
            )
        )
        var draft = CaptureDraft()
        try draft.add(try XCTUnwrap(companion.accepted.batches.first))

        // What Remove does: take the row out and release whatever it held.
        let removed = try XCTUnwrap(draft.remove(at: 0))
        let owned = try XCTUnwrap(removed.ownedCandidate)
        XCTAssertTrue(owned.source.isAdoptedFolderRoot)
        companion.submitter.discardUnsubmitted([owned])

        XCTAssertEqual(companion.connectedRoots.map(\.rootRef), [])
        XCTAssertEqual(
            try companion.candidates.load(nativeRequestID: nonce)?.map(\.state),
            [.released]
        )
    }

    func testDiscardingReviewReleasesAFolderRowAlongsideItsFiles() async throws {
        let root = try temporaryDirectory()
        let file = try writeOriginal(root, name: "brief.pdf")
        let folder = try makeFolder(root, name: "Briefs")
        let nonce = newID()
        let companion = try launch(root: root, withPanel: true)
        let panel = try XCTUnwrap(companion.panel)
        _ = try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(
                try finderOffer(nonce: nonce, slotIDs: [newID(), newID()], urls: [file, folder])
            )
        )
        XCTAssertEqual(panel.reviewItemCount, 2)

        panel.discardUnsubmittedCandidates()

        XCTAssertEqual(companion.connectedAccessRefs, [])
        XCTAssertEqual(companion.connectedRoots.map(\.rootRef), [])
        XCTAssertEqual(
            try companion.candidates.load(nativeRequestID: nonce)?.map(\.state),
            [.released, .released]
        )
        XCTAssertEqual(panel.reviewItemCount, 0)
    }

    func testQuittingReleasesAFolderCandidateThatNeverReachedReview() async throws {
        let root = try temporaryDirectory()
        let folder = try makeFolder(root, name: "Briefs")
        let nonce = newID()
        let companion = try launch(root: root)
        // Adopted, but the review it was for never opened.
        companion.receiver.checkpoint = { if $0 == .presented { throw InjectedFailure() } }
        await failure { try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(
                try finderOffer(nonce: nonce, slotIDs: [newID()], urls: [folder])
            )
        ) }
        companion.receiver.checkpoint = { _ in }
        XCTAssertEqual(companion.connectedRoots.count, 1)

        companion.receiver.companionWillTerminate()

        XCTAssertEqual(companion.connectedRoots.map(\.rootRef), [])
        XCTAssertEqual(
            try companion.candidates.load(nativeRequestID: nonce)?.map(\.state),
            [.released]
        )
        // And the receiver takes no new request afterwards.
        let error = await failure { try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(
                try finderOffer(nonce: newID(), slotIDs: [newID()], urls: [folder])
            )
        ) }
        XCTAssertEqual(error as? NativeHandoffAdoptionError, .terminating)
    }

    func testAFolderWhoseRootGrantIsGoneIsReleasedRatherThanShownAgain() async throws {
        let root = try temporaryDirectory()
        let folder = try makeFolder(root, name: "Briefs")
        let nonce = newID()
        let first = try launch(root: root)
        _ = try await first.receiver.submitAuthenticatedOffer(
            try first.mailbox.encodedOffer(
                try finderOffer(nonce: nonce, slotIDs: [newID()], urls: [folder])
            )
        )
        let rootRef = try XCTUnwrap(
            try first.candidates.load(nativeRequestID: nonce)?.first?.nativeRootRef
        )
        let ownerRef = try XCTUnwrap(
            try first.candidates.load(nativeRequestID: nonce)?.first?.nativeRootOwnerRef
        )
        // Exactly the state a crash between releasing the grant and releasing
        // the record leaves behind.
        first.submitter.releaseRootGrant(rootRef: rootRef, ownerRef: ownerRef)

        let restarted = try launch(root: root)
        restarted.receiver.reconcile()

        XCTAssertEqual(restarted.accepted.batches, [])
        XCTAssertEqual(
            try restarted.candidates.load(nativeRequestID: nonce)?.map(\.state),
            [.released]
        )
        XCTAssertEqual(restarted.connectedRoots.map(\.rootRef), [])
    }

    func testReleasingAFolderCandidateNeverDropsAGrantSubmittedWorkStillOwns() async throws {
        let root = try temporaryDirectory()
        let folder = try makeFolder(root, name: "Briefs")
        let nonce = newID()
        let companion = try launch(root: root)
        _ = try await companion.receiver.submitAuthenticatedOffer(
            try companion.mailbox.encodedOffer(
                try finderOffer(nonce: nonce, slotIDs: [newID()], urls: [folder])
            )
        )
        let record = try XCTUnwrap(companion.candidates.load(nativeRequestID: nonce)?.first)
        let rootRef = try XCTUnwrap(record.nativeRootRef)
        // A durable owner — a bulk run or a watched folder — takes the same
        // root grant.
        _ = try companion.environment.accessStore.adoptRoot(
            rootRef: rootRef,
            ownerRef: "bulk-submitted",
            selectedURL: folder
        )

        companion.submitter.discardUnsubmitted(
            try XCTUnwrap(companion.accepted.batches.first)
        )

        // Only this candidate's own owner went; the grant itself survives for
        // the submitted work that holds it.
        XCTAssertEqual(companion.connectedRoots.map(\.rootRef), [rootRef])
        XCTAssertEqual(companion.connectedRoots.map(\.ownerRefs), [["bulk-submitted"]])
        XCTAssertEqual(
            try companion.candidates.load(nativeRequestID: nonce)?.map(\.state),
            [.released]
        )
    }
}
