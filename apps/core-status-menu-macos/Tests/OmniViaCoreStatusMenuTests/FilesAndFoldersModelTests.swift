import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS

final class FilesAndFoldersModelTests: XCTestCase {
    private let binding = CallerContextBinding(
        clientContextRef: "client-1",
        principalRef: "principal-1",
        contextRevision: "context-1"
    )

    private func file(
        id: String = "file-1",
        name: String = "Notes.pdf",
        state: FolderSubscriptionState = .active,
        lastCheckedAt: String? = "2026-10-15T09:00:00Z",
        lastObservedAt: String? = nil
    ) -> FileSubscription {
        FileSubscription(
            subscriptionID: id,
            accessRef: "access-\(id)",
            linkedDocumentID: "document-\(id)",
            requestedSourceIdentity: "identity-\(id)",
            displayName: name,
            contentKind: .pdf,
            bindingGeneration: 1,
            baselineItemID: "baseline-\(id)",
            targetRef: "target-1",
            workspaceRef: "workspace-1",
            targetBindingRevision: "binding-1",
            callerContextBinding: binding,
            configGeneration: 1,
            consentGeneration: 1,
            consentRecordedAt: "2026-10-14T00:00:00Z",
            state: state,
            currentSuccessfulCaptureID: nil,
            lastObservedDigest: nil,
            lastObservedByteLength: nil,
            lastObservedAt: lastObservedAt,
            lastQueuedItemID: nil,
            dirtyEpoch: 1,
            needsReconciliation: false,
            lastCheckedAt: lastCheckedAt
        )
    }

    private func folder(
        id: String = "folder-1",
        root: String = "root-1",
        name: String = "Contracts",
        state: FolderSubscriptionState = .active,
        reviewChangeCount: Int? = nil,
        members: [FolderSubscriptionMember] = []
    ) -> FolderSubscription {
        FolderSubscription(
            subscriptionID: id,
            rootRef: root,
            rootDisplayName: name,
            targetRef: "target-1",
            workspaceRef: "workspace-1",
            targetBindingRevision: "binding-1",
            callerContextBinding: binding,
            recursive: true,
            includeHidden: false,
            configGeneration: 1,
            consentGeneration: 1,
            consentRecordedAt: "2026-10-14T00:00:00Z",
            observationEpoch: "epoch-1",
            state: state,
            needsFullReconciliation: false,
            lastCompletedMetadataScanAt: "2026-10-15T08:00:00Z",
            pendingReviewSignature: nil,
            approvedReviewSignature: nil,
            reviewChangeCount: reviewChangeCount,
            reviewExamples: nil,
            members: members
        )
    }

    private func member(
        id: String,
        state: FolderMemberState = .present
    ) -> FolderSubscriptionMember {
        FolderSubscriptionMember(
            memberID: id,
            accessRef: "access-\(id)",
            linkedDocumentID: "document-\(id)",
            requestedSourceIdentity: "identity-\(id)",
            relativeComponents: ["\(id).pdf"],
            displayName: "\(id).pdf",
            contentKind: .pdf,
            membershipGeneration: 1,
            baselineItemID: "baseline-\(id)",
            currentSuccessfulCaptureID: nil,
            lastObservedDigest: nil,
            lastObservedByteLength: nil,
            lastObservedAt: nil,
            lastQueuedItemID: nil,
            dirtyEpoch: 1,
            state: state
        )
    }

