import Foundation
import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

/// Adopting durable Share requests into review. Every test here starts from a
/// request that was published while no companion existed, which is the whole
/// point of the spool.
@MainActor
final class ShareInboxAdoptionTests: NativeHandoffTestCase {
    // MARK: Companion-closed recovery

    func testRequestQueuedWhileClosedIsAdoptedOnStartup() throws {
        let root = try temporaryDirectory()
        // No companion yet: this is the extension writing on its own.
        let requestID = try publishShare(
            try shareInbox(root),
            [("alpha.txt", "alpha"), ("beta.txt", "beta")]
        )

        let companion = try launch(root: root, withPanel: true)
        let outcome = companion.shareAdopter.reconcile()

        XCTAssertEqual(outcome.adopted, [requestID])
        XCTAssertEqual(companion.accepted.batches.count, 1)
        let batch = try XCTUnwrap(companion.accepted.batches.first)
        // Order is the order the person shared, and every candidate is a
        // captured copy, never an original.
        XCTAssertEqual(batch.map(\.displayName), ["alpha.txt", "beta.txt"])
        XCTAssertTrue(batch.allSatisfy { $0.source == .capturedSnapshot })
        XCTAssertTrue(batch.allSatisfy { $0.surface == .share })
        XCTAssertEqual(try Data(contentsOf: batch[0].url), Data("alpha".utf8))
        XCTAssertEqual(companion.panel?.reviewItemCount, 2)
        // The request is acknowledged by being dropped.
        XCTAssertEqual(companion.shareInbox.pendingRequestIDs(), [])
    }

    /// Nothing is queued, watched or delivered by an adoption. Review is the
    /// only thing that happened.
    func testAdoptionMutatesNoQueueOrSubscription() throws {
        let root = try temporaryDirectory()
        try publishShare(try shareInbox(root), [("alpha.txt", "alpha")])
        let companion = try launch(root: root, withPanel: true)
        companion.shareAdopter.reconcile()

        XCTAssertEqual(companion.environment.queue.allEntries().count, 0)
        XCTAssertEqual(companion.environment.fileSubscriptions.all().count, 0)
        XCTAssertEqual(companion.environment.folderSubscriptions.all().count, 0)
        XCTAssertEqual(companion.connectedAccessRefs, [])
        XCTAssertEqual(companion.connectedRoots.count, 0)
    }

    /// The copies survive the App Group bytes going away, which is what a
    /// provider's lifetime ending looks like from the companion's side.
    func testAdoptedCopiesSurviveTheSourceBeingRemoved() throws {
        let root = try temporaryDirectory()
        let requestID = try publishShare(try shareInbox(root), [("alpha.txt", "alpha")])
        let pending = pendingDirectory(root, requestID: requestID)
        let companion = try launch(root: root)
        companion.shareAdopter.reconcile()

        XCTAssertFalse(FileManager.default.fileExists(atPath: pending.path))
        let url = try XCTUnwrap(companion.accepted.batches.first?.first?.url)
        XCTAssertEqual(try Data(contentsOf: url), Data("alpha".utf8))
    }

    /// A shared link is adopted as the text of the link and nothing else. No
    /// request is made for it, here or later: the bytes in review are exactly
    /// the characters the host handed over.
    func testSharedLinkIsAdoptedAsItsOwnTextAndNothingIsFetched() throws {
        let root = try temporaryDirectory()
        let link = "https://example.com/a?b=c"
        try publishShare(
            try shareInbox(root),
            [("Shared Link 1.txt", link)],
            kind: .link
        )
        let companion = try launch(root: root, withPanel: true)
        companion.shareAdopter.reconcile()

        let candidate = try XCTUnwrap(companion.accepted.batches.first?.first)
        XCTAssertEqual(try Data(contentsOf: candidate.url), Data(link.utf8))
        XCTAssertEqual(candidate.source, .capturedSnapshot)
        XCTAssertEqual(companion.panel?.reviewAllowsKeepUpdated, false)
    }

    /// Shared plain text is adopted the same way: the bytes, as given.
    func testSharedTextIsAdoptedAsGiven() throws {
        let root = try temporaryDirectory()
        try publishShare(
            try shareInbox(root),
            [("Shared Text 1.txt", "a note\nwith two lines")],
            kind: .text
        )
        let companion = try launch(root: root)
        companion.shareAdopter.reconcile()

        let candidate = try XCTUnwrap(companion.accepted.batches.first?.first)
        XCTAssertEqual(
            try Data(contentsOf: candidate.url),
            Data("a note\nwith two lines".utf8)
        )
    }

    func testStagedButUnpublishedRequestIsNotAdopted() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let staged = try stageShare(inbox, [("alpha.txt", "alpha")])
        let companion = try launch(root: root)

