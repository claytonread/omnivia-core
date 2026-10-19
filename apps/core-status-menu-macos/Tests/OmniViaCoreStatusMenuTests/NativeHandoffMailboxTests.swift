import Darwin
import DocumentIntakeMacOS
import Foundation
import XCTest

private final class NativePeerSocketTestResult: @unchecked Sendable {
    private let lock = NSLock()
    private var storedError: Error?

    func store(_ error: Error) { lock.withLock { storedError = error } }
    func error() -> Error? { lock.withLock { storedError } }
}

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

        try mailbox.storeAuthenticatedOffer(offer, now: now)
        XCTAssertNoThrow(try mailbox.storeAuthenticatedOffer(offer, now: now))
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

        XCTAssertThrowsError(try mailbox.storeAuthenticatedOffer(invalid, now: now))
    }

    func testAuthenticatedReplayRejectsAConflictingOfferWithTheSameNonce() throws {
        let root = try temporaryDirectory()
        let mailbox = try NativeHandoffMailbox(root: root)
        let now = Date(timeIntervalSince1970: floor(Date().timeIntervalSince1970))
        let nonce = UUID().uuidString.lowercased()
        let slot = UUID().uuidString.lowercased()
        let original = offer(nonce: nonce, slot: slot, displayName: "original.pdf", now: now)
        let conflicting = offer(nonce: nonce, slot: slot, displayName: "replacement.pdf", now: now)

        try mailbox.storeAuthenticatedOffer(original, now: now)
        try mailbox.writeAcknowledgement(NativeHandoffAcknowledgementV1(
            requestNonce: nonce,
            acknowledgedAt: now,
            adoptions: [.init(slotID: slot, candidateRef: "candidate-1", candidateRevision: 1)]
        ))

        XCTAssertThrowsError(try mailbox.storeAuthenticatedOffer(conflicting, now: now)) {
            XCTAssertEqual($0 as? NativeHandoffMailboxError, .invalidOffer)
        }
        XCTAssertEqual(mailbox.pendingOffers(now: now), [original])
    }

    func testPeerSocketRendezvousIsFixedInsideTheMailboxRoot() throws {
        let root = try temporaryDirectory()
        let mailbox = try NativeHandoffMailbox(root: root)
        XCTAssertEqual(mailbox.peerSocketURL, root.appendingPathComponent("h.sock"))
    }

    func testPeerSocketFramesPayloadAndReportsKernelPeerIdentity() throws {
        let root = try shortTemporaryDirectory()
        let socketURL = root.appendingPathComponent("h.sock")
        let listener = try NativePeerSocket.makeListener(at: socketURL)
        defer {
            Darwin.close(listener)
            _ = Darwin.unlink(socketURL.path)
        }
        let completed = expectation(description: "server handled framed request")
        let serverResult = NativePeerSocketTestResult()
        DispatchQueue.global().async {
            do {
                let peer = Darwin.accept(listener, nil, nil)
                guard peer >= 0 else { throw NativePeerSocketError.unavailable }
                defer { Darwin.close(peer) }
                let identity = try NativePeerSocket.peerIdentity(of: peer)
                guard identity.userID == geteuid(), identity.processID == getpid() else {
                    throw NativePeerSocketError.peerUnavailable
                }
                let request = try NativePeerSocket.readFrame(from: peer, maximumBytes: 64)
                guard request == Data("request".utf8) else {
                    throw NativePeerSocketError.truncatedFrame
                }
                try NativePeerSocket.writeFrame(Data("response".utf8), to: peer, maximumBytes: 64)
            } catch {
                serverResult.store(error)
            }
            completed.fulfill()
        }

        let client = try NativePeerSocket.connect(to: socketURL)
        defer { Darwin.close(client) }
        let serverIdentity = try NativePeerSocket.peerIdentity(of: client)
        XCTAssertEqual(serverIdentity.userID, geteuid())
        XCTAssertEqual(serverIdentity.processID, getpid())
        try NativePeerSocket.writeFrame(Data("request".utf8), to: client, maximumBytes: 64)
        XCTAssertEqual(
            try NativePeerSocket.readFrame(from: client, maximumBytes: 64),
            Data("response".utf8)
        )
        wait(for: [completed], timeout: 2)
        if let error = serverResult.error() { throw error }
    }

    func testPeerSocketRejectsOversizedOutboundFrame() throws {
        var descriptors = [Int32](repeating: -1, count: 2)
        XCTAssertEqual(Darwin.socketpair(AF_UNIX, SOCK_STREAM, 0, &descriptors), 0)
        defer { descriptors.forEach { Darwin.close($0) } }

        XCTAssertThrowsError(
            try NativePeerSocket.writeFrame(Data(repeating: 1, count: 65), to: descriptors[0], maximumBytes: 64)
        ) {
            XCTAssertEqual($0 as? NativePeerSocketError, .frameTooLarge)
        }
    }

    func testNativePeerRequirementRejectsInvalidInputs() throws {
        XCTAssertThrowsError(try NativePeerRequirement("")) {
            XCTAssertEqual($0 as? NativePeerRequirementError, .invalidRequirement)
        }
        XCTAssertThrowsError(try NativePeerRequirement("anchor apple").validate(processID: 0)) {
            XCTAssertEqual($0 as? NativePeerRequirementError, .peerRejected)
        }
    }

    func testAcknowledgementRejectsInvalidCandidateRevision() throws {
        let mailbox = try NativeHandoffMailbox(root: temporaryDirectory())
        let acknowledgement = NativeHandoffAcknowledgementV1(
            requestNonce: UUID().uuidString.lowercased(),
            acknowledgedAt: Date(),
            adoptions: [.init(
                slotID: UUID().uuidString.lowercased(),
                candidateRef: "candidate-1",
                candidateRevision: 0
            )]
        )

        XCTAssertThrowsError(try mailbox.writeAcknowledgement(acknowledgement)) {
            XCTAssertEqual($0 as? NativeHandoffMailboxError, .invalidAcknowledgement)
        }
    }

    private func offer(
        nonce: String,
        slot: String,
        displayName: String,
        now: Date
    ) -> NativeHandoffOfferV1 {
        NativeHandoffOfferV1(
            requestNonce: nonce,
            expectedOwnerBundleID: NativeHandoffMailbox.companionBundleID,
            senderBundleID: NativeHandoffMailbox.finderBundleID,
            surface: .finderQuickAction,
            createdAt: now,
            expiresAt: now.addingTimeInterval(120),
            slots: [.init(
                slotID: slot,
                displayName: displayName,
                sourceKind: .originalBookmark,
                bookmark: Data("bookmark".utf8)
            )]
        )
    }

    func testAFolderSlotIsCarriedByBookmarkAndOnlyByTheQuickAction() throws {
        let now = Date()
        func offer(
            surface: NativeHandoffSurface,
            sender: String,
            slot: NativeHandoffSlotV1
        ) -> NativeHandoffOfferV1 {
            NativeHandoffOfferV1(
                requestNonce: UUID().uuidString.lowercased(),
                expectedOwnerBundleID: NativeHandoffMailbox.companionBundleID,
                senderBundleID: sender,
                surface: surface,
                createdAt: now,
                expiresAt: now.addingTimeInterval(60),
                slots: [slot]
            )
        }
        let folder = NativeHandoffSlotV1(
            slotID: UUID().uuidString.lowercased(),
            displayName: "Briefs",
            sourceKind: .folderBookmark,
            bookmark: Data("bookmark".utf8)
        )

        try NativeHandoffMailbox.validate(
            offer(surface: .finderQuickAction, sender: NativeHandoffMailbox.finderBundleID, slot: folder),
            now: now
        )
        // Share offers owned bytes and nothing else, so it may not claim a folder.
        XCTAssertThrowsError(try NativeHandoffMailbox.validate(
            offer(surface: .share, sender: NativeHandoffMailbox.shareBundleID, slot: folder),
            now: now
        ))
        // A folder claim carries one bookmark and no transfer blob.
        for broken in [
            NativeHandoffSlotV1(slotID: folder.slotID, displayName: "Briefs", sourceKind: .folderBookmark),
            NativeHandoffSlotV1(slotID: folder.slotID, displayName: "Briefs", sourceKind: .folderBookmark, bookmark: Data()),
            NativeHandoffSlotV1(
                slotID: folder.slotID,
                displayName: "Briefs",
                sourceKind: .folderBookmark,
                bookmark: Data("bookmark".utf8),
                snapshotRelativePath: "blob-1"
            ),
        ] {
            XCTAssertThrowsError(try NativeHandoffMailbox.validate(
                offer(surface: .finderQuickAction, sender: NativeHandoffMailbox.finderBundleID, slot: broken),
                now: now
            ))
        }
    }

    func testAnOfferCarryingAnUnknownSourceKindFailsClosed() throws {
        let now = Date()
        let mailbox = try NativeHandoffMailbox(root: try temporaryDirectory())
        let encoded = try mailbox.encodedOffer(
            NativeHandoffOfferV1(
                requestNonce: UUID().uuidString.lowercased(),
                expectedOwnerBundleID: NativeHandoffMailbox.companionBundleID,
                senderBundleID: NativeHandoffMailbox.finderBundleID,
                surface: .finderQuickAction,
                createdAt: now,
                expiresAt: now.addingTimeInterval(60),
                slots: [NativeHandoffSlotV1(
                    slotID: UUID().uuidString.lowercased(),
                    displayName: "Briefs",
                    sourceKind: .folderBookmark,
                    bookmark: Data("bookmark".utf8)
                )]
            ),
            now: now
        )
        let mutated = Data(
            String(decoding: encoded, as: UTF8.self)
                .replacingOccurrences(of: "folder_bookmark", with: "folder_alias")
                .utf8
        )

        XCTAssertThrowsError(try mailbox.decodeOffer(mutated, now: now))
    }

    /// The mailbox's half of the migration round trip: a rolled-back companion
    /// that is handed an offer from a schema it does not know refuses it whole
    /// and leaves the offer it already holds exactly as it was written.
    func testAnOfferFromANewerSchemaIsBlockedWithoutRewritingTheMailbox() throws {
        let root = try temporaryDirectory()
        let mailbox = try NativeHandoffMailbox(root: root)
        let now = Date(timeIntervalSince1970: floor(Date().timeIntervalSince1970))
        let nonce = UUID().uuidString.lowercased()
        let offer = NativeHandoffOfferV1(
            requestNonce: nonce,
            expectedOwnerBundleID: NativeHandoffMailbox.companionBundleID,
            senderBundleID: NativeHandoffMailbox.finderBundleID,
            surface: .finderQuickAction,
            createdAt: now,
            expiresAt: now.addingTimeInterval(60),
            slots: [NativeHandoffSlotV1(
                slotID: UUID().uuidString.lowercased(),
                displayName: "brief.pdf",
                sourceKind: .originalBookmark,
                bookmark: Data("bookmark".utf8)
            )]
        )
        try mailbox.storeAuthenticatedOffer(offer, now: now)
        let stored = root
            .appendingPathComponent("offers", isDirectory: true)
            .appendingPathComponent("\(nonce).json", isDirectory: false)
        let bytes = try Data(contentsOf: stored)

        let newer = Data(
            String(decoding: try mailbox.encodedOffer(offer, now: now), as: UTF8.self)
                .replacingOccurrences(
                    of: "omnivia.native-handoff.offer.v1",
                    with: "omnivia.native-handoff.offer.v2"
                )
                .utf8
        )

        XCTAssertThrowsError(try mailbox.decodeOffer(newer, now: now)) {
            XCTAssertEqual($0 as? NativeHandoffMailboxError, .invalidOffer)
        }
        XCTAssertEqual(try Data(contentsOf: stored), bytes)
        XCTAssertEqual(mailbox.pendingOffers(now: now), [offer])
    }

    /// The same offer, but sitting in the mailbox: sweeping pending offers must treat a newer
    /// schema's offer as someone else's, not as junk to delete along with its snapshots.
    func testPendingOffersSweepLeavesANewerSchemasOfferAndItsSnapshotsInPlace() throws {
        let root = try temporaryDirectory()
        let mailbox = try NativeHandoffMailbox(root: root)
        let now = Date(timeIntervalSince1970: floor(Date().timeIntervalSince1970))
        func offer() -> NativeHandoffOfferV1 {
            NativeHandoffOfferV1(
                requestNonce: UUID().uuidString.lowercased(),
                expectedOwnerBundleID: NativeHandoffMailbox.companionBundleID,
                senderBundleID: NativeHandoffMailbox.finderBundleID,
                surface: .finderQuickAction,
                createdAt: now,
                expiresAt: now.addingTimeInterval(60),
                slots: [NativeHandoffSlotV1(
                    slotID: UUID().uuidString.lowercased(),
                    displayName: "brief.pdf",
                    sourceKind: .originalBookmark,
                    bookmark: Data("bookmark".utf8)
                )]
            )
        }
        let current = offer()
        let newer = offer()
        try mailbox.storeAuthenticatedOffer(current, now: now)
        let newerFile = root
            .appendingPathComponent("offers", isDirectory: true)
            .appendingPathComponent("\(newer.requestNonce).json", isDirectory: false)
        let newerBytes = Data(
            String(decoding: try mailbox.encodedOffer(newer, now: now), as: UTF8.self)
                .replacingOccurrences(
                    of: "omnivia.native-handoff.offer.v1",
                    with: "omnivia.native-handoff.offer.v2"
                )
                .utf8
        )
        try newerBytes.write(to: newerFile)
        let snapshots = try mailbox.prepareSnapshotDirectory(nonce: newer.requestNonce)

        XCTAssertEqual(mailbox.pendingOffers(now: now), [current])

        XCTAssertEqual(try Data(contentsOf: newerFile), newerBytes)
        XCTAssertTrue(FileManager.default.fileExists(atPath: snapshots.path))
        XCTAssertNil(mailbox.pendingOffer(nonce: newer.requestNonce, now: now))
    }

    func testAnExtensionCompletesOnlyOnAdoptionOfEveryOfferedSlot() {
        let offered = (0..<3).map { _ in UUID().uuidString.lowercased() }

        XCTAssertTrue(NativeHandoffMailbox.adoptionSatisfiesOffer(
            offeredSlotIDs: offered,
            acknowledgedSlotIDs: offered
        ))
        // Partial, reordered, padded, empty and unoffered acknowledgements are
        // not adoption: the action keeps its items and stays open for a retry.
        XCTAssertFalse(NativeHandoffMailbox.adoptionSatisfiesOffer(
            offeredSlotIDs: offered,
            acknowledgedSlotIDs: Array(offered.dropLast())
        ))
        XCTAssertFalse(NativeHandoffMailbox.adoptionSatisfiesOffer(
            offeredSlotIDs: offered,
            acknowledgedSlotIDs: offered.reversed()
        ))
        XCTAssertFalse(NativeHandoffMailbox.adoptionSatisfiesOffer(
            offeredSlotIDs: offered,
            acknowledgedSlotIDs: offered + [UUID().uuidString.lowercased()]
        ))
        XCTAssertFalse(NativeHandoffMailbox.adoptionSatisfiesOffer(
            offeredSlotIDs: offered,
            acknowledgedSlotIDs: []
        ))
        XCTAssertFalse(NativeHandoffMailbox.adoptionSatisfiesOffer(
            offeredSlotIDs: [],
            acknowledgedSlotIDs: []
        ))
    }

    func testAnOfferBeyondTheSlotBoundIsNeverSatisfied() {
        let offered = (0...NativeHandoffMailbox.maximumSlots)
            .map { _ in UUID().uuidString.lowercased() }

        XCTAssertFalse(NativeHandoffMailbox.adoptionSatisfiesOffer(
            offeredSlotIDs: offered,
            acknowledgedSlotIDs: offered
        ))
    }

    func testARepeatedSlotIsNeverSatisfied() {
        let slot = UUID().uuidString.lowercased()

        XCTAssertFalse(NativeHandoffMailbox.adoptionSatisfiesOffer(
            offeredSlotIDs: [slot, slot],
            acknowledgedSlotIDs: [slot, slot]
        ))
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    private func shortTemporaryDirectory() throws -> URL {
        let suffix = UUID().uuidString.prefix(8).lowercased()
        let url = URL(
            fileURLWithPath: "/private/tmp/ov-\(getpid())-\(suffix)",
            isDirectory: true
        )
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: false)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