    private func run(
        id: String = "run-1",
        root: String = "root-2",
        name: String = "Invoices",
        createdAt: String = "2026-10-15T07:00:00Z",
        state: FolderBulkRunState = .adding,
        subscriptionID: String? = nil,
        memberStates: [FolderBulkMemberState] = [.admitted, .planned]
    ) -> FolderBulkRun {
        FolderBulkRun(
            runID: id,
            manifestRevision: "manifest-1",
            submissionID: "submission-\(id)",
            rootRef: root,
            rootDisplayName: name,
            subscriptionID: subscriptionID,
            targetRef: "target-1",
            workspaceRef: "workspace-1",
            targetBindingRevision: "binding-1",
            callerContextBinding: binding,
            createdAt: createdAt,
            state: state,
            members: memberStates.enumerated().map { index, state in
                FolderBulkRunMember(
                    memberID: "member-\(index)",
                    accessRef: "access-\(id)-\(index)",
                    linkedDocumentID: "document-\(id)-\(index)",
                    requestedSourceIdentity: "identity-\(id)-\(index)",
                    relativeComponents: ["file-\(index).pdf"],
                    displayName: "file-\(index).pdf",
                    contentKind: .pdf,
                    bindingGeneration: 1,
                    itemID: "item-\(id)-\(index)",
                    payloadRef: "payload-\(id)-\(index)",
                    operationKey: "operation-\(id)-\(index)",
                    declaredByteLength: 16,
                    state: state,
                    failureCode: nil
                )
            }
        )
    }

    private func group(
        root: String = "root-2",
        state: NativeRootAccessState = .active,
        authority: NativeRootAuthority = .frozenMembersOnly,
        memberRefs: [String] = ["access-run-1-0", "access-run-1-1"]
    ) -> NativeRootAccessDescriptor {
        NativeRootAccessDescriptor(
            rootRef: root,
            generation: 1,
            state: state,
            ownerRefs: ["run-1"],
            authority: authority,
            memberRefs: memberRefs,
            membersFrozen: true,
            binding: nil
        )
    }

    private func model(
        files: [FileSubscription] = [],
        folders: [FolderSubscription] = [],
        retainedRoots: [RetainedRootGroup] = [],
        runs: [FolderBulkRun] = []
    ) -> FilesAndFoldersModel {
        FilesAndFoldersModel(
            workspaceName: "workspace-1",
            files: files,
            folders: folders,
            retainedRoots: retainedRoots,
            runs: runs
        )
    }

    // MARK: - Segmentation

    func testEachSegmentShowsOnlyItsOwnKindOfSource() {
        let subject = model(
            files: [file()],
            folders: [folder()],
            retainedRoots: [
                RetainedRootGroup(
                    rootRef: "root-2",
                    displayName: "Invoices",
                    memberCount: 2,
                    membersFrozen: true
                ),
            ],
            runs: [run()]
        )

        XCTAssertEqual(subject.items(segment: .files).map(\.identifier), ["file-1"])
        XCTAssertEqual(subject.items(segment: .folders).map(\.identifier), ["folder-1", "root-2"])
        XCTAssertEqual(subject.items(segment: .recentAdditions).map(\.identifier), ["run-1"])
    }

    func testFoldersSegmentSortsSubscriptionsAndRetainedRootsTogetherByName() {
        let subject = model(
            folders: [folder(id: "folder-1", name: "Zebra"), folder(id: "folder-2", root: "root-3", name: "apples")],
            retainedRoots: [
                RetainedRootGroup(rootRef: "root-9", displayName: "Mangoes", memberCount: 1, membersFrozen: true),
            ]
        )

        XCTAssertEqual(
            subject.items(segment: .folders).map(\.displayName),
            ["apples", "Mangoes", "Zebra"]
        )
    }

    func testRecentAdditionsShowsTheNewestRunFirst() {
        let subject = model(runs: [
            run(id: "run-old", createdAt: "2026-10-01T00:00:00Z"),
            run(id: "run-new", createdAt: "2026-10-15T00:00:00Z"),
        ])

        XCTAssertEqual(
            subject.items(segment: .recentAdditions).map(\.identifier),
            ["run-new", "run-old"]
        )
    }

    // MARK: - Search

    func testSearchMatchesDisplayNameCaseInsensitively() {
        let subject = model(files: [file(name: "Quarterly report.pdf"), file(id: "file-2", name: "Notes.pdf")])

        XCTAssertEqual(
            subject.items(segment: .files, search: "quarterly").map(\.displayName),
            ["Quarterly report.pdf"]
        )
    }