        let outcome = companion.shareAdopter.reconcile()
        XCTAssertEqual(outcome, ShareInboxReconciliation())
        XCTAssertEqual(companion.accepted.batches.count, 0)
        XCTAssertTrue(FileManager.default.fileExists(atPath: staged.directory.path))
    }

    // MARK: Idempotence

    /// A second pass over the same durable state adopts nothing again.
    func testRepeatedReconciliationAdoptsOnce() throws {
        let root = try temporaryDirectory()
        try publishShare(try shareInbox(root), [("alpha.txt", "alpha")])
        let companion = try launch(root: root, withPanel: true)

        companion.shareAdopter.reconcile()
        companion.shareAdopter.reconcile()
        companion.shareAdopter.reconcile()

        XCTAssertEqual(companion.accepted.batches.count, 1)
        XCTAssertEqual(companion.panel?.reviewItemCount, 1)
        XCTAssertEqual(try ownedRequests(root).count, 1)
    }

    /// A lost acknowledgement — the copies are owned and in review, but the
    /// request was never dropped — re-copies the same bytes and lands on the
    /// candidate review already holds rather than showing it twice.
    func testLostAcknowledgementDoesNotDuplicateTheCandidate() throws {
        let root = try temporaryDirectory()
        let requestID = try publishShare(try shareInbox(root), [("alpha.txt", "alpha")])
        let companion = try launch(root: root, withPanel: true)
        companion.shareAdopter.checkpoint = { step in
            if step == .acknowledged(requestID: requestID) { throw InjectedFailure() }
        }
        _ = companion.shareAdopter.reconcile()
        XCTAssertEqual(companion.accepted.batches.count, 1)
        XCTAssertEqual(companion.panel?.reviewItemCount, 1)

        // The same process retries: the request is gone, so this pass is a
        // no-op, and review still holds exactly one item.
        companion.shareAdopter.checkpoint = { _ in }
        companion.shareAdopter.reconcile()
        XCTAssertEqual(companion.panel?.reviewItemCount, 1)
        XCTAssertEqual(companion.panel?.waitingSelectionCount, 0)
    }

    // MARK: Crash recovery

    /// A crash after the copies exist but before review sees them leaves the
    /// request durable, so the next start adopts it exactly once.
    func testCrashBeforeReviewLeavesTheRequestAdoptable() throws {
        let root = try temporaryDirectory()
        let requestID = try publishShare(try shareInbox(root), [("alpha.txt", "alpha")])
        let first = try launch(root: root, withPanel: true)
        first.shareAdopter.checkpoint = { step in
            if step == .staged(requestID: requestID) { throw InjectedFailure() }
        }
        let interrupted = first.shareAdopter.reconcile()
        XCTAssertEqual(interrupted.retried, [requestID])
        XCTAssertEqual(first.accepted.batches.count, 0)
        XCTAssertTrue(first.shareInbox.isPending(requestID: requestID))

        // Restart: durable state survives, process memory does not.
        let second = try launch(root: root, withPanel: true)
        XCTAssertEqual(second.shareAdopter.reconcile().adopted, [requestID])
        XCTAssertEqual(second.accepted.batches.count, 1)
        XCTAssertEqual(second.panel?.reviewItemCount, 1)
        XCTAssertEqual(try ownedRequests(root).count, 1)
    }

    /// A crash between review and the acknowledgement re-adopts on the next
    /// start — the previous process's review died with it, so this is one
    /// candidate, not two.
    func testCrashBetweenReviewAndAcknowledgementReAdoptsOnce() throws {
        let root = try temporaryDirectory()
        let requestID = try publishShare(try shareInbox(root), [("alpha.txt", "alpha")])
        let first = try launch(root: root, withPanel: true)
        first.shareAdopter.checkpoint = { step in
            if step == .presented(requestID: requestID) { throw InjectedFailure() }
        }
        first.shareAdopter.reconcile()
        XCTAssertEqual(first.accepted.batches.count, 1)
        XCTAssertTrue(first.shareInbox.isPending(requestID: requestID))

        let second = try launch(root: root, withPanel: true)
        XCTAssertEqual(second.shareAdopter.reconcile().adopted, [requestID])
        XCTAssertEqual(second.panel?.reviewItemCount, 1)
        XCTAssertEqual(second.shareInbox.pendingRequestIDs(), [])
    }

    // MARK: Fail-closed

    /// Bytes that are not what the manifest bound are never shown. There is no
    /// version of that request that can be adopted, so it is dropped rather
    /// than retried on every pass.
    func testTamperedBytesAreRefusedAndDropped() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let requestID = try publishShare(inbox, [("alpha.txt", "alpha")])
        let envelope = try XCTUnwrap(inbox.envelope(requestID: requestID))
        try Data("tampered".utf8).write(
            to: try inbox.payloadURL(
                requestID: requestID,
                relativePath: envelope.items[0].relativePath
            )
        )

        let companion = try launch(root: root, withPanel: true)
        let outcome = companion.shareAdopter.reconcile()
        XCTAssertEqual(outcome.rejected, [requestID])
        XCTAssertEqual(companion.accepted.batches.count, 0)
        XCTAssertEqual(companion.shareInbox.pendingRequestIDs(), [])
        XCTAssertEqual(try ownedRequests(root), [])
    }

    /// A request this build cannot read is reported and left exactly as it is.
    func testUnknownSchemaIsPreservedAndNotAdopted() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let requestID = try publishShare(inbox, [("alpha.txt", "alpha")])
        let manifest = pendingDirectory(root, requestID: requestID)
            .appendingPathComponent("manifest.json")
        let text = try XCTUnwrap(
            String(data: try Data(contentsOf: manifest), encoding: .utf8)
        )
        let newer = Data(
            text.replacingOccurrences(of: ".envelope.v1", with: ".envelope.v99").utf8
        )
        try newer.write(to: manifest)

        let companion = try launch(root: root, withPanel: true)
        let outcome = companion.shareAdopter.reconcile()
        XCTAssertEqual(outcome.preserved, [requestID])
        XCTAssertEqual(outcome.adopted, [])
        XCTAssertEqual(companion.accepted.batches.count, 0)
        XCTAssertEqual(try Data(contentsOf: manifest), newer)
        XCTAssertTrue(companion.shareInbox.isPending(requestID: requestID))
    }

    /// A full review/waiting owner is not an acknowledgement. The durable
    /// request remains in the App Group and the temporary companion copy is
    /// rolled back so a later pass can retry without losing or duplicating it.
    func testReviewCapacityLeavesTheDurableRequestForRetry() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let requestID = try publishShare(inbox, [("alpha.txt", "alpha")])
        let snapshots = try OwnedSnapshotStore(root: ownedRoot(root))
        let adopter = ShareInboxAdopter(
            inbox: inbox,
            snapshotStore: snapshots,
            openChooser: {},
            accept: { _ in false }
        )

        let outcome = adopter.reconcile()

        XCTAssertEqual(outcome.retried, [requestID])
        XCTAssertTrue(inbox.isPending(requestID: requestID))
        XCTAssertEqual(try ownedRequests(root), [])
    }

    // MARK: Several requests

    /// Two shares queued while the companion was closed are two selections:
    /// the second waits behind the first rather than merging into it. One
    /// bounded selection is adopted per pass, so the spool drains over ticks
    /// rather than freezing the first one.
    func testSecondQueuedRequestWaitsBehindTheFirst() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let first = try publishShare(inbox, [("alpha.txt", "alpha")])
        let second = try publishShare(inbox, [("beta.txt", "beta")])

        let companion = try launch(root: root, withPanel: true)
        let firstPass = companion.shareAdopter.reconcile()
        XCTAssertEqual(firstPass.adopted.count, 1)
        XCTAssertEqual(companion.shareInbox.pendingRequestIDs().count, 1)

        let secondPass = companion.shareAdopter.reconcile()
        XCTAssertEqual(
            Set(firstPass.adopted + secondPass.adopted),
            [first, second]
        )
        XCTAssertEqual(companion.accepted.batches.count, 2)
        XCTAssertEqual(companion.panel?.reviewItemCount, 1)
        XCTAssertEqual(companion.panel?.waitingSelectionCount, 1)
        XCTAssertEqual(companion.shareInbox.pendingRequestIDs(), [])
    }

    // MARK: Quitting

    /// A durable request that was never adopted stays durable: quitting takes
    /// no ownership of it.
    func testTerminatingTakesNoNewRequest() throws {
        let root = try temporaryDirectory()
        let requestID = try publishShare(try shareInbox(root), [("alpha.txt", "alpha")])
        let companion = try launch(root: root, withPanel: true)
        companion.shareAdopter.companionWillTerminate()

        XCTAssertEqual(companion.shareAdopter.reconcile(), ShareInboxReconciliation())
        XCTAssertEqual(companion.accepted.batches.count, 0)
        XCTAssertTrue(companion.shareInbox.isPending(requestID: requestID))
    }

    // MARK: The chooser continuation

    /// "Link an original…" opens the companion's own reviewed chooser. It
    /// carries no content and adopts nothing.
    func testChooserRequestOpensReviewExactlyOnce() throws {
        let root = try temporaryDirectory()
        try shareInbox(root).requestChooser()
        let companion = try launch(root: root, withPanel: true)

        XCTAssertTrue(companion.shareAdopter.reconcile().chooserOpened)
        XCTAssertEqual(companion.chooser.opens, 1)
        XCTAssertFalse(companion.shareAdopter.reconcile().chooserOpened)
        XCTAssertEqual(companion.chooser.opens, 1)
        XCTAssertEqual(companion.accepted.batches.count, 0)
    }
}
