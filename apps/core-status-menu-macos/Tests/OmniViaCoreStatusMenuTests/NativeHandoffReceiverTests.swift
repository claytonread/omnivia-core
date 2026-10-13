import Foundation
import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

/// Crash safety and exactness of native handoff adoption. The durable record is
/// the commit point: before it a failure is rolled back to the bare
/// reservation, from it nothing is rolled back, and every replay, reconcile or
/// restart converges on the same candidate refs, revisions, grants and owned
/// copies without showing a live candidate twice.
@MainActor
final class NativeHandoffReceiverTests: NativeHandoffTestCase {
    // MARK: Checkpoint scenarios

    func testFailedGrantRollsBackAndTheReplayAdoptsExactlyOneGrant() async throws {
        let root = try temporaryDirectory()
        let source = try writeOriginal(root, name: "brief.pdf")
        let nonce = newID()
        let slot = newID()
        let encoded = try appGroup(root).encodedOffer(
            try finderOffer(nonce: nonce, slotIDs: [slot], urls: [source])
        )

        let crashing = try launch(root: root, bookmarkFailures: 1)
        await failure { try await crashing.receiver.submitAuthenticatedOffer(encoded) }
        XCTAssertEqual(try crashing.candidates.load(nativeRequestID: nonce)?.map(\.state), [.needsAccess])
        XCTAssertTrue(crashing.environment.accessStore.descriptors().isEmpty)
        XCTAssertNil(crashing.mailbox.acknowledgement(nonce: nonce))
        XCTAssertTrue(crashing.accepted.batches.isEmpty)

        let restarted = try launch(root: root)
        let acknowledgement = try restarted.mailbox.decodeAcknowledgement(
            try await restarted.receiver.submitAuthenticatedOffer(encoded),
            expectedNonce: nonce
        )
        XCTAssertEqual(acknowledgement.adoptions, adoptions(nonce: nonce, slots: [slot]))
        XCTAssertEqual(restarted.connectedAccessRefs, ["candidate-access-\(nonce)-\(slot)"])
        XCTAssertEqual(restarted.environment.accessStore.descriptors().map(\.generation), [1])
        XCTAssertEqual(try restarted.candidates.load(nativeRequestID: nonce)?.map(\.state), [.adopted])
        XCTAssertEqual(restarted.accepted.batches.count, 1)
    }

    func testFinderLostAcknowledgementReconcilesAgainstTheSameAdoption() async throws {
        let root = try temporaryDirectory()
        let source = try writeOriginal(root, name: "brief.pdf")
        let nonce = newID()
        let slot = newID()
        let encoded = try appGroup(root).encodedOffer(
            try finderOffer(nonce: nonce, slotIDs: [slot], urls: [source])
        )

        let first = try launch(root: root)
        let acknowledged = try first.mailbox.decodeAcknowledgement(
            try await first.receiver.submitAuthenticatedOffer(encoded),
            expectedNonce: nonce
        )
        XCTAssertEqual(first.accepted.batches.count, 1)

        // The acknowledgement never reached durable storage, and the implicit
        // transfer grant is gone with the host: reconciliation has only the
        // companion's own adoption record to work from.
        first.mailbox.removeAcknowledgement(nonce: nonce)
        try FileManager.default.removeItem(at: source)

        let restarted = try launch(root: root)
        restarted.receiver.reconcile()
        let replayed = try XCTUnwrap(restarted.mailbox.acknowledgement(nonce: nonce))
        XCTAssertEqual(replayed.adoptions, acknowledged.adoptions)
        XCTAssertEqual(restarted.connectedAccessRefs, ["candidate-access-\(nonce)-\(slot)"])
        XCTAssertEqual(restarted.environment.accessStore.descriptors().map(\.generation), [1])
        // Review is process memory: the new process shows the candidate once.
        XCTAssertEqual(restarted.accepted.batches.count, 1)
        XCTAssertEqual(
            restarted.accepted.batches[0].map(\.url.path),
            first.accepted.batches[0].map(\.url.path)
        )
        XCTAssertEqual(
            restarted.accepted.batches[0].map(\.source),
            first.accepted.batches[0].map(\.source)
        )
        XCTAssertEqual(
            restarted.accepted.batches[0].map(\.native),
            first.accepted.batches[0].map(\.native)
        )
    }

    func testShareFailedStagingRollsBackAndTheReplayStagesOneOwnedCopyPerSlot() async throws {
        let root = try temporaryDirectory()
        let nonce = newID()
        let slots = [newID(), newID()]
        let transfer = try writeTransferBlobs(root, nonce: nonce)
        let encoded = try appGroup(root).encodedOffer(shareOffer(nonce: nonce, slotIDs: slots))

        // The second transfer blob is unreadable, so staging fails after the
        // reservation is written but before anything is adopted.
        try FileManager.default.removeItem(at: transfer.appendingPathComponent("blob-2"))
        let crashing = try launch(root: root)
        await failure { try await crashing.receiver.submitAuthenticatedOffer(encoded) }
        XCTAssertEqual(
            try crashing.candidates.load(nativeRequestID: nonce)?.map(\.state),
            [.needsAccess, .needsAccess]
        )
        XCTAssertNil(crashing.mailbox.acknowledgement(nonce: nonce))
        XCTAssertTrue(crashing.accepted.batches.isEmpty)
        XCTAssertEqual(try ownedRequests(root), [], "the half-written copy is rolled back")

        try Data("beta".utf8).write(to: transfer.appendingPathComponent("blob-2"))
        let restarted = try launch(root: root)
        let acknowledgement = try restarted.mailbox.decodeAcknowledgement(
            try await restarted.receiver.submitAuthenticatedOffer(encoded),
            expectedNonce: nonce
        )
        XCTAssertEqual(acknowledgement.adoptions, adoptions(nonce: nonce, slots: slots))
        XCTAssertEqual(try ownedRequests(root), [nonce])
        XCTAssertEqual(try ownedFiles(root, nonce: nonce), ["1-alpha.txt", "2-beta.txt"])
        XCTAssertEqual(restarted.accepted.batches.count, 1)
        XCTAssertEqual(
            restarted.accepted.batches[0].map(\.url.lastPathComponent),
            ["1-alpha.txt", "2-beta.txt"]
        )
        XCTAssertFalse(FileManager.default.fileExists(atPath: transfer.path))
    }