    func testSearchMatchesStableIdentifiersAndNothingElse() {
        let subject = model(
            folders: [folder(id: "folder-1", root: "root-aaa", name: "Contracts")],
            runs: [run(id: "run-bbb")]
        )

        XCTAssertEqual(subject.items(segment: .folders, search: "root-aaa").count, 1)
        XCTAssertEqual(subject.items(segment: .folders, search: "folder-1").count, 1)
        XCTAssertEqual(subject.items(segment: .recentAdditions, search: "run-bbb").count, 1)
        XCTAssertTrue(subject.items(segment: .folders, search: "root-zzz").isEmpty)
    }

    func testSearchIgnoresSurroundingWhitespace() {
        let subject = model(files: [file(name: "Notes.pdf")])

        XCTAssertEqual(subject.items(segment: .files, search: "  notes ").count, 1)
    }

    // MARK: - Status filters

    func testStatusFilterSortsEachStateIntoItsBucket() {
        let subject = model(folders: [
            folder(id: "folder-pending", name: "Pending", state: .waitingForCore),
            folder(id: "folder-active", name: "Active", state: .active),
            folder(id: "folder-paused", name: "Paused", state: .paused),
            folder(id: "folder-review", name: "Review", state: .reviewRequired),
            folder(id: "folder-access", name: "Access", state: .needsAccess),
            folder(id: "folder-stopped", name: "Stopped", state: .stopped),
        ])

        XCTAssertEqual(
            subject.items(segment: .folders, statusFilter: .activeOrPending).map(\.identifier).sorted(),
            ["folder-active", "folder-pending"]
        )
        XCTAssertEqual(
            subject.items(segment: .folders, statusFilter: .paused).map(\.identifier),
            ["folder-paused"]
        )
        XCTAssertEqual(
            subject.items(segment: .folders, statusFilter: .needsAttention).map(\.identifier).sorted(),
            ["folder-access", "folder-review"]
        )
        XCTAssertEqual(
            subject.items(segment: .folders, statusFilter: .stoppedOrComplete).map(\.identifier),
            ["folder-stopped"]
        )
        XCTAssertEqual(subject.items(segment: .folders, statusFilter: .all).count, 6)
    }

    func testRunStatesAndRetainedRootsLandInTheirBuckets() {
        let subject = model(
            retainedRoots: [
                RetainedRootGroup(rootRef: "root-2", displayName: "Invoices", memberCount: 2, membersFrozen: true),
            ],
            runs: [
                run(id: "run-adding", state: .adding),
                run(id: "run-paused", state: .paused),
                run(id: "run-attention", state: .needsAttention),
                run(id: "run-complete", state: .complete),
                run(id: "run-stopped", state: .stopped),
            ]
        )

        XCTAssertEqual(
            subject.items(segment: .recentAdditions, statusFilter: .stoppedOrComplete)
                .map(\.identifier).sorted(),
            ["run-complete", "run-stopped"]
        )
        XCTAssertEqual(
            subject.items(segment: .recentAdditions, statusFilter: .needsAttention).map(\.identifier),
            ["run-attention"]
        )
        // Added once and never updated is a finished source, not an active one.
        XCTAssertEqual(
            subject.items(segment: .folders, statusFilter: .stoppedOrComplete).map(\.identifier),
            ["root-2"]
        )
        XCTAssertTrue(subject.items(segment: .folders, statusFilter: .activeOrPending).isEmpty)
    }

    func testSearchAndStatusFilterApplyTogether() {
        let subject = model(files: [
            file(id: "file-1", name: "Notes.pdf", state: .paused),
            file(id: "file-2", name: "Notes archive.pdf", state: .active),
        ])

        XCTAssertEqual(
            subject.items(segment: .files, search: "notes", statusFilter: .paused).map(\.identifier),
            ["file-1"]
        )
    }

