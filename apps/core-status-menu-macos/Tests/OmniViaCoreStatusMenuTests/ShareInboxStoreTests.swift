import Foundation
import XCTest
@testable import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

/// The durable, snapshot-only Share spool itself: what becomes visible, when,
/// and what is never touched.
@MainActor
final class ShareInboxStoreTests: NativeHandoffTestCase {
    // MARK: Atomic publication

    /// A share that crashed after writing its bytes but before the one
    /// publishing step leaves nothing a companion can see.
    func testStagedRequestIsInvisibleUntilItIsPublished() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let staged = try stageShare(inbox, [("alpha.txt", "alpha")])

        XCTAssertEqual(inbox.pendingRequestIDs(), [])
        XCTAssertFalse(inbox.isPending(requestID: staged.id))
        XCTAssertNil(inbox.envelope(requestID: staged.id))
        // The bytes are still there, so the same share could still finish.
        XCTAssertTrue(FileManager.default.fileExists(atPath: staged.directory.path))
    }

    func testPublishedRequestIsCompleteAndReadable() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let requestID = try publishShare(inbox, [("alpha.txt", "alpha"), ("beta.txt", "beta")])

        XCTAssertEqual(inbox.pendingRequestIDs(), [requestID])
        let envelope = try XCTUnwrap(inbox.envelope(requestID: requestID))
        XCTAssertEqual(envelope.items.map(\.displayName), ["alpha.txt", "beta.txt"])
        XCTAssertEqual(envelope.items.map(\.byteLength), [5, 4])
        for item in envelope.items {
            let url = try inbox.payloadURL(requestID: requestID, relativePath: item.relativePath)
            XCTAssertEqual(
                ShareInboxStore.digest(of: try Data(contentsOf: url)),
                item.digest
            )
        }
    }

    /// Nothing partial ever appears: a manifest that does not match the bytes
    /// on disk is refused before the publishing step, and the request stays
    /// invisible.
    func testPublishRefusesAManifestThatDoesNotMatchTheBytes() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let staged = try stageShare(inbox, [("alpha.txt", "alpha")])
        let lying = ShareInboxItemV1(
            slotID: staged.items[0].slotID,
            displayName: "alpha.txt",
            relativePath: staged.items[0].relativePath,
            kind: .file,
            byteLength: 99,
            digest: staged.items[0].digest
        )
        XCTAssertThrowsError(try inbox.publish(ShareInboxEnvelopeV1(
            requestID: staged.id,
            senderBundleID: NativeHandoffMailbox.shareBundleID,
            createdAt: Date(),
            items: [lying]
        )))
        XCTAssertEqual(inbox.pendingRequestIDs(), [])
    }

    func testPublishRefusesWrongDigestEvenWhenLengthMatches() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let staged = try stageShare(inbox, [("alpha.txt", "alpha")])
        let lying = ShareInboxItemV1(
            slotID: staged.items[0].slotID,
            displayName: "alpha.txt",
            relativePath: staged.items[0].relativePath,
            kind: .file,
            byteLength: staged.items[0].byteLength,
            digest: sha256("bravo")
        )
        XCTAssertThrowsError(try inbox.publish(ShareInboxEnvelopeV1(
            requestID: staged.id,
            senderBundleID: NativeHandoffMailbox.shareBundleID,
            createdAt: Date(),
            items: [lying]
        ))) { error in
            XCTAssertEqual(error as? ShareInboxError, .invalidEnvelope)
        }
        XCTAssertEqual(inbox.pendingRequestIDs(), [])
    }

    func testPublishRefusesAnotherSender() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let staged = try stageShare(inbox, [("alpha.txt", "alpha")])
        XCTAssertThrowsError(try inbox.publish(ShareInboxEnvelopeV1(
            requestID: staged.id,
            senderBundleID: NativeHandoffMailbox.finderBundleID,
            createdAt: Date(),
            items: staged.items
        )))
        XCTAssertEqual(inbox.pendingRequestIDs(), [])
    }

    // MARK: Cancellation

    /// Cancelling before the publish drops the bytes; a published request is
    /// the person's and survives the share sheet closing.
    func testAbandonDropsOnlyUnpublishedBytes() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let staged = try stageShare(inbox, [("alpha.txt", "alpha")])
        inbox.abandon(requestID: staged.id)
        XCTAssertFalse(FileManager.default.fileExists(atPath: staged.directory.path))

        let published = try publishShare(inbox, [("beta.txt", "beta")])
        inbox.abandon(requestID: published)
        XCTAssertEqual(inbox.pendingRequestIDs(), [published])
    }

    // MARK: Bounds

    func testPendingRequestCountIsBounded() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        for _ in 0 ..< ShareInboxStore.maximumPendingRequests {
            _ = try publishShare(inbox, [("alpha.txt", "alpha")])
        }
        XCTAssertEqual(inbox.pendingRequestIDs().count, ShareInboxStore.maximumPendingRequests)
        XCTAssertThrowsError(try inbox.beginRequest()) { error in
            XCTAssertEqual(error as? ShareInboxError, .inboxFull)
        }
        // Nothing already durable is dropped to make room.
        XCTAssertEqual(inbox.pendingRequestIDs().count, ShareInboxStore.maximumPendingRequests)
    }

    /// A request being staged counts against the bound too, so two shares
    /// racing to open a request cannot together exceed it.
    func testStagingRequestsCountTowardTheBound() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        var opened: [ShareInboxRequest] = []
        while let request = try? inbox.beginRequest() {
            opened.append(request)
        }
        XCTAssertFalse(opened.isEmpty)
        XCTAssertLessThanOrEqual(opened.count, ShareInboxStore.maximumPendingRequests)
        XCTAssertEqual(Set(opened.map(\.requestID)).count, opened.count)
        XCTAssertThrowsError(try inbox.beginRequest())
    }

    /// The cross-process lock makes the quota decision exact even when a burst
    /// of extension hosts observes the same App Group at once.
    func testConcurrentRequestsNeverShareADirectoryOrOvershootQuota() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let lock = NSLock()
        var opened: [String] = []
        var refused = 0
        DispatchQueue.concurrentPerform(
            iterations: ShareInboxStore.maximumPendingRequests * 2
        ) { _ in
            do {
                let request = try inbox.beginRequest()
                lock.withLock { opened.append(request.requestID) }
            } catch {
                lock.withLock { refused += 1 }
            }
        }
        XCTAssertEqual(Set(opened).count, opened.count)
        XCTAssertGreaterThan(refused, 0)
        XCTAssertLessThanOrEqual(opened.count, ShareInboxStore.maximumPendingRequests)
        XCTAssertEqual(opened.count + refused, ShareInboxStore.maximumPendingRequests * 2)
        // Whatever the burst did, the next request is refused.
        XCTAssertThrowsError(try inbox.beginRequest()) { error in
            XCTAssertEqual(error as? ShareInboxError, .inboxFull)
        }
    }

    func testEnvelopeOverTheSelectionBoundIsRefused() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let staged = try stageShare(inbox, [("alpha.txt", "alpha")])
        let oversize = (0 ..< 5).map { index in
            ShareInboxItemV1(
                slotID: index == 0 ? staged.items[0].slotID : newID(),
                displayName: "big-\(index).bin",
                relativePath: "p\(index + 1)-\(UUID().uuidString.lowercased())",
                kind: .file,
                byteLength: ShareInboxStore.maximumItemBytes,
                digest: sha256("big")
            )
        }
        XCTAssertThrowsError(try inbox.publish(ShareInboxEnvelopeV1(
            requestID: staged.id,
            senderBundleID: NativeHandoffMailbox.shareBundleID,
            createdAt: Date(),
            items: oversize
        ))) { error in
            XCTAssertEqual(error as? ShareInboxError, .selectionTooLarge)
        }
    }

    func testMoreSlotsThanTheBoundAreRefused() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let staged = try stageShare(inbox, [("alpha.txt", "alpha")])
        let items = (0 ... ShareInboxStore.maximumItems).map { index in
            ShareInboxItemV1(
                slotID: newID(),
                displayName: "item-\(index).txt",
                relativePath: "p\(index + 1)-\(UUID().uuidString.lowercased())",
                kind: .file,
                byteLength: 1,
                digest: sha256("x")
            )
        }
        XCTAssertThrowsError(try inbox.publish(ShareInboxEnvelopeV1(
            requestID: staged.id,
            senderBundleID: NativeHandoffMailbox.shareBundleID,
            createdAt: Date(),
            items: items
        )))
    }

    // MARK: Unknown and newer schemas

    /// A manifest this build does not understand is left exactly as it is, so
    /// a newer companion can still adopt it. It is still reported as pending,
    /// so it still counts against the bound.
    func testNewerSchemaIsPreservedByteForByte() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let requestID = try publishShare(inbox, [("alpha.txt", "alpha")])
        let manifest = pendingDirectory(root, requestID: requestID)
            .appendingPathComponent("manifest.json")
        let text = try XCTUnwrap(
            String(data: try Data(contentsOf: manifest), encoding: .utf8)
        )
        let newer = Data(
            text.replacingOccurrences(of: ".envelope.v1", with: ".envelope.v2").utf8
        )
        try newer.write(to: manifest)

        XCTAssertNil(inbox.envelope(requestID: requestID))
        XCTAssertTrue(inbox.isPending(requestID: requestID))
        XCTAssertEqual(inbox.pendingRequestIDs(), [requestID])
        _ = inbox.reconcile()
        XCTAssertEqual(try Data(contentsOf: manifest), newer)
    }

    /// An unknown field is as foreign as an unknown schema: this build does not
    /// read the envelope half-way and does not rewrite it.
    func testUnknownFieldIsPreservedRatherThanIgnored() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let requestID = try publishShare(inbox, [("alpha.txt", "alpha")])
        let manifest = pendingDirectory(root, requestID: requestID)
            .appendingPathComponent("manifest.json")
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: try Data(contentsOf: manifest)) as? [String: Any]
        )
        object["destinationWorkspace"] = "ws-somewhere"
        let rewritten = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        try rewritten.write(to: manifest)

        XCTAssertNil(inbox.envelope(requestID: requestID))
        XCTAssertEqual(try Data(contentsOf: manifest), rewritten)
    }

    // MARK: Staging reconciliation

    func testReconcileClearsOnlyAbandonedStagingDirectories() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let fresh = try stageShare(inbox, [("alpha.txt", "alpha")])
        let published = try publishShare(inbox, [("beta.txt", "beta")])

        XCTAssertEqual(inbox.reconcile(), 0)
        XCTAssertTrue(FileManager.default.fileExists(atPath: fresh.directory.path))

        let later = Date().addingTimeInterval(ShareInboxStore.abandonedStagingAge + 60)
        XCTAssertEqual(inbox.reconcile(now: later), 1)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fresh.directory.path))
        // A published request is never cleared by age.
        XCTAssertEqual(inbox.pendingRequestIDs(), [published])
    }

    // MARK: Paths

    func testPayloadPathsAreConfinedToTheRequestDirectory() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        let staged = try stageShare(inbox, [("alpha.txt", "alpha")])
        for bad in ["../escape", "sub/dir", ".hidden", "", "manifest.json/.."] {
            XCTAssertThrowsError(
                try inbox.payloadDestination(
                    ShareInboxRequest(requestID: staged.id, directory: staged.directory),
                    relativePath: bad
                )
            )
            XCTAssertThrowsError(
                try inbox.payloadURL(requestID: staged.id, relativePath: bad)
            )
        }
        XCTAssertThrowsError(try inbox.payloadURL(requestID: "not-a-nonce", relativePath: "p1-x"))
    }

    // MARK: The chooser continuation

    func testChooserRequestIsTakenExactlyOnce() throws {
        let root = try temporaryDirectory()
        let inbox = try shareInbox(root)
        XCTAssertFalse(inbox.takeChooserRequest())
        try inbox.requestChooser()
        XCTAssertTrue(inbox.takeChooserRequest())
        XCTAssertFalse(inbox.takeChooserRequest())
        // It carries no content, so it never looks like a pending share.
        XCTAssertEqual(inbox.pendingRequestIDs(), [])
    }
}