    func testShareLostAcknowledgementRestoresCandidatesWithoutCopyingAgain() async throws {
        let root = try temporaryDirectory()
        let nonce = newID()
        let slots = [newID(), newID()]
        let transfer = try writeTransferBlobs(root, nonce: nonce)
        let encoded = try appGroup(root).encodedOffer(shareOffer(nonce: nonce, slotIDs: slots))

        let first = try launch(root: root)
        let acknowledged = try first.mailbox.decodeAcknowledgement(
            try await first.receiver.submitAuthenticatedOffer(encoded),
            expectedNonce: nonce
        )
        XCTAssertFalse(FileManager.default.fileExists(atPath: transfer.path))
        let owned = ownedURL(root, nonce: nonce, name: "1-alpha.txt")
        let modified = try owned.resourceValues(forKeys: [.contentModificationDateKey])
            .contentModificationDate

        first.mailbox.removeAcknowledgement(nonce: nonce)

        let restarted = try launch(root: root)
        restarted.receiver.reconcile()
        let replayed = try XCTUnwrap(restarted.mailbox.acknowledgement(nonce: nonce))
        XCTAssertEqual(replayed.adoptions, acknowledged.adoptions)
        XCTAssertEqual(try ownedRequests(root), [nonce])
        XCTAssertEqual(try ownedFiles(root, nonce: nonce), ["1-alpha.txt", "2-beta.txt"])
        XCTAssertEqual(
            try owned.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate,
            modified,
            "the owned copy is restored, not copied again"
        )
        XCTAssertEqual(restarted.accepted.batches.count, 1)
        XCTAssertEqual(
            restarted.accepted.batches[0].map(\.url.path),
            first.accepted.batches[0].map(\.url.path)
        )
        let staged = restarted.accepted.batches[0].map { try? Data(contentsOf: $0.url) }
        XCTAssertEqual(staged, [Data("alpha".utf8), Data("beta".utf8)])
    }

    func testAdoptedCandidatesAreRestoredAfterTheOfferIsGone() async throws {
        let root = try temporaryDirectory()
        let nonce = newID()
        let slots = [newID(), newID()]
        try writeTransferBlobs(root, nonce: nonce)
        let encoded = try appGroup(root).encodedOffer(shareOffer(nonce: nonce, slotIDs: slots))

        let first = try launch(root: root)
        let acknowledged = try first.mailbox.decodeAcknowledgement(
            try await first.receiver.submitAuthenticatedOffer(encoded),
            expectedNonce: nonce
        )

        // The acknowledgement was lost and the transfer offer has since passed
        // its deadline. An adopted candidate is bounded by its own request, not
        // by that deadline, so it must still be restored.
        first.mailbox.removeAcknowledgement(nonce: nonce)
        first.mailbox.removeOffer(nonce: nonce, removeSnapshots: true)

        let restarted = try launch(root: root)
        restarted.receiver.reconcile()
        let replayed = try XCTUnwrap(restarted.mailbox.acknowledgement(nonce: nonce))
        XCTAssertEqual(replayed.adoptions, acknowledged.adoptions)
        XCTAssertEqual(try ownedRequests(root), [nonce])
        XCTAssertEqual(try ownedFiles(root, nonce: nonce), ["1-alpha.txt", "2-beta.txt"])
        XCTAssertEqual(restarted.accepted.batches.count, 1)
        XCTAssertEqual(
            restarted.accepted.batches[0].map(\.url.path),
            first.accepted.batches[0].map(\.url.path)
        )
    }

    // MARK: Failure at every durable boundary

    private enum Recovery: CaseIterable {
        /// The same process keeps running and its timer reconciles.
        case reconcile
        /// The process restarts and the extension replays the exact offer.
        case restartAndReplay
    }

    func testFinderFailureAtEveryBoundaryConvergesOnTheSameAdoption() async throws {
        let boundaries: [(step: NativeHandoffStep, committed: Bool)] = [
            (.reserved, false),
            (.accessEstablished(slot: 0), false),
            (.accessEstablished(slot: 1), false),
            (.adopted, true),
            (.presented, true),
            (.acknowledged, true),
        ]
        for (step, committed) in boundaries {
            for recovery in Recovery.allCases {
                let label = "\(step) then \(recovery)"
                let root = try temporaryDirectory()
                let sources = [
                    try writeOriginal(root, name: "one.pdf"),
                    try writeOriginal(root, name: "two.pdf"),
                ]
                let nonce = newID()
                let slots = [newID(), newID()]
                let encoded = try appGroup(root).encodedOffer(
                    try finderOffer(nonce: nonce, slotIDs: slots, urls: sources)
                )
                let refs = slots.map { "candidate-access-\(nonce)-\($0)" }.sorted()

                let first = try launch(root: root)
                first.receiver.checkpoint = { if $0 == step { throw InjectedFailure() } }
                let error = await failure { try await first.receiver.submitAuthenticatedOffer(encoded) }
                XCTAssertEqual(error as? InjectedFailure, InjectedFailure(), label)

                // What the failure left behind.
                XCTAssertEqual(
                    try first.candidates.load(nativeRequestID: nonce)?.map(\.state),
                    committed ? [.adopted, .adopted] : [.needsAccess, .needsAccess],
                    label
                )
                XCTAssertEqual(
                    first.connectedAccessRefs,
                    committed ? refs : [],
                    committed
                        ? "\(label): past the commit point nothing is rolled back"
                        : "\(label): before it nothing is left behind"
                )
                XCTAssertEqual(
                    first.mailbox.acknowledgement(nonce: nonce) != nil,
                    step == .acknowledged,
                    label
                )
                let shownBeforeRecovery = (step == .presented || step == .acknowledged) ? 1 : 0
                XCTAssertEqual(first.accepted.batches.count, shownBeforeRecovery, label)

                first.receiver.checkpoint = { _ in }
                let recovered: Companion
                switch recovery {
                case .reconcile:
                    recovered = first
                    recovered.receiver.reconcile()
                case .restartAndReplay:
                    recovered = try launch(root: root)
                    let replay = try await recovered.receiver.submitAuthenticatedOffer(encoded)
                    XCTAssertEqual(
                        try recovered.mailbox.decodeAcknowledgement(replay, expectedNonce: nonce)
                            .adoptions,
                        adoptions(nonce: nonce, slots: slots),
                        label
                    )
                }

                XCTAssertEqual(
                    recovered.mailbox.acknowledgement(nonce: nonce)?.adoptions,
                    adoptions(nonce: nonce, slots: slots),
                    label
                )
                XCTAssertEqual(recovered.connectedAccessRefs, refs, label)
                XCTAssertEqual(
                    recovered.environment.accessStore.descriptors().map(\.accessRef),
                    refs,
                    "\(label): exactly one grant per slot"
                )
                XCTAssertEqual(
                    try recovered.candidates.load(nativeRequestID: nonce)?.map(\.state),
                    [.adopted, .adopted],
                    label
                )
                // One live candidate per process, however often it is reconciled.
                recovered.receiver.reconcile()
                recovered.receiver.reconcile()
                XCTAssertEqual(recovered.accepted.batches.count, 1, label)
                XCTAssertEqual(
                    recovered.accepted.batches[0].compactMap(\.native?.candidateRef),
                    slots.map { "nic-\(nonce)-\($0)" },
                    label
                )
                XCTAssertTrue(recovered.environment.queue.allEntries().isEmpty, "\(label): nothing is published")
            }
        }
    }