    func testEveryStatusFilterIsOfferedOnEverySegment() {
        for segment in FilesAndFoldersSegment.allCases {
            XCTAssertEqual(
                FilesAndFoldersStatusFilter.applicable(to: segment),
                FilesAndFoldersStatusFilter.allCases,
                "segment \(segment.title)"
            )
        }
    }

    // MARK: - Retained root derivation

    func testRetainedRootsAreNamedByTheRunThatEstablishedThem() {
        let roots = FilesAndFoldersModel.retainedRoots(
            groups: [group()],
            runs: [run()],
            folders: []
        )

        XCTAssertEqual(
            roots,
            [RetainedRootGroup(rootRef: "root-2", displayName: "Invoices", memberCount: 2, membersFrozen: true)]
        )
    }

    func testRetainedRootsExcludeWatchesDisconnectedRootsAndUnnamedRoots() {
        XCTAssertTrue(
            FilesAndFoldersModel.retainedRoots(
                groups: [group(authority: .watchAndMembers)],
                runs: [run()],
                folders: []
            ).isEmpty,
            "a root with standing watch authority is represented by its subscription"
        )
        XCTAssertTrue(
            FilesAndFoldersModel.retainedRoots(
                groups: [group(state: .disconnected)],
                runs: [run()],
                folders: []
            ).isEmpty,
            "a disconnected root retains no route, so nothing is retained"
        )
        XCTAssertTrue(
            FilesAndFoldersModel.retainedRoots(
                groups: [group()],
                runs: [run()],
                folders: [folder(root: "root-2")]
            ).isEmpty,
            "a watched root is already listed as a folder subscription"
        )
        XCTAssertTrue(
            FilesAndFoldersModel.retainedRoots(groups: [group()], runs: [], folders: []).isEmpty,
            "no durable run names this root, so no display name can be stated"
        )
    }

    // MARK: - Detail facts

    func testNoSelectionHasNoDetailFacts() {
        XCTAssertTrue(model().detail(for: nil).isEmpty)
    }

    func testFileDetailStatesDurableFactsAndDefersCaptureHistoryToCore() {
        let facts = model().detail(for: .file(file(lastObservedAt: nil)))
        let values = Dictionary(uniqueKeysWithValues: facts.map { ($0.label, $0.value) })

        XCTAssertEqual(values["File"], "Notes.pdf")
        XCTAssertEqual(values["Subscription"], "file-1")
        XCTAssertEqual(values["Source identity"], "identity-file-1")
        XCTAssertEqual(values["Destination workspace"], "workspace-1")
        XCTAssertEqual(values["Status"], "Watching")
        XCTAssertEqual(values["Last checked"], "2026-10-15T09:00:00Z")
        XCTAssertEqual(values["Last change seen"], "No change recorded yet")
        XCTAssertEqual(
            values["Capture history"],
            "Capture, extraction and profile history is available from Core after admission."
        )
        XCTAssertNil(values["Already added files"], "nothing is retained while the file is watched")
    }

    func testFileDetailSaysNothingWasCheckedWhenNothingWas() {
        let facts = model().detail(for: .file(file(lastCheckedAt: nil)))

        XCTAssertTrue(facts.contains(DetailFact(label: "Last checked", value: "Not checked yet")))
    }

    func testStoppedFileDetailStatesThatAlreadyAddedFilesStayLinked() {
        let facts = model().detail(for: .file(file(state: .stopped)))
        let values = Dictionary(uniqueKeysWithValues: facts.map { ($0.label, $0.value) })

        XCTAssertEqual(values["Updates"], "Stopped. This file is no longer kept updated.")
        XCTAssertEqual(
            values["Already added files"],
            "Still in Core and still linked, so they can be refreshed manually."
        )
    }

