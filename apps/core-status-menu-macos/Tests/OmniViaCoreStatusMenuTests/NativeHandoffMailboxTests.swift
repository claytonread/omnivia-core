import DocumentIntakeMacOS
import Foundation
import XCTest

final class NativeHandoffMailboxTests: XCTestCase {
    func testOfferRoundTripAndAcknowledgementAreBoundToNonce() throws {
        let root = try temporaryDirectory()
        let mailbox = try NativeHandoffMailbox(root: root)
        let now = Date(timeIntervalSince1970: floor(Date().timeIntervalSince1970))
        let nonce = UUID().uuidString.lowercased()
        let slot = UUID().uuidString.lowercased()
        let offer = NativeHandoffOfferV1(
            requestNonce: nonce,
            expectedOwnerBundleID: NativeHandoffMailbox.companionBundleID,
            senderBundleID: NativeHandoffMailbox.finderBundleID,
            surface: .finderQuickAction,
            createdAt: now,
            expiresAt: now.addingTimeInterval(120),
            slots: [NativeHandoffSlotV1(
                slotID: slot,
                displayName: "brief.pdf",
                sourceKind: .originalBookmark,
                bookmark: Data("bookmark".utf8)
            )]
        )

        try mailbox.writeOffer(offer, now: now)
        XCTAssertEqual(mailbox.pendingOffers(now: now), [offer])

        let acknowledgement = NativeHandoffAcknowledgementV1(
            requestNonce: nonce,
            acknowledgedAt: now,
            adoptions: [.init(slotID: slot, candidateRef: "candidate-1", candidateRevision: 1)]
        )
        try mailbox.writeAcknowledgement(acknowledgement)
        XCTAssertEqual(mailbox.acknowledgement(nonce: nonce), acknowledgement)
    }

    func testRejectsExpiredMismatchedAndTraversalOffers() throws {
        let root = try temporaryDirectory()
        let mailbox = try NativeHandoffMailbox(root: root)
        let now = Date(timeIntervalSince1970: floor(Date().timeIntervalSince1970))
        let nonce = UUID().uuidString.lowercased()
        let slot = UUID().uuidString.lowercased()
        let invalid = NativeHandoffOfferV1(
            requestNonce: nonce,
            expectedOwnerBundleID: "attacker.example",
            senderBundleID: NativeHandoffMailbox.shareBundleID,
            surface: .share,
            createdAt: now.addingTimeInterval(-240),
            expiresAt: now.addingTimeInterval(-120),
            slots: [.init(
                slotID: slot,
                displayName: "shared.txt",
                sourceKind: .capturedSnapshot,
                snapshotRelativePath: "../shared.txt"
            )]
        )

        XCTAssertThrowsError(try mailbox.writeOffer(invalid, now: now))
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