    func testShareFailureAtEveryBoundaryConvergesOnTheSameAdoption() async throws {
        let boundaries: [(step: NativeHandoffStep, committed: Bool)] = [
            (.reserved, false),
            (.snapshotsStaged, false),
            (.adopted, true),
            (.presented, true),
            (.acknowledged, true),
        ]
        for (step, committed) in boundaries {
            for recovery in Recovery.allCases {
                let label = "\(step) then \(recovery)"
                let root = try temporaryDirectory()
                let nonce = newID()
                let slots = [newID(), newID()]
                let transfer = try writeTransferBlobs(root, nonce: nonce)
                let encoded = try appGroup(root).encodedOffer(shareOffer(nonce: nonce, slotIDs: slots))

                let first = try launch(root: root)
                first.receiver.checkpoint = { if $0 == step { throw InjectedFailure() } }
                let error = await failure { try await first.receiver.submitAuthenticatedOffer(encoded) }
                XCTAssertEqual(error as? InjectedFailure, InjectedFailure(), label)

                let records = try XCTUnwrap(first.candidates.load(nativeRequestID: nonce), label)
                XCTAssertEqual(
                    records.map(\.state),
                    committed ? [.adopted, .adopted] : [.needsAccess, .needsAccess],
                    label
                )
                if committed {
                    // The copies are retained and their bytes are bound.
                    XCTAssertEqual(try ownedFiles(root, nonce: nonce), ["1-alpha.txt", "2-beta.txt"], label)
                    XCTAssertEqual(records.compactMap(\.ownedContentDigest), [sha256("alpha"), sha256("beta")], label)
                    XCTAssertEqual(records.compactMap(\.ownedContentByteLength), [5, 4], label)
                } else {
                    XCTAssertEqual(try ownedRequests(root), [], "\(label): nothing is left behind")
                    XCTAssertTrue(records.allSatisfy { $0.ownedContentDigest == nil }, label)
                }
                XCTAssertEqual(
                    first.mailbox.acknowledgement(nonce: nonce) != nil,
                    step == .acknowledged,
                    label
                )
                let shownBeforeRecovery = (step == .presented || step == .acknowledged) ? 1 : 0
                XCTAssertEqual(first.accepted.batches.count, shownBeforeRecovery, label)

                first.receiver.checkpoint = { _ in }
                let recovered: Companion
                switch recovery {
                case .reconcile:
                    recovered = first
                    recovered.receiver.reconcile()
                case .restartAndReplay:
                    recovered = try launch(root: root)
                    let replay = try await recovered.receiver.submitAuthenticatedOffer(encoded)
                    XCTAssertEqual(
                        try recovered.mailbox.decodeAcknowledgement(replay, expectedNonce: nonce)
                            .adoptions,
                        adoptions(nonce: nonce, slots: slots),
                        label
                    )
                }

                XCTAssertEqual(
                    recovered.mailbox.acknowledgement(nonce: nonce)?.adoptions,
                    adoptions(nonce: nonce, slots: slots),
                    label
                )
                XCTAssertEqual(try ownedRequests(root), [nonce], "\(label): one owned set")
                XCTAssertEqual(try ownedFiles(root, nonce: nonce), ["1-alpha.txt", "2-beta.txt"], label)
                XCTAssertEqual(
                    try recovered.candidates.load(nativeRequestID: nonce)?.compactMap(\.ownedContentDigest),
                    [sha256("alpha"), sha256("beta")],
                    label
                )
                recovered.receiver.reconcile()
                recovered.receiver.reconcile()
                XCTAssertEqual(recovered.accepted.batches.count, 1, label)
                XCTAssertEqual(
                    recovered.accepted.batches[0].map { try? Data(contentsOf: $0.url) },
                    [Data("alpha".utf8), Data("beta".utf8)],
                    label
                )
                XCTAssertTrue(recovered.environment.queue.allEntries().isEmpty, "\(label): nothing is published")
                if step != .acknowledged {
                    XCTAssertFalse(FileManager.default.fileExists(atPath: transfer.path), label)
                }
            }
        }
    }

    func testAcknowledgementWriteFailureAfterAdoptionRetainsEverythingAndWritesTheSameAcknowledgement() async throws {
        // Finder: a real write failure on the acknowledgement store.
        let finderRoot = try temporaryDirectory()
        let source = try writeOriginal(finderRoot, name: "brief.pdf")
        let finderNonce = newID()
        let finderSlot = newID()
        let finderEncoded = try appGroup(finderRoot).encodedOffer(
            try finderOffer(nonce: finderNonce, slotIDs: [finderSlot], urls: [source])
        )
        let finder = try launch(root: finderRoot)
        let finderRef = "candidate-access-\(finderNonce)-\(finderSlot)"

        try await withPermissions(0o500, on: acknowledgementsRoot(finderRoot)) {
            await failure { try await finder.receiver.submitAuthenticatedOffer(finderEncoded) }
        }
        // The record says adopted, so nothing it names was disconnected.
        XCTAssertEqual(try finder.candidates.load(nativeRequestID: finderNonce)?.map(\.state), [.adopted])
        XCTAssertEqual(finder.connectedAccessRefs, [finderRef])
        XCTAssertEqual(finder.accepted.batches.count, 1)
        XCTAssertNil(finder.mailbox.acknowledgement(nonce: finderNonce))

        finder.receiver.reconcile()
        XCTAssertEqual(
            finder.mailbox.acknowledgement(nonce: finderNonce)?.adoptions,
            adoptions(nonce: finderNonce, slots: [finderSlot])
        )
        XCTAssertEqual(finder.connectedAccessRefs, [finderRef])
        finder.receiver.reconcile()
        XCTAssertEqual(finder.accepted.batches.count, 1, "reconciling must not show it again")

        // Share: the owned copies are retained and still bound.
        let shareRoot = try temporaryDirectory()
        let shareNonce = newID()
        let shareSlots = [newID(), newID()]
        try writeTransferBlobs(shareRoot, nonce: shareNonce)
        let shareEncoded = try appGroup(shareRoot).encodedOffer(
            shareOffer(nonce: shareNonce, slotIDs: shareSlots)
        )
        let share = try launch(root: shareRoot)

        try await withPermissions(0o500, on: acknowledgementsRoot(shareRoot)) {
            await failure { try await share.receiver.submitAuthenticatedOffer(shareEncoded) }
        }
        XCTAssertEqual(try share.candidates.load(nativeRequestID: shareNonce)?.map(\.state), [.adopted, .adopted])
        XCTAssertEqual(try ownedFiles(shareRoot, nonce: shareNonce), ["1-alpha.txt", "2-beta.txt"])
        XCTAssertEqual(share.accepted.batches.count, 1)
        XCTAssertNil(share.mailbox.acknowledgement(nonce: shareNonce))

        share.receiver.reconcile()
        XCTAssertEqual(
            share.mailbox.acknowledgement(nonce: shareNonce)?.adoptions,
            adoptions(nonce: shareNonce, slots: shareSlots)
        )
        XCTAssertEqual(try ownedFiles(shareRoot, nonce: shareNonce), ["1-alpha.txt", "2-beta.txt"])
        share.receiver.reconcile()
        XCTAssertEqual(share.accepted.batches.count, 1)
    }