    func testFolderDetailReportsMembershipScanAndReviewCount() {
        let facts = model().detail(
            for: .folder(
                folder(
                    reviewChangeCount: 3,
                    members: [member(id: "a"), member(id: "b", state: .absent)]
                )
            )
        )
        let values = Dictionary(uniqueKeysWithValues: facts.map { ($0.label, $0.value) })

        XCTAssertEqual(values["Root"], "root-1")
        XCTAssertEqual(values["Scope"], "This folder and its subfolders")
        XCTAssertEqual(values["Files enrolled"], "2 (1 present)")
        XCTAssertEqual(values["Last metadata scan"], "2026-10-15T08:00:00Z")
        XCTAssertEqual(values["Unusual changes to review"], "3")
    }

    func testFolderDetailRecordsNoReviewCountWhenNoneWasRecorded() {
        let facts = model().detail(for: .folder(folder()))

        XCTAssertTrue(
            facts.contains(DetailFact(label: "Unusual changes to review", value: "None recorded"))
        )
    }

    func testRetainedRootDetailSaysAddedOnceAndExplainsTheDisabledActions() {
        let facts = model().detail(
            for: .retainedRoot(
                RetainedRootGroup(
                    rootRef: "root-2",
                    displayName: "Invoices",
                    memberCount: 2,
                    membersFrozen: true
                )
            )
        )
        let values = Dictionary(uniqueKeysWithValues: facts.map { ($0.label, $0.value) })

        XCTAssertEqual(values["Updates"], "Added once. This folder is not kept updated.")
        XCTAssertEqual(values["Files in this group"], "2 (membership frozen)")
        XCTAssertEqual(
            values["Already added files"],
            "Still in Core and still linked, so they can be refreshed manually."
        )
        XCTAssertEqual(
            values["Actions"],
            "Updating actions do not apply: nothing watches this folder."
        )
    }

    func testRunDetailReportsPerFileTotalsAndRefusesAWholeFolderResult() {
        let facts = model().detail(
            for: .bulkRun(
                run(
                    state: .needsAttention,
                    memberStates: [.admitted, .published, .planned, .stopped, .needsAttention]
                )
            )
        )
        let values = Dictionary(uniqueKeysWithValues: facts.map { ($0.label, $0.value) })

        XCTAssertEqual(values["Files in this run"], "5")
        XCTAssertEqual(
            values["Progress"],
            "1 added, 1 in progress, 1 waiting, 1 stopped, 1 need attention"
        )
        XCTAssertEqual(
            values["Whole-folder result"],
            "Not recorded. Each file carries its own outcome."
        )
        XCTAssertEqual(values["Updates"], "Added once. This run establishes no watch.")
    }

    func testFinishedRunDetailStatesThatAddedFilesStayLinked() {
        let facts = model().detail(
            for: .bulkRun(run(state: .complete, subscriptionID: "folder-1", memberStates: [.admitted]))
        )
        let values = Dictionary(uniqueKeysWithValues: facts.map { ($0.label, $0.value) })

        XCTAssertEqual(
            values["Already added files"],
            "Still in Core and still linked, so they can be refreshed manually."
        )
        XCTAssertEqual(
            values["Updates"],
            "This folder is also kept updated as a folder subscription."
        )
    }

    func testRunActivityTextNeverClaimsAWholeFolderSuccess() {
        let item = FilesAndFoldersItem.bulkRun(
            run(state: .complete, memberStates: [.admitted, .stopped])
        )

        XCTAssertEqual(
            item.activityText,
            "1 added · 0 in progress · 0 waiting · 1 stopped · 0 needs attention"
                + " · links retained for manual refresh"
        )
    }

    // MARK: - Actions

    func testActionsStayDisabledWithNoSelection() {
        let actions = FilesAndFoldersModel.actions(for: nil)

        XCTAssertFalse(actions.pauseEnabled)
        XCTAssertFalse(actions.stopEnabled)
        XCTAssertFalse(actions.checkEnabled)
        XCTAssertEqual(actions.pauseTitle, "Pause")
        XCTAssertEqual(actions.stopTitle, "Stop updating")
    }

