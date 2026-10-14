@testable import DocumentIntakeMacOS
import Foundation
import XCTest

/// WP-10: the reviewed one-off **Add current files** mode. Membership is frozen
/// at approval, the retained root group serves only those members, and the plan
/// publishes through the ordinary bounded queue with separate per-child
/// outcomes.
final class FolderOneOffIntakeTests: XCTestCase {
    private struct BookmarkCodec: SecurityScopedBookmarkCoding {
        func makeReadOnlyBookmark(for url: URL) throws -> Data { Data(url.path.utf8) }
        func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
            (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
        }
        func startAccessing(_ url: URL) -> Bool { true }
        func stopAccessing(_ url: URL) {}
    }

    private static let importOnce = FolderCapturePolicy(
        mode: .importOnce,
        recursive: true,
        includeHidden: false
    )

    // MARK: - Review and preview policy

    func testReviewShowsScopePoliciesCandidateCountsAndEveryExcludedReason() throws {
        let root = try temporaryDirectory()
        let state = root
            .deletingLastPathComponent()
            .appendingPathComponent("state-" + UUID().uuidString, isDirectory: true)
        try write("keep", to: root.appendingPathComponent("keep.txt"))
        try write("pdf", to: root.appendingPathComponent("brief.pdf"))
        try write("hidden", to: root.appendingPathComponent(".hidden.txt"))
        try write("ds", to: root.appendingPathComponent(".DS_Store"))
        try write("lock", to: root.appendingPathComponent("~$draft.docx"))
        try write("bin", to: root.appendingPathComponent("archive.bin"))
        try Data().write(to: root.appendingPathComponent("empty.txt"))
        try Data(repeating: 0x61, count: 1024 * 1024 + 1)
            .write(to: root.appendingPathComponent("huge.txt"))
        let cache = root.appendingPathComponent("node_modules", isDirectory: true)
        try FileManager.default.createDirectory(at: cache, withIntermediateDirectories: true)
        try write("ignored", to: cache.appendingPathComponent("readme.md"))
        let package = root.appendingPathComponent("Report.rtfd", isDirectory: true)
        try FileManager.default.createDirectory(at: package, withIntermediateDirectories: true)
        try write("inside", to: package.appendingPathComponent("TXT.rtf"))
        let outside = try temporaryDirectory()
        try write("secret", to: outside.appendingPathComponent("secret.txt"))
        try FileManager.default.createSymbolicLink(
            at: root.appendingPathComponent("escape"),
            withDestinationURL: outside
        )
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec()
        )
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )

        let plan = try submission.plan(root: root, policy: Self.importOnce)

        XCTAssertEqual(plan.members.map(\.displayName), ["brief.pdf", "keep.txt"])
        XCTAssertEqual(plan.rootDisplayName, root.lastPathComponent)
        XCTAssertEqual(plan.destinationWorkspaceRef, "workspace-1")
        XCTAssertTrue(plan.policy.recursive)
        XCTAssertEqual(plan.approvedFormatPolicyRef, "omnivia.folder-formats.v1")
        XCTAssertEqual(plan.exclusionsPolicyRef, "omnivia.folder-exclusions.v1")
        XCTAssertNil(plan.limit)
        XCTAssertEqual(plan.knownBytes, UInt64("keep".utf8.count + "pdf".utf8.count))
        XCTAssertEqual(plan.exclusionCounts[.hidden], 2)
        XCTAssertEqual(plan.exclusionCounts[.temporary], 1)
        XCTAssertEqual(plan.exclusionCounts[.toolCache], 1)
        XCTAssertEqual(plan.exclusionCounts[.package], 1)
        XCTAssertEqual(plan.exclusionCounts[.link], 1)
        XCTAssertEqual(plan.exclusionCounts[.unsupportedFormat], 1)
        XCTAssertEqual(plan.exclusionCounts[.emptyFile], 1)
        XCTAssertEqual(plan.exclusionCounts[.tooLarge], 1)
        // Nothing inside an excluded tree was examined at all.
        XCTAssertFalse(plan.members.contains { $0.displayName.contains("node_modules") })
        XCTAssertFalse(plan.members.contains { $0.displayName.contains("escape") })
        XCTAssertTrue(plan.scopeDetail.contains("2 supported files"))
        XCTAssertTrue(plan.scopeDetail.contains("unavailable"))
        // No capture, no evidence and no watcher came out of a preview.
        XCTAssertTrue(environment.queue.allEntries().isEmpty)
        XCTAssertTrue(environment.folderBulkRuns.all().isEmpty)
        XCTAssertTrue(environment.folderSubscriptions.all().isEmpty)
        XCTAssertTrue(environment.accessStore.rootDescriptors().isEmpty)
    }

    func testTheCompanionsOwnStoreIsNeverIngestedEvenFromInsideTheSelectedRoot() throws {
        let root = try temporaryDirectory()
        try write("keep", to: root.appendingPathComponent("keep.txt"))
        // The native store lives inside the reviewed root, and it holds a file
        // whose extension would otherwise qualify.
        let state = root.appendingPathComponent("core-state", isDirectory: true)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec()
        )
        try write("receipt", to: state.appendingPathComponent("receipt.md"))
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )

        let plan = try submission.plan(root: root, policy: Self.importOnce)

        XCTAssertEqual(plan.members.map(\.displayName), ["keep.txt"])
        XCTAssertEqual(plan.exclusionCounts[.controlledStore], 1)
        // And it cannot be selected as a root either.
        XCTAssertThrowsError(try submission.plan(root: state, policy: Self.importOnce)) {
            XCTAssertEqual($0 as? FolderCaptureSubmissionError, .unsafeRoot)
        }
    }

    func testRecursionOffNeverReadsAnUnapprovedDescendant() async throws {
        let root = try temporaryDirectory()
        try write("top", to: root.appendingPathComponent("top.txt"))
        let nested = root.appendingPathComponent("nested", isDirectory: true)
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        try write("deep", to: nested.appendingPathComponent("deep.txt"))
        let environment = try environment(for: root)
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )

        let flat = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(mode: .importOnce, recursive: false, includeHidden: false)
        )
        let entries = try await submission.submit(flat)

        XCTAssertEqual(flat.members.map(\.displayName), ["top.txt"])
        XCTAssertEqual(flat.exclusionCounts[.notRecursive], 1)
        XCTAssertEqual(entries.compactMap(\.intent.displayName), ["top.txt"])
        // The unapproved descendant has no member route at all, so nothing can
        // read it through this group.
        let group = try XCTUnwrap(environment.accessStore.rootDescriptors().first)
        XCTAssertEqual(group.memberRefs.count, 1)
        XCTAssertEqual(
            try XCTUnwrap(environment.folderBulkRuns.all().first).members.count,
            1
        )
    }

    func testOverLimitScopeIsRefusedAndEnrolsNothing() async throws {
        let root = try temporaryDirectory()
        for index in 0..<6 {
            try write("f\(index)", to: root.appendingPathComponent("f\(index).txt"))
        }
        let environment = try environment(for: root)
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1",
            limits: FolderScopeLimits(
                maximumEligibleFiles: 3,
                maximumExaminedEntries: 100,
                maximumDepth: 32
            )
        )

        let plan = try submission.plan(root: root, policy: Self.importOnce)

        // The breach is displayed with the counts reviewed so far; the first
        // subset is not quietly accepted as the whole folder.
        XCTAssertEqual(plan.limit, .eligibleFiles(3))
        XCTAssertEqual(plan.members.count, 3)
        XCTAssertTrue(plan.limit?.reviewDetail.contains("3 supported files") == true)
        await expectRefusal(.scanLimitExceeded) { try await submission.submit(plan) }
        XCTAssertTrue(environment.folderBulkRuns.all().isEmpty)
        XCTAssertTrue(environment.accessStore.rootDescriptors().isEmpty)
        XCTAssertTrue(environment.queue.allEntries().isEmpty)
    }

    func testAnExaminedEntryLimitIsRefusedTheSameWay() async throws {
        let root = try temporaryDirectory()
        for index in 0..<6 {
            try write("f\(index)", to: root.appendingPathComponent("f\(index).txt"))
        }
        let environment = try environment(for: root)
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1",
            limits: FolderScopeLimits(
                maximumEligibleFiles: 10_000,
                maximumExaminedEntries: 2,
                maximumDepth: 32
            )
        )

        let plan = try submission.plan(root: root, policy: Self.importOnce)

        XCTAssertEqual(plan.limit, .examinedEntries(2))
        await expectRefusal(.scanLimitExceeded) { try await submission.submit(plan) }
        XCTAssertTrue(environment.folderBulkRuns.all().isEmpty)
    }

    func testAPartlyUnreadableFolderIsRefusedAndEnrolsNothing() async throws {
        let root = try temporaryDirectory()
        try write("readable", to: root.appendingPathComponent("readable.txt"))
        let nested = root.appendingPathComponent("nested", isDirectory: true)
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        try write("unseen", to: nested.appendingPathComponent("unseen.txt"))
        let environment = try environment(for: root)
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )
        try XCTSkipUnless(
            makeUnreadable(nested),
            "This system still lists an unreadable directory."
        )

        let plan = try submission.plan(root: root, policy: Self.importOnce)

        // The preview says plainly that this is not the whole folder, without
        // naming where the gap is, and submission refuses the visible subset.
        XCTAssertEqual(plan.members.map(\.displayName), ["readable.txt"])
        XCTAssertEqual(plan.coverageFailures.map(\.relativeComponents), [["nested"]])
        let detail = try XCTUnwrap(plan.coverageDetail)
        XCTAssertTrue(detail.contains("1 item"))
        XCTAssertFalse(detail.contains(root.path))
        await expectRefusal(.coverageIncomplete) { try await submission.submit(plan) }
        XCTAssertTrue(environment.folderBulkRuns.all().isEmpty)
        XCTAssertTrue(environment.folderSubscriptions.all().isEmpty)
        XCTAssertTrue(environment.accessStore.rootDescriptors().isEmpty)
        XCTAssertTrue(environment.queue.allEntries().isEmpty)
    }

    func testAnUnreadableFolderIsRefusedRatherThanWatchedAsEmpty() async throws {
        let root = try temporaryDirectory()
        try write("readable", to: root.appendingPathComponent("readable.txt"))
        let environment = try environment(for: root)
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )
        try XCTSkipUnless(makeUnreadable(root), "This system still lists an unreadable directory.")

        // A root that cannot be listed is not an empty folder, so the mode that
        // accepts an empty scope — watch new and changed only — cannot be used
        // to establish a watch over nothing.
        XCTAssertThrowsError(
            try submission.plan(
                root: root,
                policy: FolderCapturePolicy(
                    mode: .keepUpdated,
                    recursive: true,
                    includeHidden: false,
                    watchInitialContent: .onlyNewAndChanged
                )
            )
        ) { XCTAssertEqual($0 as? FolderCaptureSubmissionError, .rootUnavailable) }
        XCTAssertTrue(environment.folderSubscriptions.all().isEmpty)
    }

    func testAWatchedRootThatLosesCoverageNeedsAttentionInsteadOfDroppingMembers() async throws {
        let root = try temporaryDirectory()
        try write("top", to: root.appendingPathComponent("top.txt"))
        let nested = root.appendingPathComponent("nested", isDirectory: true)
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        try write("deep", to: nested.appendingPathComponent("deep.txt"))
        let environment = try environment(for: root)
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(
                mode: .keepUpdated,
                recursive: true,
                includeHidden: false,
                watchInitialContent: .onlyNewAndChanged
            )
        )
        _ = try await submission.submit(plan)
        XCTAssertEqual(plan.members.count, 2)
        XCTAssertEqual(environment.folderSubscriptions.all().first?.state, .active)

        // Access to part of the watched scope is lost after the watch exists.
        try XCTSkipUnless(
            makeUnreadable(nested),
            "This system still lists an unreadable directory."
        )
        let reports = await environment.folderMonitoring.reconcileAll()

        // The member inside it is not treated as deleted, and the subscription
        // asks for attention instead of reporting a smaller folder.
        let subscription = try XCTUnwrap(environment.folderSubscriptions.all().first)
        XCTAssertEqual(reports.first?.state, .needsAccess)
        XCTAssertEqual(subscription.state, .needsAccess)
        XCTAssertEqual(
            subscription.members.map(\.displayName).sorted(),
            ["nested/deep.txt", "top.txt"]
        )
        XCTAssertTrue(subscription.members.allSatisfy { $0.state == .present })
    }

    func testTheReviewedPreviewAndTheActualScopeAgreeOnMembership() async throws {
        let root = try temporaryDirectory()
        try write("keep", to: root.appendingPathComponent("keep.txt"))
        try write("ds", to: root.appendingPathComponent(".DS_Store"))
        try write("lock", to: root.appendingPathComponent("~$draft.docx"))
        try Data().write(to: root.appendingPathComponent("empty.txt"))
        try Data(repeating: 0x61, count: 1024 * 1024 + 1)
            .write(to: root.appendingPathComponent("huge.txt"))
        let cache = root.appendingPathComponent("node_modules", isDirectory: true)
        try FileManager.default.createDirectory(at: cache, withIntermediateDirectories: true)
        try write("ignored", to: cache.appendingPathComponent("readme.md"))
        let package = root.appendingPathComponent("Report.rtfd", isDirectory: true)
        try FileManager.default.createDirectory(at: package, withIntermediateDirectories: true)
        try write("inside", to: package.appendingPathComponent("TXT.rtf"))
        let outside = try temporaryDirectory()
        try write("secret", to: outside.appendingPathComponent("secret.txt"))
        try FileManager.default.createSymbolicLink(
            at: root.appendingPathComponent("escape"),
            withDestinationURL: outside
        )
        let environment = try environment(for: root)
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )
        let plan = try submission.plan(
            root: root,
            policy: FolderCapturePolicy(
                mode: .keepUpdated,
                recursive: true,
                includeHidden: false,
                watchInitialContent: .onlyNewAndChanged
            )
        )

        _ = try await submission.submit(plan)
        _ = await environment.folderMonitoring.reconcileAll()

        // The watched-root scan and the preview run the one shared traversal,
        // so the actual scope holds exactly the reviewed membership and the
        // reconcile finds nothing new to read.
        let subscription = try XCTUnwrap(environment.folderSubscriptions.all().first)
        XCTAssertEqual(plan.members.map(\.displayName), ["keep.txt"])
        XCTAssertEqual(subscription.members.map(\.displayName), ["keep.txt"])
        XCTAssertTrue(environment.queue.allEntries().isEmpty)
    }

    // MARK: - Frozen membership

    func testMembershipIsFrozenAtApprovalAndALaterSiblingIsRefusedAfterRestart() async throws {
        let root = try temporaryDirectory()
        try write("one", to: root.appendingPathComponent("one.txt"))
        try write("two", to: root.appendingPathComponent("two.md"))
        let state = stateDirectory(for: root)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec()
        )
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )
        let plan = try submission.plan(root: root, policy: Self.importOnce)
        _ = try await submission.submit(plan)
        let run = try XCTUnwrap(environment.folderBulkRuns.all().first)
        let group = try XCTUnwrap(environment.accessStore.rootDescriptors().first)
        XCTAssertEqual(group.authority, .importPlan)
        XCTAssertTrue(group.membersFrozen)
        XCTAssertEqual(group.memberRefs.sorted(), run.members.map(\.memberID).sorted())

        // A sibling appears after approval.
        try write("three", to: root.appendingPathComponent("three.txt"))

        // It is not in the frozen manifest, and it cannot be enrolled now…
        XCTAssertFalse(run.members.contains { $0.displayName == "three.txt" })
        XCTAssertThrowsError(
            try environment.accessStore.adoptRootMember(
                accessRef: "access-late",
                linkedDocumentID: "doc-late",
                rootRef: group.rootRef,
                memberRef: "member-late",
                relativeComponents: ["three.txt"],
                expectedRootGeneration: group.generation
            )
        ) { XCTAssertEqual($0 as? NativeAccessError, .membershipFrozen) }

        // …nor after a restart.
        let reopened = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec()
        )
        let restored = try XCTUnwrap(reopened.accessStore.rootDescriptors().first)
        XCTAssertTrue(restored.membersFrozen)
        XCTAssertEqual(restored.memberRefs, group.memberRefs)
        XCTAssertThrowsError(
            try reopened.accessStore.adoptRootMember(
                accessRef: "access-late",
                linkedDocumentID: "doc-late",
                rootRef: restored.rootRef,
                memberRef: "member-late",
                relativeComponents: ["three.txt"],
                expectedRootGeneration: restored.generation
            )
        ) { XCTAssertEqual($0 as? NativeAccessError, .membershipFrozen) }
        // The run itself never grew, and nothing new was queued.
        let restoredRun = try XCTUnwrap(reopened.folderBulkRuns.run(id: run.runID))
        XCTAssertEqual(restoredRun.members.map(\.memberID), run.members.map(\.memberID))
    }

    func testACompletedPlanNarrowsToFrozenMembersOnlyAndKeepsManualMemberReads() async throws {
        let root = try temporaryDirectory()
        try write("retained", to: root.appendingPathComponent("retained.txt"))
        let state = stateDirectory(for: root)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec()
        )
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )
        let entries = try await submission.submit(
            try submission.plan(root: root, policy: Self.importOnce)
        )
        let run = try XCTUnwrap(environment.folderBulkRuns.all().first)
        let member = try XCTUnwrap(run.members.first)
        try admit(
            try XCTUnwrap(entries.first),
            captureID: "dcap-" + String(repeating: "a", count: 64),
            queue: environment.queue
        )

        _ = await environment.folderBulkCoordinator.reconcileReceipts()

        let group = try XCTUnwrap(environment.accessStore.rootDescriptors().first)
        XCTAssertEqual(environment.folderBulkRuns.run(id: run.runID)?.state, .complete)
        XCTAssertEqual(group.authority, .frozenMembersOnly)
        // The run's own owner is gone; the grant survives for its member route.
        XCTAssertTrue(group.ownerRefs.isEmpty)
        XCTAssertEqual(group.state, .active)

        // After a restart, a manual member read still works through that
        // frozen-members authority…
        let reopened = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec()
        )
        let text = try reopened.accessStore.withReadAccess(
            accessRef: member.accessRef,
            expectedGeneration: member.bindingGeneration,
            expectedRootBindingGeneration: run.rootBindingGeneration
        ) { try String(contentsOf: $0, encoding: .utf8) }
        XCTAssertEqual(text, "retained")

        // …and a newly created sibling still cannot be read through it, by
        // discovery or by enrolment.
        try write("sibling", to: root.appendingPathComponent("sibling.txt"))
        XCTAssertThrowsError(
            try reopened.accessStore.withRootReadAccess(
                rootRef: run.rootRef,
                expectedGeneration: run.rootBindingGeneration,
                purpose: .memberDiscovery
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .discoveryNotPermitted) }
        XCTAssertThrowsError(
            try reopened.accessStore.adoptRootMember(
                accessRef: "access-sibling",
                linkedDocumentID: "doc-sibling",
                rootRef: run.rootRef,
                memberRef: "member-sibling",
                relativeComponents: ["sibling.txt"],
                expectedRootGeneration: run.rootBindingGeneration
            )
        ) { XCTAssertEqual($0 as? NativeAccessError, .membershipFrozen) }
        // A new reviewed scope is the only way in, and it is a separate group.
        let again = FolderCaptureSubmission(environment: reopened, workspaceRef: "workspace-1")
        let second = try again.plan(root: root, policy: Self.importOnce)
        XCTAssertEqual(second.members.map(\.displayName), ["retained.txt", "sibling.txt"])
        _ = try await again.submit(second)
        XCTAssertEqual(reopened.accessStore.rootDescriptors().count, 2)
        XCTAssertEqual(reopened.folderBulkRuns.all().count, 2)
    }

    func testAnotherDestinationCannotJoinAnApprovedRootGroup() throws {
        let root = try temporaryDirectory()
        let state = stateDirectory(for: root)
        let store = try NativeAccessStore(
            stateDirectory: state,
            codec: BookmarkCodec(),
            gate: PublicationGate()
        )
        _ = try store.adoptRoot(
            rootRef: "root-a",
            ownerRef: "run-a",
            selectedURL: root,
            binding: NativeRootAccessBinding(.companion(workspaceRef: "workspace-1")),
            authority: .watchAndMembers
        )

        XCTAssertThrowsError(
            try store.adoptRoot(
                rootRef: "root-a",
                ownerRef: "run-b",
                selectedURL: root,
                binding: NativeRootAccessBinding(.companion(workspaceRef: "workspace-2")),
                authority: .watchAndMembers
            )
        ) { XCTAssertEqual($0 as? NativeAccessError, .bindingMismatch) }
    }

    // MARK: - Containment

    func testAMemberMovedOutOfTheRootCannotBeRead() async throws {
        let root = try temporaryDirectory()
        let nested = root.appendingPathComponent("nested", isDirectory: true)
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        try write("moving", to: nested.appendingPathComponent("moving.txt"))
        let environment = try environment(for: root)
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )
        let plan = try submission.plan(root: root, policy: Self.importOnce)
        _ = try await submission.submit(plan)
        let run = try XCTUnwrap(environment.folderBulkRuns.all().first)
        let member = try XCTUnwrap(run.members.first)

        let elsewhere = try temporaryDirectory()
        try FileManager.default.moveItem(
            at: nested.appendingPathComponent("moving.txt"),
            to: elsewhere.appendingPathComponent("moving.txt")
        )

        XCTAssertThrowsError(
            try environment.accessStore.withReadAccess(
                accessRef: member.accessRef,
                expectedGeneration: member.bindingGeneration,
                expectedRootBindingGeneration: run.rootBindingGeneration
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .memberOutsideRoot) }

        // Replacing the member's own parent with a link out of scope is refused
        // too, so the locator never follows the file to its new home.
        try FileManager.default.removeItem(at: nested)
        try FileManager.default.createSymbolicLink(at: nested, withDestinationURL: elsewhere)
        XCTAssertThrowsError(
            try environment.accessStore.withReadAccess(
                accessRef: member.accessRef,
                expectedGeneration: member.bindingGeneration,
                expectedRootBindingGeneration: run.rootBindingGeneration
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .memberOutsideRoot) }
    }

    func testAFinderAliasMemberRouteIsRefused() throws {
        let root = try temporaryDirectory()
        let outside = try temporaryDirectory()
        try write("secret", to: outside.appendingPathComponent("secret.txt"))
        let alias = root.appendingPathComponent("alias.txt")
        do {
            let bookmark = try outside.appendingPathComponent("secret.txt").bookmarkData(
                options: .suitableForBookmarkFile,
                includingResourceValuesForKeys: nil,
                relativeTo: nil
            )
            try URL.writeBookmarkData(bookmark, to: alias)
        } catch {
            throw XCTSkip("This system does not permit writing a Finder alias file.")
        }
        let values = try alias.resourceValues(forKeys: [.isAliasFileKey])
        try XCTSkipUnless(values.isAliasFile == true, "Alias file was not recognised.")
        let state = stateDirectory(for: root)
        let store = try NativeAccessStore(
            stateDirectory: state,
            codec: BookmarkCodec(),
            gate: PublicationGate()
        )
        let group = try store.adoptRoot(
            rootRef: "root-a",
            ownerRef: "run-a",
            selectedURL: root,
            binding: NativeRootAccessBinding(.companion(workspaceRef: "workspace-1")),
            authority: .watchAndMembers
        )
        _ = try store.adoptRootMember(
            accessRef: "access-a",
            linkedDocumentID: "doc-a",
            rootRef: "root-a",
            memberRef: "member-a",
            relativeComponents: ["alias.txt"],
            expectedRootGeneration: group.generation
        )

        XCTAssertThrowsError(
            try store.withReadAccess(
                accessRef: "access-a",
                expectedGeneration: 1,
                expectedRootBindingGeneration: group.generation
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .memberOutsideRoot) }
    }

    func testAPackageOrDirectoryMemberRouteIsRefused() throws {
        let root = try temporaryDirectory()
        let package = root.appendingPathComponent("Report.rtfd", isDirectory: true)
        try FileManager.default.createDirectory(at: package, withIntermediateDirectories: true)
        try write("inside", to: package.appendingPathComponent("TXT.rtf"))
        let store = try NativeAccessStore(
            stateDirectory: stateDirectory(for: root),
            codec: BookmarkCodec(),
            gate: PublicationGate()
        )
        let group = try store.adoptRoot(
            rootRef: "root-a",
            ownerRef: "run-a",
            selectedURL: root,
            binding: NativeRootAccessBinding(.companion(workspaceRef: "workspace-1")),
            authority: .watchAndMembers
        )
        for (accessRef, components) in [
            ("access-package", ["Report.rtfd"]),
            ("access-inside", ["Report.rtfd", "TXT.rtf"]),
        ] {
            _ = try store.adoptRootMember(
                accessRef: accessRef,
                linkedDocumentID: "doc-" + accessRef,
                rootRef: "root-a",
                memberRef: "member-" + accessRef,
                relativeComponents: components,
                expectedRootGeneration: group.generation
            )
            XCTAssertThrowsError(
                try store.withReadAccess(
                    accessRef: accessRef,
                    expectedGeneration: 1,
                    expectedRootBindingGeneration: group.generation
                ) { _ in () }
            ) { XCTAssertEqual($0 as? NativeAccessError, .memberOutsideRoot) }
        }
    }

    func testAStaleRootGenerationCannotReadOrPublish() throws {
        let root = try temporaryDirectory()
        try write("a", to: root.appendingPathComponent("a.txt"))
        let store = try NativeAccessStore(
            stateDirectory: stateDirectory(for: root),
            codec: BookmarkCodec(),
            gate: PublicationGate()
        )
        let binding = NativeRootAccessBinding(.companion(workspaceRef: "workspace-1"))
        let first = try store.adoptRoot(
            rootRef: "root-a",
            ownerRef: "run-a",
            selectedURL: root,
            binding: binding,
            authority: .watchAndMembers
        )
        let member = try store.adoptRootMember(
            accessRef: "access-a",
            linkedDocumentID: "doc-a",
            rootRef: "root-a",
            memberRef: "member-a",
            relativeComponents: ["a.txt"],
            expectedRootGeneration: first.generation
        )
        XCTAssertNoThrow(
            try store.withReadAccess(
                accessRef: "access-a",
                expectedGeneration: member.generation,
                expectedRootBindingGeneration: first.generation
            ) { _ in () }
        )

        // The root is relinked: its owner and member go, then it is adopted
        // again under the next generation.
        _ = try store.disconnect(accessRef: "access-a")
        _ = try store.releaseRoot(rootRef: "root-a", ownerRef: "run-a")
        let second = try store.adoptRoot(
            rootRef: "root-a",
            ownerRef: "run-b",
            selectedURL: root,
            binding: binding,
            authority: .watchAndMembers
        )
        XCTAssertEqual(second.generation, first.generation + 1)

        let stale = NativeAccessExpectation(
            accessRef: "access-a",
            generation: member.generation,
            rootBindingGeneration: first.generation
        )
        XCTAssertThrowsError(
            try store.withReadAccess(
                accessRef: stale.accessRef,
                expectedGeneration: stale.generation,
                expectedRootBindingGeneration: stale.rootBindingGeneration
            ) { _ in () }
        ) { XCTAssertEqual($0 as? NativeAccessError, .disconnected) }
        XCTAssertThrowsError(try store.withValidatedGenerations([stale]) { () }) {
            XCTAssertEqual($0 as? NativeAccessError, .disconnected)
        }
    }

    // MARK: - Bounded progressive processing

    func testProgressiveBatchesKeepQueueCapsExactProgressAndPerChildOutcomes() async throws {
        let root = try temporaryDirectory()
        for index in 0..<12 {
            try write("file \(index)", to: root.appendingPathComponent(String(format: "%02d.txt", index)))
        }
        let state = stateDirectory(for: root)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec()
        )
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )
        let plan = try submission.plan(root: root, policy: Self.importOnce)

        let first = try await submission.submit(plan)

        // Acknowledging the plan publishes one ordinary bounded batch, not
        // twelve added files.
        XCTAssertEqual(plan.members.count, 12)
        XCTAssertEqual(first.count, FolderBulkRunCoordinator.maximumBatchItems)
        let run = try XCTUnwrap(environment.folderBulkRuns.all().first)
        XCTAssertEqual(run.members.count, 12)
        XCTAssertEqual(run.publishedCount, 10)
        XCTAssertEqual(run.plannedCount, 2)
        XCTAssertEqual(run.admittedCount, 0)
        XCTAssertEqual(run.state, .adding)
        XCTAssertEqual(environment.queue.allEntries().count, 10)
        // Every published item carries its own identity, key and bulk lineage.
        XCTAssertEqual(Set(first.map(\.intent.itemId)).count, 10)
        XCTAssertEqual(Set(first.map(\.intent.operationKey)).count, 10)
        XCTAssertTrue(first.allSatisfy { $0.intent.bulkOrigin?.bulkRunId == run.runID })
        XCTAssertTrue(first.allSatisfy {
            $0.intent.bulkOrigin?.manifestRevision == run.manifestRevision
        })

        // The next bounded batch carries the rest, still inside the ordinary
        // pending queue limits.
        let second = await environment.folderBulkCoordinator.advance(runID: run.runID)
        XCTAssertEqual(second.newlyPublished.count, 2)
        XCTAssertEqual(second.published, 12)
        XCTAssertEqual(second.planned, 0)
        XCTAssertEqual(second.admitted, 0)
        XCTAssertTrue(second.progressDetail.contains("0 added"))
        XCTAssertTrue(second.progressDetail.contains("of 12"))
        XCTAssertLessThanOrEqual(
            environment.queue.allEntries().count,
            CaptureIntentQueue.maximumUnresolvedItems
        )

        for (index, entry) in (first + second.newlyPublished).enumerated() {
            try admit(
                entry,
                captureID: "dcap-" + String(repeating: String(format: "%02x", index), count: 32),
                queue: environment.queue
            )
        }
        let report = await environment.folderBulkCoordinator.reconcileReceipts()

        let completed = try XCTUnwrap(report.first { $0.runID == run.runID })
        XCTAssertEqual(completed.admittedCount, 12)
        XCTAssertEqual(completed.state, .complete)
    }

    func testOneUnreadableChildNeedsAttentionWhileTheOthersStillPublish() async throws {
        let root = try temporaryDirectory()
        try write("ok", to: root.appendingPathComponent("a-ok.txt"))
        try write("other", to: root.appendingPathComponent("c-other.txt"))
        let denied = root.appendingPathComponent("b-denied.txt")
        try write("denied", to: denied)
        let environment = try environment(for: root)
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )
        let plan = try submission.plan(root: root, policy: Self.importOnce)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o000],
            ofItemAtPath: denied.path
        )
        addTeardownBlock {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: denied.path
            )
        }

        let published = try await submission.submit(plan)

        let run = try XCTUnwrap(environment.folderBulkRuns.all().first)
        XCTAssertEqual(published.count, 2)
        XCTAssertEqual(
            published.compactMap(\.intent.displayName).sorted(),
            ["a-ok.txt", "c-other.txt"]
        )
        let attention = run.members.filter { $0.state == .needsAttention }
        XCTAssertEqual(attention.map(\.displayName), ["b-denied.txt"])
        XCTAssertEqual(attention.first?.failureCode, "preparation_failed")
        XCTAssertEqual(run.state, .adding)

        // Retry acts on the same frozen membership and discovers nothing.
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: denied.path
        )
        try write("new", to: root.appendingPathComponent("d-new.txt"))
        _ = try await environment.folderBulkCoordinator.retryUnavailable(runID: run.runID)
        let report = await environment.folderBulkCoordinator.advance(runID: run.runID)

        XCTAssertEqual(
            report.newlyPublished.compactMap(\.intent.displayName),
            ["b-denied.txt"]
        )
        XCTAssertEqual(report.total, 3)
        XCTAssertEqual(report.needsAttention, 0)
        XCTAssertFalse(
            environment.folderBulkRuns.run(id: run.runID)?
                .members.contains { $0.displayName == "d-new.txt" } == true
        )
    }

    func testPauseResumeStopAndRestartPreserveSuccessfulAndQueuedChildren() async throws {
        let root = try temporaryDirectory()
        for index in 0..<12 {
            try write("file \(index)", to: root.appendingPathComponent(String(format: "%02d.txt", index)))
        }
        let state = stateDirectory(for: root)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec()
        )
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )
        let first = try await submission.submit(
            try submission.plan(root: root, policy: Self.importOnce)
        )
        let runID = try XCTUnwrap(environment.folderBulkRuns.all().first).runID
        try admit(
            try XCTUnwrap(first.first),
            captureID: "dcap-" + String(repeating: "a", count: 64),
            queue: environment.queue
        )

        _ = try await environment.folderBulkCoordinator.pause(runID: runID)
        let paused = await environment.folderBulkCoordinator.advance(runID: runID)
        XCTAssertEqual(paused.state, .paused)
        XCTAssertTrue(paused.newlyPublished.isEmpty)

        // The pause is durable, and a restart does not resume it by itself.
        let reopened = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec()
        )
        let afterRestart = await reopened.folderBulkCoordinator.advance(runID: runID)
        XCTAssertEqual(afterRestart.state, .paused)
        XCTAssertEqual(afterRestart.admitted, 1)
        XCTAssertEqual(afterRestart.published, 9)
        XCTAssertEqual(afterRestart.planned, 2)

        _ = try await reopened.folderBulkCoordinator.resume(runID: runID)
        let stopped = try await reopened.folderBulkCoordinator.stopRemaining(runID: runID)

        // Only unstarted members stop. The admitted child keeps its canonical
        // success and the published ones keep reconciling.
        XCTAssertEqual(stopped.stoppedCount, 2)
        XCTAssertEqual(stopped.admittedCount, 1)
        XCTAssertEqual(stopped.publishedCount, 9)
        XCTAssertEqual(stopped.plannedCount, 0)
        XCTAssertEqual(reopened.queue.allEntries().count, 10)
    }

    func testStopRemainingStopsEveryUnpublishedChildIncludingOnesNeedingAttention() async throws {
        let root = try temporaryDirectory()
        for index in 0..<12 {
            try write("file \(index)", to: root.appendingPathComponent(String(format: "%02d.txt", index)))
        }
        let environment = try environment(for: root)
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )
        let plan = try submission.plan(root: root, policy: Self.importOnce)
        // One member of the first bounded batch cannot be read, so it lands in
        // needsAttention while two later members are still merely planned.
        let denied = root.appendingPathComponent("03.txt")
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o000],
            ofItemAtPath: denied.path
        )
        addTeardownBlock {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: denied.path
            )
        }
        let published = try await submission.submit(plan)
        let runID = try XCTUnwrap(environment.folderBulkRuns.all().first).runID
        let captureID = "dcap-" + String(repeating: "a", count: 64)
        let admitted = try XCTUnwrap(published.first)
        try admit(admitted, captureID: captureID, queue: environment.queue)
        _ = await environment.folderBulkCoordinator.reconcileReceipts()
        let before = try XCTUnwrap(environment.folderBulkRuns.run(id: runID))
        XCTAssertEqual(before.admittedCount, 1)
        XCTAssertEqual(before.attentionCount, 1)
        XCTAssertEqual(before.plannedCount, 2)

        let stopped = try await environment.folderBulkCoordinator.stopRemaining(runID: runID)

        let states = Dictionary(
            uniqueKeysWithValues: stopped.members.map { ($0.displayName, $0.state) }
        )
        XCTAssertEqual(states["03.txt"], .stopped)
        XCTAssertEqual(states["10.txt"], .stopped)
        XCTAssertEqual(states["11.txt"], .stopped)
        XCTAssertEqual(stopped.stoppedCount, 3)
        XCTAssertEqual(stopped.plannedCount, 0)
        XCTAssertEqual(stopped.attentionCount, 0)
        // The successful outcomes are untouched.
        XCTAssertEqual(stopped.admittedCount, 1)
        XCTAssertEqual(stopped.publishedCount, 8)
        XCTAssertEqual(
            states[try XCTUnwrap(admitted.intent.displayName)],
            .admitted
        )
        XCTAssertEqual(environment.queue.allEntries().count, published.count)
        XCTAssertEqual(
            environment.queue.allEntries()
                .first { $0.intent.itemId == admitted.intent.itemId }?.admittedCaptureID,
            captureID
        )
    }

    func testARestartContinuesTheSameFrozenManifestWithoutRereadingPublishedBytes() async throws {
        let root = try temporaryDirectory()
        for index in 0..<12 {
            try write("file \(index)", to: root.appendingPathComponent(String(format: "%02d.txt", index)))
        }
        let state = stateDirectory(for: root)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec()
        )
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )
        let first = try await submission.submit(
            try submission.plan(root: root, policy: Self.importOnce)
        )
        let run = try XCTUnwrap(environment.folderBulkRuns.all().first)
        for (index, entry) in first.enumerated() {
            try admit(
                entry,
                captureID: "dcap-" + String(repeating: String(format: "%x", index), count: 64),
                queue: environment.queue
            )
        }
        let publishedDigests = Dictionary(
            uniqueKeysWithValues: first.map { ($0.intent.itemId, $0.intent.localIntegrityDigest) }
        )
        // The source changes after those bytes were published.
        for index in 0..<10 {
            try write(
                "rewritten \(index)",
                to: root.appendingPathComponent(String(format: "%02d.txt", index))
            )
        }

        let reopened = try DocumentIntakeEnvironment(
            stateRoot: state,
            bookmarkCodec: BookmarkCodec()
        )
        let report = await reopened.folderBulkCoordinator.advance(runID: run.runID)

        // Recovery continues with the remaining frozen members and never
        // rereads an already published original: every admitted item still
        // carries exactly the bytes it was published with.
        XCTAssertEqual(report.admitted, 10)
        XCTAssertEqual(report.newlyPublished.count, 2)
        XCTAssertEqual(report.total, 12)
        for entry in reopened.queue.allEntries() where entry.state == .admitted {
            XCTAssertEqual(
                entry.intent.localIntegrityDigest,
                publishedDigests[entry.intent.itemId],
                entry.intent.displayName ?? entry.intent.itemId
            )
        }
        XCTAssertEqual(
            reopened.folderBulkRuns.run(id: run.runID)?.members.map(\.memberID),
            run.members.map(\.memberID)
        )
    }

    func testTheFrozenManifestCannotBeWidenedOrRebound() async throws {
        let root = try temporaryDirectory()
        try write("one", to: root.appendingPathComponent("one.txt"))
        let environment = try environment(for: root)
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )
        _ = try await submission.submit(
            try submission.plan(root: root, policy: Self.importOnce)
        )
        let run = try XCTUnwrap(environment.folderBulkRuns.all().first)
        let store = environment.folderBulkRuns

        XCTAssertThrowsError(
            try store.update(id: run.runID) { value in
                value.members.append(value.members[0])
            }
        ) { XCTAssertEqual($0 as? FolderBulkRunStoreError, .invalidState) }
        XCTAssertThrowsError(
            try store.update(id: run.runID) { value in value.members.removeAll() }
        ) { XCTAssertEqual($0 as? FolderBulkRunStoreError, .invalidState) }
        XCTAssertThrowsError(
            try store.update(id: run.runID) { value in
                value.exclusionsPolicyRef = "omnivia.folder-exclusions.v99"
            }
        ) { XCTAssertEqual($0 as? FolderBulkRunStoreError, .invalidState) }
        // Progress itself is still allowed.
        XCTAssertNoThrow(
            try store.update(id: run.runID) { value in
                value.members[0].state = .needsAttention
            }
        )
        XCTAssertEqual(
            store.run(id: run.runID)?.approvedFormatPolicyRef,
            "omnivia.folder-formats.v1"
        )
    }

    func testOriginalBytesAndAttributesAreUnchangedByAFolderRun() async throws {
        let root = try temporaryDirectory()
        let keys: Set<URLResourceKey> = [
            .fileSizeKey, .contentModificationDateKey, .creationDateKey,
            .fileResourceIdentifierKey,
        ]
        var sources: [URL: (Data, URLResourceValues)] = [:]
        for name in ["one.txt", "two.md", "three.csv"] {
            let url = root.appendingPathComponent(name)
            try write("contents of \(name)", to: url)
            sources[url] = (try Data(contentsOf: url), try url.resourceValues(forKeys: keys))
        }
        let environment = try environment(for: root)
        let submission = FolderCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1"
        )

        _ = try await submission.submit(
            try submission.plan(root: root, policy: Self.importOnce)
        )

        for (url, expected) in sources {
            XCTAssertEqual(try Data(contentsOf: url), expected.0, url.lastPathComponent)
            let after = try url.resourceValues(forKeys: keys)
            XCTAssertEqual(after.fileSize, expected.1.fileSize)
            XCTAssertEqual(after.contentModificationDate, expected.1.contentModificationDate)
            XCTAssertEqual(after.creationDate, expected.1.creationDate)
            XCTAssertEqual(
                String(describing: after.fileResourceIdentifier),
                String(describing: expected.1.fileResourceIdentifier)
            )
        }
        XCTAssertEqual(
            Set(try FileManager.default.contentsOfDirectory(atPath: root.path)),
            ["one.txt", "two.md", "three.csv", "state"]
        )
    }

    // MARK: - Helpers

    private func environment(for root: URL) throws -> DocumentIntakeEnvironment {
        try DocumentIntakeEnvironment(
            stateRoot: stateDirectory(for: root),
            bookmarkCodec: BookmarkCodec()
        )
    }

    /// Inside the reviewed root on purpose: the controlled-store exclusion is
    /// what keeps it out of every plan.
    private func stateDirectory(for root: URL) -> URL {
        root.appendingPathComponent("state", isDirectory: true)
    }

    private func write(_ contents: String, to url: URL) throws {
        try Data(contents.utf8).write(to: url)
    }

    /// True once the directory really cannot be listed. Returns false where the
    /// process can read it anyway — running as root, for instance — so the test
    /// skips rather than asserting on a failure that did not happen.
    private func makeUnreadable(_ url: URL) throws -> Bool {
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o000],
            ofItemAtPath: url.path
        )
        addTeardownBlock {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o700],
                ofItemAtPath: url.path
            )
        }
        return (try? FileManager.default.contentsOfDirectory(atPath: url.path)) == nil
    }

    private func expectRefusal<T>(
        _ expected: FolderCaptureSubmissionError,
        _ work: () async throws -> T
    ) async {
        do {
            _ = try await work()
            XCTFail("expected \(expected)")
        } catch {
            XCTAssertEqual(error as? FolderCaptureSubmissionError, expected)
        }
    }

    private func admit(
        _ entry: CaptureQueueEntry,
        captureID: String,
        queue: CaptureIntentQueue
    ) throws {
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

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-wp10-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