    func testAdoptedWriteThatFailsForRealIsRolledBackBecauseTheRecordStillReadsReserved() async throws {
        try XCTSkipIf(getuid() == 0, "permission bits do not bind root")
        let root = try temporaryDirectory()
        let nonce = newID()
        let slots = [newID(), newID()]
        try writeTransferBlobs(root, nonce: nonce)
        let encoded = try appGroup(root).encodedOffer(shareOffer(nonce: nonce, slotIDs: slots))
        let first = try launch(root: root)
        let records = candidatesRoot(root)

        // Fail the adopted write itself: the copies exist, the record is still
        // reserved and readable, so the rollback is correct.
        first.receiver.checkpoint = { step in
            if step == .snapshotsStaged {
                try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: records.path)
            }
        }
        await failure { try await first.receiver.submitAuthenticatedOffer(encoded) }
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: records.path)

        XCTAssertEqual(
            try first.candidates.load(nativeRequestID: nonce)?.map(\.state),
            [.needsAccess, .needsAccess]
        )
        XCTAssertEqual(try ownedRequests(root), [], "rolled back")
        XCTAssertTrue(first.accepted.batches.isEmpty)

        first.receiver.checkpoint = { _ in }
        first.receiver.reconcile()
        XCTAssertEqual(first.accepted.batches.count, 1)
        XCTAssertEqual(try ownedFiles(root, nonce: nonce), ["1-alpha.txt", "2-beta.txt"])
    }

    func testUnreadableRecordAfterAFailedAdoptedWriteLeavesTheResourcesAlone() async throws {
        try XCTSkipIf(getuid() == 0, "permission bits do not bind root")
        let root = try temporaryDirectory()
        let nonce = newID()
        let slots = [newID(), newID()]
        try writeTransferBlobs(root, nonce: nonce)
        let encoded = try appGroup(root).encodedOffer(shareOffer(nonce: nonce, slotIDs: slots))
        let first = try launch(root: root)
        let records = candidatesRoot(root)

        // The adopted write fails and the record cannot be read back either, so
        // whether it landed is unknown. Destroying the copies under a record
        // that may say adopted is worse than keeping them.
        first.receiver.checkpoint = { step in
            if step == .snapshotsStaged {
                try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: records.path)
            }
        }
        await failure { try await first.receiver.submitAuthenticatedOffer(encoded) }
        XCTAssertEqual(try ownedFiles(root, nonce: nonce), ["1-alpha.txt", "2-beta.txt"])
        XCTAssertTrue(first.accepted.batches.isEmpty)

        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: records.path)
        first.receiver.checkpoint = { _ in }
        first.receiver.reconcile()
        XCTAssertEqual(try first.candidates.load(nativeRequestID: nonce)?.map(\.state), [.adopted, .adopted])
        XCTAssertEqual(first.accepted.batches.count, 1)
        XCTAssertEqual(try ownedRequests(root), [nonce])
    }

    // MARK: Corrupt and unavailable state fails closed

    func testCorruptRecordFailsClosedAndIsNeverReadAsNoAdoption() async throws {
        let foreign = newID()
        let variants: [(name: String, bytes: (_ nonce: String, _ slot: String) throws -> Data)] = [
            ("garbage", { _, _ in Data("not json".utf8) }),
            ("empty file", { _, _ in Data() }),
            ("no records", { _, _ in Data("[]".utf8) }),
            ("record of another request", { _, slot in
                try Self.encode(Self.record(nonce: foreign, slot: slot))
            }),
            ("unknown candidate kind", { nonce, slot in
                let text = String(
                    decoding: try Self.encode(Self.record(nonce: nonce, slot: slot)),
                    as: UTF8.self
                )
                return Data(text.replacingOccurrences(of: "original_file", with: "folder_root").utf8)
            }),
            ("adopted snapshot with no bound digest", { nonce, slot in
                try Self.encode(Self.record(
                    nonce: nonce,
                    slot: slot,
                    kind: .ownedSnapshot,
                    state: .adopted
                ))
            }),
        ]
        for variant in variants {
            let root = try temporaryDirectory()
            let source = try writeOriginal(root, name: "brief.pdf")
            let nonce = newID()
            let slot = newID()
            let encoded = try appGroup(root).encodedOffer(
                try finderOffer(nonce: nonce, slotIDs: [slot], urls: [source])
            )
            let companion = try launch(root: root)
            let file = candidatesRoot(root).appendingPathComponent("\(nonce).json")
            let corrupt = try variant.bytes(nonce, slot)
            try corrupt.write(to: file)

            // An invalid record is not an absent one.
            XCTAssertThrowsError(try companion.candidates.load(nativeRequestID: nonce), variant.name) {
                XCTAssertEqual($0 as? NativeIntakeCandidateStoreError, .invalidRecord, variant.name)
            }
            let error = await failure { try await companion.receiver.submitAuthenticatedOffer(encoded) }
            XCTAssertEqual(error as? NativeIntakeCandidateStoreError, .invalidRecord, variant.name)

            XCTAssertTrue(companion.environment.accessStore.descriptors().isEmpty, variant.name)
            XCTAssertTrue(companion.accepted.batches.isEmpty, variant.name)
            XCTAssertNil(companion.mailbox.acknowledgement(nonce: nonce), variant.name)
            XCTAssertEqual(try Data(contentsOf: file), corrupt, "\(variant.name): left in place, not replaced")

            // The offer the failed submit stored is now pending. Reconciling
            // must report the request and still adopt nothing for it.
            let outcome = companion.receiver.reconcile()
            XCTAssertEqual(outcome.failedClosed, [nonce], variant.name)
            XCTAssertTrue(companion.environment.accessStore.descriptors().isEmpty, variant.name)
            XCTAssertTrue(companion.accepted.batches.isEmpty, variant.name)
            XCTAssertEqual(try Data(contentsOf: file), corrupt, variant.name)
        }
    }

    func testCorruptShareRecordCreatesNoOwnedSnapshot() async throws {
        let root = try temporaryDirectory()
        let nonce = newID()
        let slots = [newID(), newID()]
        try writeTransferBlobs(root, nonce: nonce)
        let encoded = try appGroup(root).encodedOffer(shareOffer(nonce: nonce, slotIDs: slots))
        let companion = try launch(root: root)
        try Data("{".utf8).write(to: candidatesRoot(root).appendingPathComponent("\(nonce).json"))

        await failure { try await companion.receiver.submitAuthenticatedOffer(encoded) }
        companion.receiver.reconcile()

        XCTAssertEqual(try ownedRequests(root), [])
        XCTAssertTrue(companion.accepted.batches.isEmpty)
    }

    func testUnreadableRecordFailsClosedInsteadOfLookingAbsent() async throws {
        try XCTSkipIf(getuid() == 0, "permission bits do not bind root")
        let root = try temporaryDirectory()
        let source = try writeOriginal(root, name: "brief.pdf")
        let nonce = newID()
        let slot = newID()
        let encoded = try appGroup(root).encodedOffer(
            try finderOffer(nonce: nonce, slotIDs: [slot], urls: [source])
        )
        let first = try launch(root: root)
        _ = try await first.receiver.submitAuthenticatedOffer(encoded)
        first.mailbox.removeAcknowledgement(nonce: nonce)

        let restarted = try launch(root: root)
        let file = candidatesRoot(root).appendingPathComponent("\(nonce).json")
        try await withPermissions(0o000, on: file) {
            XCTAssertThrowsError(try restarted.candidates.load(nativeRequestID: nonce)) {
                XCTAssertEqual($0 as? NativeIntakeCandidateStoreError, .unavailable)
            }
            let error = await failure { try await restarted.receiver.submitAuthenticatedOffer(encoded) }
            XCTAssertEqual(error as? NativeIntakeCandidateStoreError, .unavailable)
            XCTAssertEqual(restarted.receiver.reconcile().failedClosed, [nonce])
        }
        // A record that could not be read was not adopted a second time.
        XCTAssertEqual(restarted.environment.accessStore.descriptors().map(\.accessRef), ["candidate-access-\(nonce)-\(slot)"])
        XCTAssertEqual(restarted.environment.accessStore.descriptors().map(\.generation), [1])
        XCTAssertTrue(restarted.accepted.batches.isEmpty)
    }

    func testAnUnlistableRecordStoreAdoptsNothing() async throws {
        try XCTSkipIf(getuid() == 0, "permission bits do not bind root")
        let root = try temporaryDirectory()
        let source = try writeOriginal(root, name: "brief.pdf")
        let nonce = newID()
        let encoded = try appGroup(root).encodedOffer(
            try finderOffer(nonce: nonce, slotIDs: [newID()], urls: [source])
        )
        let companion = try launch(root: root)
        try companion.mailbox.storeAuthenticatedOffer(try companion.mailbox.decodeOffer(encoded))

        try await withPermissions(0o000, on: candidatesRoot(root)) {
            let outcome = companion.receiver.reconcile()
            XCTAssertTrue(outcome.storeUnavailable)
            XCTAssertTrue(companion.environment.accessStore.descriptors().isEmpty)
            XCTAssertTrue(companion.accepted.batches.isEmpty)
        }
    }

    // MARK: Exact idempotence

    func testExactReplayMapsToTheSameRefsAndAdoptsOnce() async throws {
        let root = try temporaryDirectory()
        let sources = [try writeOriginal(root, name: "one.pdf"), try writeOriginal(root, name: "two.pdf")]
        let nonce = newID()
        let slots = [newID(), newID()]
        let encoded = try appGroup(root).encodedOffer(
            try finderOffer(nonce: nonce, slotIDs: slots, urls: sources)
        )
        let companion = try launch(root: root)

        let first = try await companion.receiver.submitAuthenticatedOffer(encoded)
        let second = try await companion.receiver.submitAuthenticatedOffer(encoded)
        XCTAssertEqual(first, second, "a replay is answered with the same acknowledgement")
        let decoded = try companion.mailbox.decodeAcknowledgement(first, expectedNonce: nonce)
        XCTAssertEqual(decoded.adoptions, adoptions(nonce: nonce, slots: slots))

        // Even with the acknowledgement lost, the same request maps to the same
        // refs and revisions and is neither adopted nor shown a second time.
        companion.mailbox.removeAcknowledgement(nonce: nonce)
        let third = try companion.mailbox.decodeAcknowledgement(
            try await companion.receiver.submitAuthenticatedOffer(encoded),
            expectedNonce: nonce
        )
        XCTAssertEqual(third.adoptions, decoded.adoptions)
        companion.receiver.reconcile()
        XCTAssertEqual(companion.environment.accessStore.descriptors().count, 2, "one grant per slot")
        XCTAssertEqual(companion.accepted.batches.count, 1, "one live candidate set per process")
        XCTAssertEqual(try companion.candidates.load(nativeRequestID: nonce)?.map(\.candidateRevision), [1, 1])
    }

    func testRestartShowsAnAdoptedCandidateOnceAndOnlyOnce() async throws {
        let root = try temporaryDirectory()
        let nonce = newID()
        let slots = [newID(), newID()]
        try writeTransferBlobs(root, nonce: nonce)
        let encoded = try appGroup(root).encodedOffer(shareOffer(nonce: nonce, slotIDs: slots))
        let first = try launch(root: root)
        _ = try await first.receiver.submitAuthenticatedOffer(encoded)
        XCTAssertEqual(first.accepted.batches.count, 1)

        // The process dies with the candidates unreviewed. Nothing released them.
        let restarted = try launch(root: root)
        restarted.receiver.reconcile()
        restarted.receiver.reconcile()
        _ = try await restarted.receiver.submitAuthenticatedOffer(encoded)
        XCTAssertEqual(restarted.accepted.batches.count, 1, "reopened once in the new process")
        XCTAssertEqual(try ownedRequests(root), [nonce])
        XCTAssertEqual(restarted.environment.queue.allEntries().count, 0)
    }

    func testConflictingReplayFailsAndChangesNothing() async throws {
        let root = try temporaryDirectory()
        let sources = [try writeOriginal(root, name: "one.pdf"), try writeOriginal(root, name: "two.pdf")]
        let nonce = newID()
        let slots = [newID(), newID()]
        let encoded = try appGroup(root).encodedOffer(
            try finderOffer(nonce: nonce, slotIDs: slots, urls: sources)
        )
        let companion = try launch(root: root)
        let original = try await companion.receiver.submitAuthenticatedOffer(encoded)

        let conflicts: [(String, Data)] = [
            ("different slots", try appGroup(root).encodedOffer(
                try finderOffer(nonce: nonce, slotIDs: [newID(), newID()], urls: sources)
            )),
            ("different files under the same slots", try appGroup(root).encodedOffer(
                try finderOffer(
                    nonce: nonce,
                    slotIDs: slots,
                    urls: [try writeOriginal(root, name: "other.pdf"), sources[1]]
                )
            )),
            ("fewer slots", try appGroup(root).encodedOffer(
                try finderOffer(nonce: nonce, slotIDs: [slots[0]], urls: [sources[0]])
            )),
        ]
        for (name, conflicting) in conflicts {
            let error = await failure { try await companion.receiver.submitAuthenticatedOffer(conflicting) }
            XCTAssertEqual(error as? NativeHandoffMailboxError, .invalidOffer, name)
        }
        let replayed = try await companion.receiver.submitAuthenticatedOffer(encoded)
        XCTAssertEqual(replayed, original)
        XCTAssertEqual(companion.connectedAccessRefs.count, 2)
        XCTAssertEqual(companion.accepted.batches.count, 1)
    }

    func testConflictingReplayIsRefusedEvenAfterTheOriginalOfferAndAcknowledgementAreGone() async throws {
        let root = try temporaryDirectory()
        let sources = [try writeOriginal(root, name: "one.pdf"), try writeOriginal(root, name: "two.pdf")]
        let alternate = root.appendingPathComponent("alternate", isDirectory: true)
        try FileManager.default.createDirectory(at: alternate, withIntermediateDirectories: true)
        let differentOne = try writeOriginal(alternate, name: "one.pdf", text: "different")
        let nonce = newID()
        let slots = [newID(), newID()]
        let now = Date()
        let encoded = try appGroup(root).encodedOffer(
            try finderOffer(nonce: nonce, slotIDs: slots, urls: sources, now: now)
        )
        let companion = try launch(root: root)
        _ = try await companion.receiver.submitAuthenticatedOffer(encoded)

        // The stored offer is pruned and the acknowledgement lost, so only the
        // durable record can say what this nonce was reserved for.
        companion.mailbox.removeOffer(nonce: nonce, removeSnapshots: true)
        companion.mailbox.removeAcknowledgement(nonce: nonce)
        let rebound = try appGroup(root).encodedOffer(
            try finderOffer(
                nonce: nonce,
                slotIDs: slots,
                urls: [differentOne, sources[1]],
                now: now
            )
        )
        let error = await failure { try await companion.receiver.submitAuthenticatedOffer(rebound) }
        XCTAssertEqual(error as? NativeHandoffMailboxError, .invalidOffer)

        XCTAssertEqual(companion.connectedAccessRefs.count, 2)
        XCTAssertEqual(companion.accepted.batches.count, 1)
        XCTAssertNil(companion.mailbox.acknowledgement(nonce: nonce), "a conflicting offer is never acknowledged")
        // The conflicting offer is pending but is never allowed to drive the request.
        XCTAssertEqual(companion.receiver.reconcile().failedClosed, [nonce])
        XCTAssertNil(companion.mailbox.acknowledgement(nonce: nonce))
    }

    // MARK: Owned copies are bound to their bytes

    func testAPlantedFileAtTheOwnedNameIsReplacedNeverReused() async throws {
        let root = try temporaryDirectory()
        let nonce = newID()
        let slots = [newID(), newID()]
        try writeTransferBlobs(root, nonce: nonce)
        let encoded = try appGroup(root).encodedOffer(shareOffer(nonce: nonce, slotIDs: slots))
        // Same length as the real bytes, so a path-and-size check would accept them.
        let owned = ownedRoot(root).appendingPathComponent(nonce, isDirectory: true)
        try FileManager.default.createDirectory(at: owned, withIntermediateDirectories: true)
        try Data("forge".utf8).write(to: owned.appendingPathComponent("1-alpha.txt"))
        try Data("evil".utf8).write(to: owned.appendingPathComponent("2-beta.txt"))

        let companion = try launch(root: root)
        _ = try await companion.receiver.submitAuthenticatedOffer(encoded)

        XCTAssertEqual(
            companion.accepted.batches[0].map { try? Data(contentsOf: $0.url) },
            [Data("alpha".utf8), Data("beta".utf8)]
        )
        XCTAssertEqual(
            try companion.candidates.load(nativeRequestID: nonce)?.compactMap(\.ownedContentDigest),
            [sha256("alpha"), sha256("beta")]
        )
    }

    func testAnInterruptedCopyNeverBecomesTrustedBytes() async throws {
        let root = try temporaryDirectory()
        let nonce = newID()
        let slots = [newID(), newID()]
        try writeTransferBlobs(root, nonce: nonce)
        let encoded = try appGroup(root).encodedOffer(shareOffer(nonce: nonce, slotIDs: slots))
        // A crashed attempt left one finished copy, one short copy and a partial.
        let owned = ownedRoot(root).appendingPathComponent(nonce, isDirectory: true)
        try FileManager.default.createDirectory(at: owned, withIntermediateDirectories: true)
        try Data("alpha".utf8).write(to: owned.appendingPathComponent("1-alpha.txt"))
        try Data("be".utf8).write(to: owned.appendingPathComponent("2-beta.txt"))
        try Data("par".utf8).write(to: owned.appendingPathComponent(".\(newID()).partial"))

        let companion = try launch(root: root)
        _ = try await companion.receiver.submitAuthenticatedOffer(encoded)

        XCTAssertEqual(
            companion.accepted.batches[0].map { try? Data(contentsOf: $0.url) },
            [Data("alpha".utf8), Data("beta".utf8)]
        )
        XCTAssertEqual(
            try companion.candidates.load(nativeRequestID: nonce)?.compactMap(\.ownedContentByteLength),
            [5, 4]
        )
    }

    func testATamperedOwnedCopyIsReleasedNotShownAgain() async throws {
        let tamperings: [(name: String, apply: (URL) throws -> Void)] = [
            ("same length, different bytes", { try Data("ALPHA".utf8).write(to: $0) }),
            ("truncated", { try Data("al".utf8).write(to: $0) }),
            ("grown", { try Data("alpha-and-more".utf8).write(to: $0) }),
            ("emptied", { try Data().write(to: $0) }),
            ("deleted", { try FileManager.default.removeItem(at: $0) }),
            ("replaced by a link to identical bytes", { owned in
                let elsewhere = owned.deletingLastPathComponent()
                    .deletingLastPathComponent().appendingPathComponent("elsewhere.txt")
                try Data("alpha".utf8).write(to: elsewhere)
                try FileManager.default.removeItem(at: owned)
                try FileManager.default.createSymbolicLink(at: owned, withDestinationURL: elsewhere)
            }),
        ]
        for tampering in tamperings {
            let root = try temporaryDirectory()
            let nonce = newID()
            let slots = [newID(), newID()]
            try writeTransferBlobs(root, nonce: nonce)
            let encoded = try appGroup(root).encodedOffer(shareOffer(nonce: nonce, slotIDs: slots))
            let first = try launch(root: root)
            _ = try await first.receiver.submitAuthenticatedOffer(encoded)
            XCTAssertEqual(first.accepted.batches.count, 1, tampering.name)

            try tampering.apply(ownedURL(root, nonce: nonce, name: "1-alpha.txt"))

            // The next process finds a copy that no longer matches what was
            // bound. It fails closed: nothing is shown and nothing is kept.
            let restarted = try launch(root: root)
            restarted.receiver.reconcile()
            XCTAssertTrue(restarted.accepted.batches.isEmpty, tampering.name)
            XCTAssertEqual(try ownedRequests(root).filter { $0 == nonce }, [], tampering.name)
            XCTAssertEqual(
                try restarted.candidates.load(nativeRequestID: nonce)?.map(\.state),
                [.released, .released],
                "\(tampering.name): released tombstone blocks replay"
            )
            XCTAssertTrue(restarted.environment.queue.allEntries().isEmpty, tampering.name)
        }
    }

    func testReservationWithNoRemainingOfferFailsClosedAndLeavesNothingBehind() async throws {
        // Share: an interrupted attempt left a planted copy and the transfer
        // window has since closed.
        let shareRoot = try temporaryDirectory()
        let shareNonce = newID()
        let shareSlots = [newID(), newID()]
        let past = Date().addingTimeInterval(-200)
        let expired = shareOffer(nonce: shareNonce, slotIDs: shareSlots, now: past)
        let shareCompanion = try launch(root: shareRoot)
        try shareCompanion.mailbox.storeAuthenticatedOffer(expired, now: past)
        var reservation: [NativeIntakeCandidateV1] = []
        for (index, slot) in shareSlots.enumerated() {
            reservation.append(Self.record(
                nonce: shareNonce,
                slot: slot,
                kind: .ownedSnapshot,
                state: .needsAccess,
                content: try OwnedSnapshotStore.stagedFilename(["alpha.txt", "beta.txt"][index], index: index)
            ))
        }
        try shareCompanion.candidates.write(reservation)
        let planted = ownedRoot(shareRoot).appendingPathComponent(shareNonce, isDirectory: true)
        try FileManager.default.createDirectory(at: planted, withIntermediateDirectories: true)
        try Data("forge".utf8).write(to: planted.appendingPathComponent("1-alpha.txt"))

        shareCompanion.receiver.reconcile()

        XCTAssertEqual(try ownedRequests(shareRoot), [], "no transfer to copy from: the planted file is removed")
        XCTAssertEqual(
            try shareCompanion.candidates.load(nativeRequestID: shareNonce)?.map(\.state),
            [.released, .released]
        )
        XCTAssertTrue(shareCompanion.accepted.batches.isEmpty)
        XCTAssertNil(shareCompanion.mailbox.acknowledgement(nonce: shareNonce))

        // Finder: a grant a crashed attempt created is released with it.
        let finderRoot = try temporaryDirectory()
        let source = try writeOriginal(finderRoot, name: "brief.pdf")
        let finderNonce = newID()
        let finderSlot = newID()
        let finderCompanion = try launch(root: finderRoot)
        let staleOffer = try finderOffer(nonce: finderNonce, slotIDs: [finderSlot], urls: [source], now: past)
        try finderCompanion.mailbox.storeAuthenticatedOffer(staleOffer, now: past)
        try finderCompanion.candidates.write([
            Self.record(nonce: finderNonce, slot: finderSlot),
        ])
        _ = try finderCompanion.submitter.adoptOriginal(
            source,
            displayName: "brief.pdf",
            surface: .finderQuickAction,
            accessRef: "candidate-access-\(finderNonce)-\(finderSlot)",
            linkedDocumentID: "candidate-doc-\(finderNonce)-\(finderSlot)"
        )
        XCTAssertEqual(finderCompanion.connectedAccessRefs.count, 1)

        finderCompanion.receiver.reconcile()

        XCTAssertEqual(finderCompanion.connectedAccessRefs, [])
        XCTAssertEqual(
            try finderCompanion.candidates.load(nativeRequestID: finderNonce)?.map(\.state),
            [.released]
        )
        XCTAssertTrue(finderCompanion.accepted.batches.isEmpty)
    }

    func testOwnedCopyQuotasStillBind() async throws {
        // One item over the per-item limit.
        let itemRoot = try temporaryDirectory()
        let itemNonce = newID()
        let itemSlots = [newID()]
        let itemTransfer = try appGroup(itemRoot).prepareSnapshotDirectory(nonce: itemNonce)
        try sparseFile(at: itemTransfer.appendingPathComponent("blob-1"), bytes: OwnedSnapshotStore.maximumItemBytes + 1)
        let item = try launch(root: itemRoot)
        let itemError = await failure {
            try await item.receiver.submitAuthenticatedOffer(
                try appGroup(itemRoot).encodedOffer(shareOffer(nonce: itemNonce, slotIDs: itemSlots))
            )
        }
        XCTAssertEqual(itemError as? OwnedSnapshotStoreError, .itemTooLarge)
        XCTAssertEqual(try ownedRequests(itemRoot), [])
        XCTAssertTrue(item.accepted.batches.isEmpty)

        // Items that each fit but together exceed the selection limit.
        let selectionRoot = try temporaryDirectory()
        let selectionNonce = newID()
        let selectionSlots = (0..<5).map { _ in newID() }
        let selectionTransfer = try appGroup(selectionRoot).prepareSnapshotDirectory(nonce: selectionNonce)
        for index in 0..<4 {
            try sparseFile(
                at: selectionTransfer.appendingPathComponent("blob-\(index + 1)"),
                bytes: OwnedSnapshotStore.maximumItemBytes
            )
        }
        try Data("x".utf8).write(to: selectionTransfer.appendingPathComponent("blob-5"))
        let selection = try launch(root: selectionRoot)
        let selectionError = await failure {
            try await selection.receiver.submitAuthenticatedOffer(
                try appGroup(selectionRoot).encodedOffer(
                    shareOffer(
                        nonce: selectionNonce,
                        slotIDs: selectionSlots,
                        names: ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]
                    )
                )
            )
        }
        XCTAssertEqual(selectionError as? OwnedSnapshotStoreError, .selectionTooLarge)
        XCTAssertEqual(try ownedRequests(selectionRoot), [])
        XCTAssertTrue(selection.accepted.batches.isEmpty)
    }

    // MARK: Release

    func testAReleasedRequestIsNeverAdoptedAgainByAReplay() async throws {
        let root = try temporaryDirectory()
        let source = try writeOriginal(root, name: "brief.pdf")
        let nonce = newID()
        let slot = newID()
        let encoded = try appGroup(root).encodedOffer(
            try finderOffer(nonce: nonce, slotIDs: [slot], urls: [source])
        )
        let companion = try launch(root: root, withPanel: true)

        // The acknowledgement cannot be written, so no durable ack answers a
        // replay and only the record can stand between it and a second adoption.
        try await withPermissions(0o500, on: acknowledgementsRoot(root)) {
            await failure { try await companion.receiver.submitAuthenticatedOffer(encoded) }
        }
        XCTAssertEqual(companion.panel?.liveNativeCandidateRefs, ["nic-\(nonce)-\(slot)"])

        companion.panel?.discardUnsubmittedCandidates()
        XCTAssertEqual(companion.connectedAccessRefs, [])
        XCTAssertEqual(try companion.candidates.load(nativeRequestID: nonce)?.map(\.state), [.released])

        let error = await failure { try await companion.receiver.submitAuthenticatedOffer(encoded) }
        XCTAssertEqual(error as? NativeHandoffAdoptionError, .released)
        companion.receiver.reconcile()
        XCTAssertEqual(companion.connectedAccessRefs, [], "a released candidate is not adopted again")
        XCTAssertEqual(companion.accepted.batches.count, 1)
        XCTAssertNil(companion.mailbox.acknowledgement(nonce: nonce), "a released request is not acknowledged")
        XCTAssertEqual(
            try companion.candidates.load(nativeRequestID: nonce)?.map(\.state),
            [.released],
            "the tombstone stands while the offer could still be replayed"
        )

        // Deleting the grant-bearing offer does not erase the replay binding.
        companion.mailbox.removeOffer(nonce: nonce, removeSnapshots: true)
        companion.receiver.reconcile()
        XCTAssertEqual(
            try companion.candidates.load(nativeRequestID: nonce)?.map(\.state),
            [.released]
        )
    }

    func testQuittingReleasesWhatNeverReachedReviewAndRefusesNewRequests() async throws {
        let root = try temporaryDirectory()
        let source = try writeOriginal(root, name: "brief.pdf")
        let nonce = newID()
        let slot = newID()
        let encoded = try appGroup(root).encodedOffer(
            try finderOffer(nonce: nonce, slotIDs: [slot], urls: [source])
        )
        let companion = try launch(root: root)
        // Adopted and durable, but the process stops before it is shown.
        companion.receiver.checkpoint = { if $0 == .adopted { throw InjectedFailure() } }
        await failure { try await companion.receiver.submitAuthenticatedOffer(encoded) }
        XCTAssertEqual(companion.connectedAccessRefs.count, 1)
        XCTAssertTrue(companion.accepted.batches.isEmpty)

        companion.receiver.companionWillTerminate()

        XCTAssertEqual(companion.connectedAccessRefs, [])
        XCTAssertEqual(try companion.candidates.load(nativeRequestID: nonce)?.map(\.state), [.released])
        companion.receiver.checkpoint = { _ in }
        let error = await failure { try await companion.receiver.submitAuthenticatedOffer(encoded) }
        XCTAssertEqual(error as? NativeHandoffAdoptionError, .terminating)
        companion.receiver.reconcile()
        XCTAssertEqual(companion.connectedAccessRefs, [])
        XCTAssertTrue(companion.accepted.batches.isEmpty)
    }

    // MARK: Helpers

    private func adoptions(
        nonce: String,
        slots: [String]
    ) -> [NativeHandoffAcknowledgementV1.Adoption] {
        slots.map {
            NativeHandoffAcknowledgementV1.Adoption(
                slotID: $0,
                candidateRef: "nic-\(nonce)-\($0)",
                candidateRevision: 1
            )
        }
    }

    private func sparseFile(at url: URL, bytes: UInt64) throws {
        FileManager.default.createFile(atPath: url.path, contents: nil)
        let handle = try FileHandle(forWritingTo: url)
        defer { try? handle.close() }
        try handle.truncate(atOffset: bytes)
    }

    nonisolated private static func record(
        nonce: String,
        slot: String,
        kind: NativeIntakeCandidateKind = .originalFile,
        state: NativeIntakeCandidateState = .needsAccess,
        content: String? = nil
    ) -> NativeIntakeCandidateV1 {
        NativeIntakeCandidateV1(
            nativeRequestID: nonce,
            offerDigest: "sha256:" + String(repeating: "01", count: 32),
            slotID: slot,
            candidateRef: "nic-\(nonce)-\(slot)",
            kind: kind,
            nativeAccessLeaseRef: kind == .originalFile ? "candidate-access-\(nonce)-\(slot)" : nil,
            ownedContentRef: kind == .ownedSnapshot ? (content ?? "1-alpha.txt") : nil,
            displayName: "brief.pdf",
            state: state
        )
    }

    nonisolated private static func encode(_ record: NativeIntakeCandidateV1) throws -> Data {
        try JSONEncoder().encode([record])
    }
}
