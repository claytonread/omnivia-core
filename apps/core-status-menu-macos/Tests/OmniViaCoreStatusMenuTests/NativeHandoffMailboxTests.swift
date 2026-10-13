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