    func testRetainedRootOffersNoUpdatingActions() {
        let actions = FilesAndFoldersModel.actions(
            for: .retainedRoot(
                RetainedRootGroup(rootRef: "root-2", displayName: "Invoices", memberCount: 2, membersFrozen: true)
            )
        )

        XCTAssertFalse(actions.pauseEnabled)
        XCTAssertFalse(actions.stopEnabled)
        XCTAssertFalse(actions.checkEnabled)
    }

    func testWatchedFolderOffersPauseStopAndCheck() {
        let actions = FilesAndFoldersModel.actions(for: .folder(folder()))

        XCTAssertEqual(actions.pauseTitle, "Pause")
        XCTAssertTrue(actions.pauseEnabled)
        XCTAssertEqual(actions.stopTitle, "Stop updating")
        XCTAssertTrue(actions.stopEnabled)
        XCTAssertTrue(actions.checkEnabled)
    }

    func testFolderInReviewContinuesChangesRatherThanResuming() {
        let actions = FilesAndFoldersModel.actions(for: .folder(folder(state: .reviewRequired)))

        XCTAssertEqual(actions.pauseTitle, "Continue changes")
        XCTAssertTrue(actions.pauseEnabled)
        XCTAssertFalse(actions.checkEnabled, "a folder awaiting review is not checked on demand")
    }

    func testPausedAndStoppedSubscriptionsScopeTheirOwnActions() {
        let paused = FilesAndFoldersModel.actions(for: .file(file(state: .paused)))
        XCTAssertEqual(paused.pauseTitle, "Resume")
        XCTAssertTrue(paused.stopEnabled)
        XCTAssertFalse(paused.checkEnabled)

        let stopped = FilesAndFoldersModel.actions(for: .file(file(state: .stopped)))
        XCTAssertFalse(stopped.pauseEnabled)
        XCTAssertFalse(stopped.stopEnabled)
        XCTAssertFalse(stopped.checkEnabled)
    }

    func testRunWithUnavailableFilesRetriesRatherThanPauses() {
        let actions = FilesAndFoldersModel.actions(
            for: .bulkRun(run(state: .needsAttention, memberStates: [.needsAttention, .planned]))
        )

        XCTAssertEqual(actions.pauseTitle, "Retry unavailable files")
        XCTAssertTrue(actions.pauseEnabled)
        XCTAssertEqual(actions.stopTitle, "Stop adding remaining files")
        XCTAssertTrue(actions.stopEnabled, "one member has not started")
        XCTAssertFalse(actions.checkEnabled)
    }

    func testFinishedRunStopsNothingAndChecksNothing() {
        let actions = FilesAndFoldersModel.actions(
            for: .bulkRun(run(state: .complete, memberStates: [.admitted]))
        )

        XCTAssertFalse(actions.pauseEnabled)
        XCTAssertFalse(actions.stopEnabled, "no member is still waiting to be added")
        XCTAssertFalse(actions.checkEnabled)
    }

    func testPausedRunResumes() {
        let actions = FilesAndFoldersModel.actions(
            for: .bulkRun(run(state: .paused, memberStates: [.planned]))
        )

        XCTAssertEqual(actions.pauseTitle, "Resume")
        XCTAssertTrue(actions.pauseEnabled)
    }

    // MARK: - Summary

    func testSummaryNamesTheConfiguredWorkspace() {
        let subject = model(files: [file(), file(id: "file-2", name: "B.pdf", state: .stopped)])
        let items = subject.items(segment: .files)

        XCTAssertEqual(
            subject.summary(items: items, segment: .files),
            "1 active or pending · 2 listed · workspace workspace-1"
        )
    }

    func testEmptySegmentsSayWhatIsEmpty() {
        let subject = model()

        XCTAssertEqual(subject.summary(items: [], segment: .files), "No files are listed.")
        XCTAssertEqual(subject.summary(items: [], segment: .folders), "No folders are listed.")
        XCTAssertEqual(
            subject.summary(items: [], segment: .recentAdditions),
            "No folder additions are recorded."
        )
    }
}
