@testable import DocumentIntakeMacOS
import Foundation
import XCTest

/// WP-10 repair: folder establishment is durable and recoverable before any root
/// or member authority can be orphaned.
///
/// Each test interrupts establishment at one point, proves what is on disk is a
/// truthful non-usable pending intent, then rebuilds `DocumentIntakeEnvironment`
/// from that disk and proves recovery lands exactly one run, subscription, root
/// and member set under the original identities — with no duplicate owner,
/// orphan grant, discovery authority or queued child.
final class FolderEstablishmentRecoveryTests: XCTestCase {
    private struct BookmarkCodec: SecurityScopedBookmarkCoding {
        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
        }
        func startAccessing(_ url: URL) -> Bool { true }
        func stopAccessing(_ url: URL) {}
    }

    private struct InjectedFault: Error {}

    private static let importOnce = FolderCapturePolicy(
        mode: .importOnce,
        recursive: true,
        includeHidden: false
    )
    private static let keepUpdated = FolderCapturePolicy(
        mode: .keepUpdated,
        recursive: true,
        includeHidden: false
    )
    private static let watchNewOnly = FolderCapturePolicy(
        mode: .keepUpdated,
        recursive: true,
        includeHidden: false,
        watchInitialContent: .onlyNewAndChanged
    )

    // MARK: - After the intent and root atomic write

    func testACrashRightAfterTheIntentAndRootWriteOrphansNothingAndRecoversInFull() async throws {
        let lane = try lane(files: 3)
        let submission = FolderCaptureSubmission(
            environment: lane.environment,
            workspaceRef: "workspace-1"
        )
        submission.afterIntentRecorded = { throw InjectedFault() }

        let plan = try submission.plan(root: lane.root, policy: Self.importOnce)
        await expectFault { try await submission.submit(plan) }

        // The very first durable authority write already carries the whole
        // reviewed plan, so the grant that exists names what owns it.
        let intent = try XCTUnwrap(lane.environment.accessStore.pendingFolderEstablishments().first)
        XCTAssertEqual(lane.environment.accessStore.pendingFolderEstablishments().count, 1)
        XCTAssertEqual(intent.members.count, 3)
        XCTAssertEqual(intent.subscriptionID, nil)
        XCTAssertEqual(intent.authority, .importPlan)
        try assertPendingAndUnusable(lane.environment, intent: intent, memberGrants: 0)

        let reopened = try lane.reopen()
        let recovered = await reopened.reconcilePendingFolderEstablishment()

        XCTAssertEqual(recovered, [intent.submissionID])
        try assertEstablished(reopened, matches: intent)
        let run = try XCTUnwrap(reopened.folderBulkRuns.all().first)
        XCTAssertEqual(run.state, .adding)
        XCTAssertEqual(run.publishedCount, 3)
        XCTAssertEqual(Set(reopened.queue.allEntries().map(\.intent.itemId)),
                       Set(intent.members.map(\.itemID)))
    }

    // MARK: - During member establishment

    func testACrashPartwayThroughMembersLeavesNoConnectedMemberWithoutAnOwnerRecord() async throws {
        let lane = try lane(files: 4)
        let submission = FolderCaptureSubmission(
            environment: lane.environment,
            workspaceRef: "workspace-1"
        )
        submission.afterMemberEstablished = { index in
            if index == 1 { throw InjectedFault() }
        }

        let plan = try submission.plan(root: lane.root, policy: Self.importOnce)
        await expectFault { try await submission.submit(plan) }

        // Two member grants exist. Neither is an orphan: the pending intent is
        // their owner record, and it names the two that are still to come.
        let intent = try XCTUnwrap(lane.environment.accessStore.pendingFolderEstablishments().first)
        XCTAssertEqual(intent.members.count, 4)
        try assertPendingAndUnusable(lane.environment, intent: intent, memberGrants: 2)
        let group = try XCTUnwrap(lane.environment.accessStore.rootDescriptors().first)
        XCTAssertFalse(group.membersFrozen)
        XCTAssertEqual(group.memberRefs, Array(intent.members.prefix(2).map(\.memberID)).sorted())

        let reopened = try lane.reopen()
        _ = await reopened.reconcilePendingFolderEstablishment()

        // The two that were already established keep their identities rather
        // than being enrolled a second time under new ones.
        try assertEstablished(reopened, matches: intent)
        let run = try XCTUnwrap(reopened.folderBulkRuns.all().first)
        XCTAssertEqual(run.members.map(\.memberID), intent.members.map(\.memberID))
        XCTAssertEqual(run.members.map(\.accessRef), intent.members.map(\.accessRef))
        XCTAssertTrue(run.members.allSatisfy { $0.bindingGeneration == 1 })
    }

    // MARK: - After subscription registration

    func testACrashAfterSubscriptionRegistrationActivatesTheSameWatchAndNoSecondOne() async throws {
        let lane = try lane(files: 2)
        let submission = FolderCaptureSubmission(
            environment: lane.environment,
            workspaceRef: "workspace-1"
        )
        submission.afterSubscriptionRegistered = { throw InjectedFault() }

        let plan = try submission.plan(root: lane.root, policy: Self.watchNewOnly)
        await expectFault { try await submission.submit(plan) }

        let intent = try XCTUnwrap(lane.environment.accessStore.pendingFolderEstablishments().first)
        let subscription = try XCTUnwrap(lane.environment.folderSubscriptions.all().first)
        XCTAssertEqual(subscription.subscriptionID, intent.subscriptionID)
        XCTAssertEqual(subscription.state, .active)
        // The subscription record exists but its root is not established, so the
        // watch has no discovery authority yet…
        try assertPendingAndUnusable(lane.environment, intent: intent, memberGrants: 2)

        // …and monitoring says so truthfully instead of scanning it or
        // reporting the grant as lost.
        let reports = await lane.environment.folderMonitoring.reconcileAll()
        XCTAssertEqual(reports.map(\.state), [.active])
        XCTAssertEqual(reports.map(\.examined), [0])
        XCTAssertEqual(lane.environment.folderSubscriptions.all().first?.state, .active)
        let observed = await lane.environment.folderMonitoring.activeObservationRoots()
        XCTAssertTrue(observed.isEmpty)
        XCTAssertTrue(lane.environment.queue.allEntries().isEmpty)

        let reopened = try lane.reopen()
        _ = await reopened.reconcilePendingFolderEstablishment()

        try assertEstablished(reopened, matches: intent)
        XCTAssertEqual(reopened.folderSubscriptions.all().count, 1)
        let recovered = try XCTUnwrap(reopened.folderSubscriptions.all().first)
        XCTAssertEqual(recovered.subscriptionID, subscription.subscriptionID)
        XCTAssertEqual(recovered.members, subscription.members)
        XCTAssertEqual(recovered.observationEpoch, subscription.observationEpoch)
        XCTAssertEqual(recovered.consentRecordedAt, subscription.consentRecordedAt)
        XCTAssertTrue(reopened.folderBulkRuns.all().isEmpty)
        let observedAfter = await reopened.folderMonitoring.activeObservationRoots()
        XCTAssertFalse(observedAfter.isEmpty)
    }

    // MARK: - After bulk-run registration

    func testACrashAfterRunRegistrationQueuesEveryChildExactlyOnce() async throws {
        let lane = try lane(files: 3)
        let submission = FolderCaptureSubmission(
            environment: lane.environment,
            workspaceRef: "workspace-1"
        )
        submission.afterRunRegistered = { throw InjectedFault() }

        let plan = try submission.plan(root: lane.root, policy: Self.importOnce)
        await expectFault { try await submission.submit(plan) }

        let intent = try XCTUnwrap(lane.environment.accessStore.pendingFolderEstablishments().first)
        let run = try XCTUnwrap(lane.environment.folderBulkRuns.all().first)
        XCTAssertEqual(run.runID, intent.runID)
        XCTAssertEqual(run.plannedCount, 3)
        // Nothing was queued, and the root is still not discoverable.
        try assertPendingAndUnusable(lane.environment, intent: intent, memberGrants: 3)

        let reopened = try lane.reopen()
        _ = await reopened.reconcilePendingFolderEstablishment()

        try assertEstablished(reopened, matches: intent)
        XCTAssertEqual(reopened.folderBulkRuns.all().count, 1)
        let recovered = try XCTUnwrap(reopened.folderBulkRuns.run(id: run.runID))
        XCTAssertEqual(recovered.members.map(\.itemID), run.members.map(\.itemID))
        XCTAssertEqual(recovered.publishedCount, 3)
        XCTAssertEqual(reopened.queue.allEntries().count, 3)
        XCTAssertEqual(Set(reopened.queue.allEntries().map(\.intent.itemId)).count, 3)
    }

    // MARK: - Before activation

    func testACrashBeforeActivationHoldsBothOwnersWithoutGrantingDiscovery() async throws {
        let lane = try lane(files: 2)
        let submission = FolderCaptureSubmission(
            environment: lane.environment,
            workspaceRef: "workspace-1"
        )
        submission.beforeActivation = { throw InjectedFault() }

        let plan = try submission.plan(root: lane.root, policy: Self.keepUpdated)
        await expectFault { try await submission.submit(plan) }

        let intent = try XCTUnwrap(lane.environment.accessStore.pendingFolderEstablishments().first)
        XCTAssertEqual(intent.ownerRefs.count, 2)
        XCTAssertNotNil(intent.runID)
        XCTAssertNotNil(intent.subscriptionID)
        XCTAssertEqual(lane.environment.folderSubscriptions.all().count, 1)
        XCTAssertEqual(lane.environment.folderBulkRuns.all().count, 1)
        try assertPendingAndUnusable(lane.environment, intent: intent, memberGrants: 2)

        let reopened = try lane.reopen()
        _ = await reopened.reconcilePendingFolderEstablishment()

        try assertEstablished(reopened, matches: intent)
        XCTAssertEqual(reopened.folderSubscriptions.all().count, 1)
        XCTAssertEqual(reopened.folderBulkRuns.all().count, 1)
        XCTAssertEqual(reopened.queue.allEntries().count, 2)
        XCTAssertNoThrow(
            try reopened.accessStore.withRootReadAccess(
                rootRef: intent.rootRef,
                expectedGeneration: 1,
                purpose: .memberDiscovery
            ) { _ in () }
        )
    }

    // MARK: - After activation, before completion

    func testACrashAfterActivationRetiresTheIntentWithoutRepeatingTheWork() async throws {
        let lane = try lane(files: 3)
        let submission = FolderCaptureSubmission(
            environment: lane.environment,
            workspaceRef: "workspace-1"
        )
        submission.afterActivation = { throw InjectedFault() }

        let plan = try submission.plan(root: lane.root, policy: Self.importOnce)
        await expectFault { try await submission.submit(plan) }

        // The work is established; only the acknowledgement is missing, which is
        // what the retained intent says.
        let intent = try XCTUnwrap(lane.environment.accessStore.pendingFolderEstablishments().first)
        let group = try XCTUnwrap(lane.environment.accessStore.rootDescriptors().first)
        XCTAssertEqual(group.state, .active)
        XCTAssertEqual(lane.environment.folderBulkRuns.all().count, 1)
        XCTAssertTrue(lane.environment.queue.allEntries().isEmpty)

        let reopened = try lane.reopen()
        _ = await reopened.reconcilePendingFolderEstablishment()

        try assertEstablished(reopened, matches: intent)
        XCTAssertEqual(reopened.folderBulkRuns.all().count, 1)
        XCTAssertEqual(reopened.queue.allEntries().count, 3)

        // A second reconcile has nothing left to do and publishes nothing more.
        let again = await reopened.reconcilePendingFolderEstablishment()
        XCTAssertTrue(again.isEmpty)
        XCTAssertEqual(reopened.queue.allEntries().count, 3)
        XCTAssertEqual(reopened.folderBulkRuns.all().count, 1)
    }

    // MARK: - Replay

    func testExactReplayIsIdempotentAndAConflictingReplayIsRefused() async throws {
        let lane = try lane(files: 2)
        let submission = FolderCaptureSubmission(
            environment: lane.environment,
            workspaceRef: "workspace-1"
        )
        submission.afterMemberEstablished = { _ in throw InjectedFault() }
        let plan = try submission.plan(root: lane.root, policy: Self.importOnce)
        await expectFault { try await submission.submit(plan) }
        let intent = try XCTUnwrap(lane.environment.accessStore.pendingFolderEstablishments().first)

        // A different plan under the same submission identity is refused before
        // it can touch the grant it does not describe.
        let conflicting = PendingFolderSubmission(
            submissionID: intent.submissionID,
            rootRef: intent.rootRef,
            rootDisplayName: intent.rootDisplayName,
            runID: intent.runID,
            manifestRevision: intent.manifestRevision,
            subscriptionID: intent.subscriptionID,
            observationEpoch: intent.observationEpoch,
            binding: intent.binding,
            authority: intent.authority,
            mode: intent.mode,
            recursive: intent.recursive,
            includeHidden: intent.includeHidden,
            watchInitialContent: intent.watchInitialContent,
            approvedFormatPolicyRef: intent.approvedFormatPolicyRef,
            exclusionsPolicyRef: intent.exclusionsPolicyRef,
            configGeneration: intent.configGeneration,
            consentGeneration: intent.consentGeneration,
            createdAt: intent.createdAt,
            members: Array(intent.members.dropLast())
        )
        XCTAssertThrowsError(
            try lane.environment.accessStore.beginFolderEstablishment(
                conflicting,
                selectedURL: lane.root
            )
        ) { XCTAssertEqual($0 as? NativeAccessError, .conflictingEstablishment) }

        // Exact replay finishes it, and replaying again changes nothing.
        submission.afterMemberEstablished = nil
        let first = try await submission.establish(intent)
        let second = try await submission.establish(intent)

        XCTAssertEqual(first.count, 2)
        XCTAssertTrue(second.isEmpty)
        try assertEstablished(lane.environment, matches: intent)
        XCTAssertEqual(lane.environment.folderBulkRuns.all().count, 1)
        XCTAssertEqual(lane.environment.queue.allEntries().count, 2)
    }

    func testANewSubmissionFinishesAPendingEstablishmentBeforeCountingWatches() async throws {
        let lane = try lane(files: 1)
        let submission = FolderCaptureSubmission(
            environment: lane.environment,
            workspaceRef: "workspace-1"
        )
        submission.afterSubscriptionRegistered = { throw InjectedFault() }
        let plan = try submission.plan(root: lane.root, policy: Self.keepUpdated)
        await expectFault { try await submission.submit(plan) }
        let intent = try XCTUnwrap(lane.environment.accessStore.pendingFolderEstablishments().first)

        // Submitting the same folder again recovers the pending establishment
        // first, so the duplicate it would otherwise create is refused instead.
        let fresh = FolderCaptureSubmission(
            environment: lane.environment,
            workspaceRef: "workspace-1"
        )
        let second = try fresh.plan(root: lane.root, policy: Self.keepUpdated)
        do {
            _ = try await fresh.submit(second)
            XCTFail("expected duplicateWatch")
        } catch {
            XCTAssertEqual(error as? FolderCaptureSubmissionError, .duplicateWatch)
        }

        try assertEstablished(lane.environment, matches: intent)
        XCTAssertEqual(lane.environment.folderSubscriptions.all().count, 1)
        XCTAssertEqual(lane.environment.accessStore.rootDescriptors().count, 1)
    }

    // MARK: - A local stop while activation is still pending

    /// END-01: stopping is a one-way door. The watch is stopped while Core has
    /// not yet confirmed its baseline, so the confirmation lands late — and a
    /// late confirmation is not consent to resume watching.
    func testALateBaselineAdmissionDoesNotReactivateALocallyStoppedWatch() async throws {
        let lane = try lane(files: 1)
        let submission = FolderCaptureSubmission(
            environment: lane.environment,
            workspaceRef: "workspace-1"
        )
        let plan = try submission.plan(root: lane.root, policy: Self.keepUpdated)
        let baseline = try await submission.submit(plan)
        let subscription = try XCTUnwrap(lane.environment.folderSubscriptions.all().first)
        XCTAssertEqual(subscription.state, .waitingForCore)

        _ = try await lane.environment.folderMonitoring.stop(
            subscriptionID: subscription.subscriptionID
        )
        try admit(baseline[0], captureID: "dcap-" + String(repeating: "a", count: 64), queue: lane.environment.queue)

        await lane.environment.folderMonitoring.reconcileReceipts()
        let reports = await lane.environment.folderMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.state), [.stopped])
        try await assertStoppedForGood(lane.environment, subscriptionID: subscription.subscriptionID)
        // The late receipt is not recorded against the stopped watch either: a
        // watch that was stopped has no current capture to carry forward.
        let stopped = try XCTUnwrap(
            lane.environment.folderSubscriptions.subscription(id: subscription.subscriptionID)
        )
        XCTAssertEqual(stopped.members.compactMap(\.currentSuccessfulCaptureID), [])
        XCTAssertEqual(lane.environment.queue.allEntries().count, baseline.count)

        // And the stop survives a restart: reconciling from disk leaves it stopped.
        let reopened = try lane.reopen()
        _ = await reopened.reconcilePendingFolderEstablishment()
        await reopened.folderMonitoring.reconcileReceipts()
        try await assertStoppedForGood(reopened, subscriptionID: subscription.subscriptionID)
    }

    /// END-01: an add with Keep updated that Core has not yet confirmed keeps its identities across
    /// a restart, observes nothing until the confirmation, and is then activated as the same watch.
    func testAWaitingFolderWatchKeepsItsIdentitiesAcrossARestartAndObservesNothingUntilActivated() async throws {
        let lane = try lane(files: 2)
        let submission = FolderCaptureSubmission(
            environment: lane.environment,
            workspaceRef: "workspace-1"
        )
        let plan = try submission.plan(root: lane.root, policy: Self.keepUpdated)
        let baseline = try await submission.submit(plan)
        let submitted = try XCTUnwrap(lane.environment.folderSubscriptions.all().first)
        XCTAssertEqual(submitted.state, .waitingForCore)

        let reopened = try lane.reopen()
        let recovered = await reopened.reconcilePendingFolderEstablishment()
        XCTAssertTrue(recovered.isEmpty)
        XCTAssertEqual(reopened.folderSubscriptions.all(), [submitted])
        XCTAssertEqual(
            reopened.queue.allEntries().map(\.intent.itemId).sorted(),
            baseline.map(\.intent.itemId).sorted()
        )
        try Data("new before activation".utf8).write(to: lane.root.appendingPathComponent("new.md"))

        let waiting = await reopened.folderMonitoring.reconcileAll()
        XCTAssertEqual(waiting.map(\.state), [.waitingForCore])
        let beforeActivation = await reopened.folderMonitoring.activeObservationRoots()
        XCTAssertTrue(beforeActivation.isEmpty)
        XCTAssertEqual(reopened.queue.allEntries().count, baseline.count)

        for (index, entry) in baseline.enumerated() {
            try admit(
                entry,
                captureID: "dcap-" + String(index) + String(repeating: "a", count: 63),
                queue: reopened.queue
            )
        }
        _ = await reopened.folderMonitoring.reconcileAll()

        let activated = try XCTUnwrap(reopened.folderSubscriptions.all().first)
        XCTAssertEqual(reopened.folderSubscriptions.all().count, 1)
        XCTAssertEqual(activated.subscriptionID, submitted.subscriptionID)
        XCTAssertEqual(activated.rootRef, submitted.rootRef)
        // Each member is the one the add created, now holding the capture Core confirmed for it.
        // The file that appeared while the watch was waiting is found only after activation.
        let retained = Array(activated.members.prefix(submitted.members.count))
        XCTAssertEqual(retained.map(\.memberID), submitted.members.map(\.memberID))
        XCTAssertEqual(retained.map(\.accessRef), submitted.members.map(\.accessRef))
        XCTAssertEqual(retained.map(\.baselineItemID), submitted.members.map(\.baselineItemID))
        XCTAssertEqual(activated.members.count, submitted.members.count + 1)
        XCTAssertEqual(
            Set(submitted.members.compactMap(\.baselineItemID)),
            Set(baseline.map(\.intent.itemId))
        )
        XCTAssertEqual(activated.members.compactMap(\.currentSuccessfulCaptureID).count, baseline.count)
        XCTAssertEqual(activated.state, .active)
        let afterActivation = await reopened.folderMonitoring.activeObservationRoots()
        XCTAssertEqual(afterActivation.map(\.subscriptionID), [submitted.subscriptionID])
    }

    /// The other canonical outcome. A baseline Core refuses would normally send
    /// the watch to review, which is a resumable state — so for a watch already
    /// stopped it would be the same reactivation by another name.
    func testALateBaselineFailureDoesNotReopenALocallyStoppedWatch() async throws {
        let lane = try lane(files: 1)
        let submission = FolderCaptureSubmission(
            environment: lane.environment,
            workspaceRef: "workspace-1"
        )
        let plan = try submission.plan(root: lane.root, policy: Self.keepUpdated)
        let baseline = try await submission.submit(plan)
        let subscription = try XCTUnwrap(lane.environment.folderSubscriptions.all().first)
        XCTAssertEqual(subscription.state, .waitingForCore)

        _ = try await lane.environment.folderMonitoring.stop(
            subscriptionID: subscription.subscriptionID
        )
        _ = try lane.environment.queue.markFailed(
            itemID: baseline[0].intent.itemId,
            failureCode: "delivery-refused"
        )

        await lane.environment.folderMonitoring.reconcileReceipts()
        let reports = await lane.environment.folderMonitoring.reconcileAll()

        XCTAssertEqual(reports.map(\.state), [.stopped])
        try await assertStoppedForGood(lane.environment, subscriptionID: subscription.subscriptionID)
    }

    /// A stopped watch stays stopped, observes nothing, and cannot be resumed
    /// back into the watch it was: re-enrolling the folder is the only way back.
    private func assertStoppedForGood(
        _ environment: DocumentIntakeEnvironment,
        subscriptionID: String,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async throws {
        let subscription = try XCTUnwrap(
            environment.folderSubscriptions.subscription(id: subscriptionID),
            file: file,
            line: line
        )
        XCTAssertEqual(subscription.state, .stopped, file: file, line: line)
        XCTAssertThrowsError(
            try environment.accessStore.withRootReadAccess(
                rootRef: subscription.rootRef,
                expectedGeneration: subscription.rootBindingGeneration,
                purpose: .memberDiscovery
            ) { _ in () },
            file: file,
            line: line
        ) {
            XCTAssertEqual($0 as? NativeAccessError, .discoveryNotPermitted, file: file, line: line)
        }
        do {
            _ = try await environment.folderMonitoring.resume(subscriptionID: subscriptionID)
            XCTFail("a stopped watch must not resume", file: file, line: line)
        } catch {
            XCTAssertEqual(error as? FolderMonitoringError, .subscriptionNotActive, file: file, line: line)
        }
    }

    private func admit(_ entry: CaptureQueueEntry, captureID: String, queue: CaptureIntentQueue) throws {
        let digest = "sha256:" + String(repeating: "b", count: 64)
        let staging = try queue.beginStage(
            itemID: entry.intent.itemId,
            requestDigest: digest,
            descriptorDigest: digest
        )
        _ = try queue.markAdmitted(
            itemID: entry.intent.itemId,
            attemptGeneration: staging.attemptGeneration,
            captureID: captureID
        )
    }

    // MARK: - Shared assertions

    /// What an interrupted establishment must look like on disk: a truthful
    /// pending intent, a grant that cannot discover anything, and no queued
    /// child.
    private func assertPendingAndUnusable(
        _ environment: DocumentIntakeEnvironment,
        intent: PendingFolderSubmission,
        memberGrants: Int,
        file: StaticString = #filePath,
        line: UInt = #line
    ) throws {
        XCTAssertEqual(
            environment.accessStore.pendingFolderEstablishments().map(\.submissionID),
            [intent.submissionID],
            file: file,
            line: line
        )
        let groups = environment.accessStore.rootDescriptors()
        XCTAssertEqual(groups.count, 1, "one grant per establishment", file: file, line: line)
        let group = try XCTUnwrap(groups.first, file: file, line: line)
        XCTAssertEqual(group.rootRef, intent.rootRef, file: file, line: line)
        XCTAssertEqual(group.state, .provisional, file: file, line: line)
        XCTAssertEqual(group.ownerRefs, intent.ownerRefs, file: file, line: line)
        XCTAssertEqual(group.binding, intent.binding, file: file, line: line)
        // Prepared is not established: nothing may discover through it.
        XCTAssertThrowsError(
            try environment.accessStore.withRootReadAccess(
                rootRef: intent.rootRef,
                expectedGeneration: group.generation,
                purpose: .memberDiscovery
            ) { _ in () },
            file: file,
            line: line
        ) {
            XCTAssertEqual(
                $0 as? NativeAccessError,
                .discoveryNotPermitted,
                file: file,
                line: line
            )
        }
        let connected = environment.accessStore.descriptors().filter { $0.state == .connected }
        XCTAssertEqual(connected.count, memberGrants, file: file, line: line)
        XCTAssertTrue(
            Set(connected.map(\.accessRef))
                .isSubset(of: Set(intent.members.map(\.accessRef))),
            "every grant belongs to this intent",
            file: file,
            line: line
        )
        XCTAssertTrue(environment.queue.allEntries().isEmpty, file: file, line: line)
    }

    /// What recovery must land: one established root under the original
    /// identities, each reviewed member enrolled exactly once, and no intent
    /// left over.
    private func assertEstablished(
        _ environment: DocumentIntakeEnvironment,
        matches intent: PendingFolderSubmission,
        file: StaticString = #filePath,
        line: UInt = #line
    ) throws {
        XCTAssertTrue(
            environment.accessStore.pendingFolderEstablishments().isEmpty,
            file: file,
            line: line
        )
        let groups = environment.accessStore.rootDescriptors()
        XCTAssertEqual(groups.count, 1, file: file, line: line)
        let group = try XCTUnwrap(groups.first, file: file, line: line)
        XCTAssertEqual(group.rootRef, intent.rootRef, file: file, line: line)
        XCTAssertEqual(group.generation, 1, "no replacement grant", file: file, line: line)
        XCTAssertEqual(group.state, .active, file: file, line: line)
        XCTAssertEqual(
            Set(group.ownerRefs).count,
            group.ownerRefs.count,
            "no duplicate owner",
            file: file,
            line: line
        )
        XCTAssertTrue(
            Set(group.ownerRefs).isSubset(of: Set(intent.ownerRefs)),
            file: file,
            line: line
        )
        XCTAssertEqual(
            group.memberRefs,
            intent.members.map(\.memberID).sorted(),
            "exactly the reviewed membership",
            file: file,
            line: line
        )
        let connected = environment.accessStore.descriptors().filter { $0.state == .connected }
        XCTAssertEqual(
            connected.map(\.accessRef).sorted(),
            intent.members.map(\.accessRef).sorted(),
            "one grant per reviewed member",
            file: file,
            line: line
        )
        XCTAssertTrue(
            connected.allSatisfy { $0.rootBindingGeneration == group.generation },
            file: file,
            line: line
        )
        if let runID = intent.runID {
            let run = try XCTUnwrap(environment.folderBulkRuns.run(id: runID), file: file, line: line)
            XCTAssertEqual(run.submissionID, intent.submissionID, file: file, line: line)
            XCTAssertEqual(run.manifestRevision, intent.manifestRevision, file: file, line: line)
            XCTAssertEqual(
                run.members.map(\.memberID),
                intent.members.map(\.memberID),
                file: file,
                line: line
            )
            XCTAssertEqual(environment.folderBulkRuns.all().count, 1, file: file, line: line)
        }
        if let subscriptionID = intent.subscriptionID {
            let subscription = try XCTUnwrap(
                environment.folderSubscriptions.subscription(id: subscriptionID),
                file: file,
                line: line
            )
            XCTAssertEqual(subscription.rootRef, intent.rootRef, file: file, line: line)
            XCTAssertEqual(subscription.rootBindingGeneration, 1, file: file, line: line)
            XCTAssertEqual(environment.folderSubscriptions.all().count, 1, file: file, line: line)
        }
        // Nothing was queued that this intent did not freeze.
        let queued = environment.queue.allEntries().map(\.intent.itemId)
        XCTAssertEqual(Set(queued).count, queued.count, "no duplicate child", file: file, line: line)
        XCTAssertTrue(
            Set(queued).isSubset(of: Set(intent.members.map(\.itemID))),
            file: file,
            line: line
        )
    }

    // MARK: - Helpers

    private struct Lane {
        let root: URL
        let state: URL
        let environment: DocumentIntakeEnvironment

        func reopen() throws -> DocumentIntakeEnvironment {
            try DocumentIntakeEnvironment(stateRoot: state, bookmarkCodec: BookmarkCodec())
        }
    }

    private func lane(files: Int) throws -> Lane {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-wp10-recovery-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        for index in 0..<files {
            try Data("file \(index)".utf8)
                .write(to: root.appendingPathComponent(String(format: "%02d.txt", index)))
        }
        // Inside the reviewed root on purpose: the controlled-store exclusion is
        // what keeps the companion's own state out of every plan.
        let state = root.appendingPathComponent("state", isDirectory: true)
        return Lane(
            root: root,
            state: state,
            environment: try DocumentIntakeEnvironment(
                stateRoot: state,
                bookmarkCodec: BookmarkCodec()
            )
        )
    }

    private func expectFault<T>(_ work: () async throws -> T) async {
        do {
            _ = try await work()
            XCTFail("expected the injected fault")
        } catch {
            XCTAssertTrue(error is InjectedFault, "unexpected error: \(error)")
        }
    }
}
