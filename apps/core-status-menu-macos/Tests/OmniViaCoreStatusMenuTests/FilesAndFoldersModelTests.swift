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
        runs: [FolderBulkRun] = [],
        activity: [CaptureActivityRecord] = [],
        activityAvailability: CaptureActivityAvailability = .current
    ) -> FilesAndFoldersModel {
        FilesAndFoldersModel(
            workspaceName: "workspace-1",
            files: files,
            folders: folders,
            retainedRoots: retainedRoots,
            runs: runs,
            activity: activity,
            activityAvailability: activityAvailability
        )
    }

    /// One activity record, as the store writes them.
    private func record(
        item: String,
        submission: String = "submission-1",
        name: String? = nil,
        run: String? = nil,
        member: String? = nil,
        sequence: UInt64,
        fact: CaptureActivityFact,
        resolvedAt: String? = "2026-10-15T10:00:00Z",
        failureCode: String? = nil
    ) -> CaptureActivityRecord {
        CaptureActivityRecord(
            itemID: item,
            submissionID: submission,
            operationKey: "operation-\(item)",
            displayName: name ?? "\(item).pdf",
            runID: run,
            manifestRevision: run == nil ? nil : "manifest-1",
            memberID: run == nil ? nil : (member ?? "member-\(item)"),
            sequence: sequence,
            firstObservedAt: "2026-10-15T09:00:00Z",
            fact: fact,
            resolvedAt: fact.isResolved ? resolvedAt : nil,
            attemptGeneration: fact == .checking ? 1 : 0,
            attemptOperationKey: fact == .checking ? "capop-1" : nil,
            failureCode: failureCode
        )
    }

    /// The activity of a whole run, one record per member, mirroring the member
    /// states the fixture run was built with.
    private func activity(of run: FolderBulkRun) -> [CaptureActivityRecord] {
        run.members.enumerated().map { index, member in
            record(
                item: member.itemID,
                submission: run.submissionID,
                name: member.displayName,
                run: run.runID,
                member: member.memberID,
                sequence: UInt64(index + 1),
                fact: {
                    switch member.state {
                    case .planned: .planned
                    case .published: .queued
                    case .admitted: .processing
                    case .needsAttention: .failed
                    case .stopped: .stopped
                    }
                }()
            )
        }
    }

    private func group(of run: FolderBulkRun) -> CaptureActivityGroup {
        CaptureActivityGroup(
            identifier: run.runID,
            displayName: run.rootDisplayName,
            run: run,
            items: activity(of: run)
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
            runs: [run()],
            activity: activity(of: run())
        )

        XCTAssertEqual(subject.items(segment: .files).map(\.identifier), ["file-1"])
        XCTAssertEqual(subject.items(segment: .folders).map(\.identifier), ["folder-1", "root-2"])
        XCTAssertEqual(subject.items(segment: .recentAdditions).map(\.identifier), ["run-1"])
    }

    func testAnAdditionWhoseActivityWasClearedIsNoLongerListed() {
        let subject = model(runs: [run()], activity: [])

        XCTAssertTrue(
            subject.items(segment: .recentAdditions).isEmpty,
            "the frozen manifest still exists, but its cleared receipts are not resurrected"
        )
    }

    func testOrdinarySubmissionsAndFolderRunsAreSeparateGroups() {
        let frozen: FolderBulkRun = run()
        let subject = model(
            runs: [frozen],
            activity: activity(of: frozen) + [
                record(item: "item-manual", submission: "submission-manual", sequence: 9, fact: .queued),
            ]
        )

        XCTAssertEqual(
            subject.items(segment: .recentAdditions).map(\.identifier),
            ["submission-manual", "run-1"]
        )
        XCTAssertEqual(
            subject.items(segment: .recentAdditions).map(\.displayName),
            ["item-manual.pdf", "Invoices"]
        )
    }

    func testAnOrdinarySubmissionOfSeveralItemsIsNamedByItsCount() {
        let subject = model(activity: [
            record(item: "item-a", sequence: 1, fact: .queued),
            record(item: "item-b", sequence: 2, fact: .queued),
        ])

        XCTAssertEqual(
            subject.items(segment: .recentAdditions).map(\.displayName),
            ["2 items added together"]
        )
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

    func testRecentAdditionsShowsTheNewestAdditionFirst() {
        let subject = model(activity: [
            record(item: "item-old", submission: "submission-old", sequence: 1, fact: .processing),
            record(item: "item-new", submission: "submission-new", sequence: 2, fact: .queued),
        ])

        XCTAssertEqual(
            subject.items(segment: .recentAdditions).map(\.identifier),
            ["submission-new", "submission-old"]
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
        let frozen: FolderBulkRun = run(id: "run-bbb")
        let subject = model(
            folders: [folder(id: "folder-1", root: "root-aaa", name: "Contracts")],
            runs: [frozen],
            activity: activity(of: frozen)
        )

        XCTAssertEqual(subject.items(segment: .folders, search: "root-aaa").count, 1)
        XCTAssertEqual(subject.items(segment: .folders, search: "folder-1").count, 1)
        XCTAssertEqual(subject.items(segment: .recentAdditions, search: "run-bbb").count, 1)
        XCTAssertEqual(
            subject.items(segment: .recentAdditions, search: "item-run-bbb-0").count,
            1,
            "an addition is searchable by the frozen item identity it is recovered by"
        )
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

    func testRetainedRootsLandInTheirBucket() {
        let subject = model(
            retainedRoots: [
                RetainedRootGroup(rootRef: "root-2", displayName: "Invoices", memberCount: 2, membersFrozen: true),
            ]
        )

        // Added once and never updated is a finished source, not an active one.
        XCTAssertEqual(
            subject.items(segment: .folders, statusFilter: .stoppedOrComplete).map(\.identifier),
            ["root-2"]
        )
        XCTAssertTrue(subject.items(segment: .folders, statusFilter: .activeOrPending).isEmpty)
    }

    func testAdditionsAreFilteredByWhatTheirOwnItemsProve() {
        let subject = model(activity: [
            record(item: "item-queued", submission: "submission-queued", sequence: 1, fact: .queued),
            record(item: "item-sending", submission: "submission-sending", sequence: 2, fact: .sending),
            record(item: "item-checking", submission: "submission-checking", sequence: 3, fact: .checking),
            record(item: "item-core", submission: "submission-core", sequence: 4, fact: .processing),
            record(item: "item-failed", submission: "submission-failed", sequence: 5, fact: .failed),
            record(item: "item-stopped", submission: "submission-stopped", sequence: 6, fact: .stopped),
        ])

        XCTAssertEqual(subject.items(segment: .recentAdditions, statusFilter: .all).count, 6)
        // Pending is an addition that is not finished. An item Core has only
        // accepted for processing is one: acceptance is not a searchable
        // publication or a terminal receipt, and this Mac will never see one.
        XCTAssertEqual(
            subject.items(segment: .recentAdditions, statusFilter: .pending)
                .map(\.identifier).sorted(),
            ["submission-core", "submission-queued", "submission-sending"]
        )
        XCTAssertEqual(
            subject.items(segment: .recentAdditions, statusFilter: .all)
                .first(where: { $0.identifier == "submission-core" })?.statusLabel,
            "Processing in Core"
        )
        XCTAssertEqual(
            subject.items(segment: .recentAdditions, statusFilter: .needsAttention)
                .map(\.identifier).sorted(),
            ["submission-checking", "submission-failed"],
            "an unknown outcome needs a decision as much as a known failure does"
        )
    }

    func testAMixedAdditionIsFiledUnderItsMostDemandingItem() {
        let subject = model(activity: [
            record(item: "item-0", sequence: 1, fact: .processing),
            record(item: "item-1", sequence: 2, fact: .queued),
            record(item: "item-2", sequence: 3, fact: .failed),
        ])

        XCTAssertEqual(
            subject.items(segment: .recentAdditions, statusFilter: .needsAttention).count,
            1
        )
        XCTAssertTrue(
            subject.items(segment: .recentAdditions, statusFilter: .pending).isEmpty,
            "a group that needs attention is not also offered as ordinary pending work"
        )
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

    func testEachSegmentOffersOnlyTheFiltersItsOwnStatesReach() {
        for segment in [FilesAndFoldersSegment.files, .folders] {
            XCTAssertEqual(
                FilesAndFoldersStatusFilter.applicable(to: segment),
                [.all, .activeOrPending, .paused, .needsAttention, .stoppedOrComplete],
                "segment \(segment.title)"
            )
        }
        XCTAssertEqual(
            FilesAndFoldersStatusFilter.applicable(to: .recentAdditions),
            [.all, .pending, .needsAttention]
        )
        XCTAssertEqual(
            FilesAndFoldersStatusFilter.applicable(to: .recentAdditions).map(\.title),
            ["All", "Pending", "Needs attention"]
        )
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

    func testRunDetailReportsPerFileFactsAndRefusesAWholeAdditionResult() {
        let frozen: FolderBulkRun = run(
            state: .needsAttention,
            memberStates: [.admitted, .published, .planned, .stopped, .needsAttention]
        )
        let facts = model().detail(for: .activity(group(of: frozen)))
        let values = Dictionary(uniqueKeysWithValues: facts.map { ($0.label, $0.value) })

        XCTAssertEqual(values["Folder run"], "run-1")
        XCTAssertEqual(values["Manifest revision"], "manifest-1")
        XCTAssertEqual(values["Files in this addition"], "5")
        XCTAssertEqual(
            values["Progress"],
            "1 accepted by Core for processing · 1 queued on this Mac · "
                + "1 waiting to be prepared · 1 need attention · 1 stopped"
        )
        XCTAssertEqual(
            values["Whole-addition result"],
            "Not recorded. Each file carries its own outcome."
        )
        XCTAssertEqual(values["Updates"], "Added once. This run establishes no watch.")
    }

    func testEveryItemCarriesItsOwnOutcomeAndRecoveryIdentityInTheDetail() {
        let subject = model(activity: [
            record(item: "item-0", name: "Invoice.pdf", sequence: 1, fact: .processing),
            record(item: "item-1", name: "Receipt.pdf", sequence: 2, fact: .failed, failureCode: "download_failed"),
        ])
        let facts = subject.detail(for: subject.items(segment: .recentAdditions).first)

        XCTAssertTrue(
            facts.contains(where: {
                $0.label == "Invoice.pdf"
                    && $0.value.hasPrefix("Processing in Core · item item-0")
            }),
            "\(facts)"
        )
        XCTAssertTrue(
            facts.contains(where: {
                $0.label == "Receipt.pdf"
                    && $0.value.contains("Needs attention · item item-1 · reason download_failed")
            }),
            "\(facts)"
        )
        XCTAssertFalse(
            facts.contains(where: { $0.value.contains("Added to Core") }),
            "this side proves no retrieval-ready capture"
        )
    }

    func testActivityDetailStatesTheRetentionBoundAndWhatClearingLeavesAlone() {
        let subject = model(activity: [record(item: "item-0", sequence: 1, fact: .processing)])
        let facts = subject.detail(for: subject.items(segment: .recentAdditions).first)
        let values = Dictionary(uniqueKeysWithValues: facts.map { ($0.label, $0.value) })

        XCTAssertEqual(
            values["Activity retention"],
            "Completed activity is kept for up to 30 days or 100 items. Clearing it removes "
                + "local receipts only: queued work and its recovery keys, original links, running "
                + "watches, folder run manifests and anything already in Core are untouched."
        )
        XCTAssertEqual(
            values["Capture history"],
            "Capture, extraction and profile history is available from Core after admission."
        )
    }

    func testFinishedRunDetailStatesThatAddedFilesStayLinked() {
        let frozen: FolderBulkRun = run(state: .complete, subscriptionID: "folder-1", memberStates: [.admitted])
        let facts = model().detail(for: .activity(group(of: frozen)))
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

    func testRecoveryDetailIsOfferedOnlyForAnUnknownOutcome() {
        let unknown = model(activity: [record(item: "item-0", sequence: 1, fact: .checking)])
        let resolved = model(activity: [record(item: "item-0", sequence: 1, fact: .processing)])

        let facts = unknown.recoveryDetail(for: unknown.items(segment: .recentAdditions).first)
        XCTAssertTrue(
            facts.contains(where: {
                $0.label == "Recovery identity — item-0.pdf"
                    && $0.value == "item item-0 · operation operation-item-0 · attempt 1"
                        + " · attempt key capop-1"
            }),
            "\(facts)"
        )
        XCTAssertTrue(
            facts.contains(
                DetailFact(
                    label: "Reconciliation",
                    value: "This addition is reconciled under its existing key. No replacement key, "
                        + "destination change or removal is offered while its outcome is unknown."
                )
            )
        )
        XCTAssertTrue(
            resolved.recoveryDetail(for: resolved.items(segment: .recentAdditions).first).isEmpty
        )
        XCTAssertTrue(unknown.recoveryDetail(for: .file(file())).isEmpty)
    }

    func testActivityTextNeverClaimsAWholeAdditionSuccess() {
        let frozen: FolderBulkRun = run(state: .complete, memberStates: [.admitted, .stopped])
        let item = FilesAndFoldersItem.activity(group(of: frozen))

        XCTAssertEqual(
            item.activityText,
            "1 accepted by Core for processing · 1 stopped"
        )
        XCTAssertEqual(item.countText, "1/2")
        XCTAssertEqual(item.statusLabel, "Processing in Core")
    }

    func testAQueuedAdditionIsNeverLabelledAdded() {
        let planned = FilesAndFoldersItem.activity(
            CaptureActivityGroup(
                identifier: "run-1",
                displayName: "Invoices",
                run: run(memberStates: [.planned]),
                items: [record(item: "item-0", run: "run-1", sequence: 1, fact: .planned)]
            )
        )
        let queued = FilesAndFoldersItem.activity(
            CaptureActivityGroup(
                identifier: "submission-1",
                displayName: "Invoice.pdf",
                items: [record(item: "item-0", sequence: 1, fact: .queued)]
            )
        )

        XCTAssertEqual(planned.statusLabel, "Folder addition queued")
        XCTAssertEqual(queued.statusLabel, "Queued on this Mac")
        XCTAssertEqual(planned.countText, "0/1", "nothing is accepted by Core yet")
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
            for: .activity(
                group(of: run(state: .needsAttention, memberStates: [.needsAttention, .planned]))
            )
        )

        XCTAssertEqual(actions.pauseTitle, "Retry unavailable files")
        XCTAssertTrue(actions.pauseEnabled)
        XCTAssertEqual(actions.stopTitle, "Stop adding remaining files")
        XCTAssertTrue(actions.stopEnabled, "one member has not started")
        XCTAssertFalse(actions.checkEnabled)
        XCTAssertFalse(actions.revealEnabled, "no outcome here is unknown")
    }

    func testFinishedRunStopsNothingAndChecksNothing() {
        let actions = FilesAndFoldersModel.actions(
            for: .activity(group(of: run(state: .complete, memberStates: [.admitted])))
        )

        XCTAssertFalse(actions.pauseEnabled)
        XCTAssertFalse(actions.stopEnabled, "no member is still waiting to be added")
        XCTAssertFalse(actions.checkEnabled)
        XCTAssertFalse(actions.revealEnabled)
    }

    func testAddingRunAdvancesOnDemandAndStopsOnlyUnstartedMembers() {
        let actions = FilesAndFoldersModel.actions(
            for: .activity(group(of: run(state: .adding, memberStates: [.published, .planned])))
        )

        XCTAssertEqual(actions.pauseTitle, "Pause")
        XCTAssertTrue(actions.pauseEnabled)
        XCTAssertTrue(actions.checkEnabled)
        XCTAssertTrue(actions.stopEnabled)

        let nothingUnstarted = FilesAndFoldersModel.actions(
            for: .activity(group(of: run(state: .adding, memberStates: [.published])))
        )
        XCTAssertFalse(
            nothingUnstarted.stopEnabled,
            "a published member is already under way, so there is nothing safe to stop"
        )
    }

    func testPausedRunResumes() {
        let actions = FilesAndFoldersModel.actions(
            for: .activity(group(of: run(state: .paused, memberStates: [.planned])))
        )

        XCTAssertEqual(actions.pauseTitle, "Resume")
        XCTAssertTrue(actions.pauseEnabled)
    }

    func testAnUnknownOutcomeOffersOnlyRevealAndNoRunActionWithoutARun() {
        let withoutRun = FilesAndFoldersModel.actions(
            for: .activity(
                CaptureActivityGroup(
                    identifier: "submission-1",
                    displayName: "Invoice.pdf",
                    items: [
                        record(item: "item-0", sequence: 1, fact: .checking),
                        record(item: "item-1", sequence: 2, fact: .failed),
                    ]
                )
            )
        )

        XCTAssertTrue(withoutRun.revealEnabled)
        XCTAssertFalse(
            withoutRun.pauseEnabled,
            "no authority can return an ordinary failed item to pending work"
        )
        XCTAssertFalse(withoutRun.stopEnabled, "the queue exposes no cancellation")
        XCTAssertFalse(withoutRun.checkEnabled)
    }

    func testARunMemberWhoseOutcomeIsUnknownStillOffersItsRunActions() {
        let frozen: FolderBulkRun = run(state: .adding, memberStates: [.published, .planned])
        let actions = FilesAndFoldersModel.actions(
            for: .activity(
                CaptureActivityGroup(
                    identifier: frozen.runID,
                    displayName: frozen.rootDisplayName,
                    run: frozen,
                    items: [
                        record(item: "item-run-1-0", run: frozen.runID, sequence: 1, fact: .checking),
                        record(item: "item-run-1-1", run: frozen.runID, sequence: 2, fact: .planned),
                    ]
                )
            )
        )

        XCTAssertTrue(actions.revealEnabled)
        XCTAssertTrue(actions.checkEnabled, "the run can still advance its unstarted members")
        XCTAssertTrue(actions.stopEnabled, "one member has not started")
    }

    func testClearableActivityCountsOnlyTerminalItems() {
        let subject = model(activity: [
            record(item: "item-planned", sequence: 1, fact: .planned),
            record(item: "item-queued", sequence: 2, fact: .queued),
            record(item: "item-sending", sequence: 3, fact: .sending),
            record(item: "item-checking", sequence: 4, fact: .checking),
            record(item: "item-core", sequence: 5, fact: .processing),
            record(item: "item-failed", sequence: 6, fact: .failed),
            record(item: "item-stopped", sequence: 7, fact: .stopped),
        ])

        XCTAssertEqual(
            subject.clearableActivityCount,
            2,
            "the failed ordinary item and the stopped one; an accepted item is unfinished work"
        )
        XCTAssertTrue(subject.canMutateActivity)
    }

    func testAnAcceptedItemIsUnfinishedActivityThatNothingLocalClears() throws {
        let subject = model(activity: [record(item: "item-core", sequence: 1, fact: .processing)])
        let item = try XCTUnwrap(subject.items(segment: .recentAdditions).first)

        XCTAssertEqual(item.statusFilter, .pending)
        XCTAssertEqual(item.statusLabel, "Processing in Core")
        XCTAssertEqual(subject.clearableActivityCount, 0)
        XCTAssertEqual(
            subject.summary(items: subject.items(segment: .recentAdditions), segment: .recentAdditions),
            "1 pending · 0 needs attention · 1 listed · workspace workspace-1"
        )
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
            "No recent additions are recorded."
        )
    }

    // MARK: - Local activity availability

    func testUnavailableActivityIsIdentifiedAsStaleInTheSummary() {
        let records = [
            record(item: "item-0", submission: "submission-0", sequence: 1, fact: .queued),
            record(item: "item-1", submission: "submission-1", sequence: 2, fact: .failed),
        ]
        let subject = model(activity: records, activityAvailability: .unavailable)
        let items = subject.items(segment: .recentAdditions)

        XCTAssertEqual(
            subject.summary(items: items, segment: .recentAdditions),
            "Local activity unavailable — last read, may be out of date · 1 pending · "
                + "1 needs attention · 2 listed · workspace workspace-1"
        )
        XCTAssertEqual(
            model(activityAvailability: .unavailable)
                .summary(items: [], segment: .recentAdditions),
            "Local activity is unavailable. Nothing recorded here could be read."
        )
        XCTAssertEqual(
            model(activity: records).summary(items: items, segment: .recentAdditions),
            "1 pending · 1 needs attention · 2 listed · workspace workspace-1"
        )
    }

    func testUnavailableActivityOffersNoMutationOrRecoveryAction() {
        let frozen: FolderBulkRun = run(state: .needsAttention, memberStates: [.needsAttention, .planned])
        var items = activity(of: frozen)
        items[0] = record(
            item: items[0].itemID,
            submission: frozen.submissionID,
            run: frozen.runID,
            member: items[0].memberID,
            sequence: 1,
            fact: .checking
        )
        let subject = model(
            runs: [frozen],
            activity: items,
            activityAvailability: .unavailable
        )
        let selected = subject.items(segment: .recentAdditions).first
        let actions = FilesAndFoldersModel.actions(
            for: selected,
            activity: subject.activityAvailability
        )

        XCTAssertFalse(subject.canMutateActivity)
        XCTAssertFalse(actions.pauseEnabled, "a retry would act on facts this surface cannot vouch for")
        XCTAssertFalse(actions.stopEnabled)
        XCTAssertFalse(actions.checkEnabled)
        XCTAssertFalse(actions.revealEnabled)
        XCTAssertTrue(
            subject.recoveryDetail(for: selected).isEmpty,
            "a recovery identity is a current-facts decision"
        )
        // The same selection with current facts does offer them, so the
        // difference is availability and nothing else.
        let current = model(runs: [frozen], activity: items)
        let offered = FilesAndFoldersModel.actions(
            for: current.items(segment: .recentAdditions).first,
            activity: current.activityAvailability
        )
        XCTAssertTrue(offered.pauseEnabled)
        XCTAssertTrue(offered.stopEnabled)
        XCTAssertTrue(offered.revealEnabled)
        XCTAssertFalse(current.recoveryDetail(for: current.items(segment: .recentAdditions).first).isEmpty)
    }

    func testUnavailableActivityStillOffersFileAndFolderActionsFromTheirOwnStores() {
        let subject = model(folders: [folder()], activityAvailability: .unavailable)
        let actions = FilesAndFoldersModel.actions(
            for: subject.items(segment: .folders).first,
            activity: subject.activityAvailability
        )

        XCTAssertTrue(actions.pauseEnabled)
        XCTAssertTrue(actions.stopEnabled)
        XCTAssertTrue(actions.checkEnabled)
    }

    func testUnavailableActivityDetailSaysNothingWasSavedAndNothingElseChanged() {
        let subject = model(
            activity: [record(item: "item-0", sequence: 1, fact: .processing)],
            activityAvailability: .unavailable
        )
        let facts = subject.detail(for: subject.items(segment: .recentAdditions).first)
        let values = Dictionary(uniqueKeysWithValues: facts.map { ($0.label, $0.value) })

        XCTAssertEqual(
            values["Local activity"],
            "Unavailable. These are the last records this Mac read and may be out of date; "
                + "nothing observed since was saved locally. Clearing, retrying, stopping, checking "
                + "and recovery details are unavailable until it can be read again. Queued work and "
                + "its recovery keys, original links, running watches, folder run manifests and "
                + "everything already in Core are unchanged."
        )
        XCTAssertNil(
            model(activity: [record(item: "item-0", sequence: 1, fact: .processing)])
                .detail(for: subject.items(segment: .recentAdditions).first)
                .first(where: { $0.label == "Local activity" }),
            "nothing is said about availability while the record is current"
        )
    }

    func testRecentAdditionsSummaryCountsPendingAndAttentionSeparately() {
        let subject = model(activity: [
            record(item: "item-0", submission: "submission-0", sequence: 1, fact: .queued),
            record(item: "item-1", submission: "submission-1", sequence: 2, fact: .failed),
            record(item: "item-2", submission: "submission-2", sequence: 3, fact: .stopped),
        ])
        let items = subject.items(segment: .recentAdditions)

        XCTAssertEqual(
            subject.summary(items: items, segment: .recentAdditions),
            "1 pending · 1 needs attention · 3 listed · workspace workspace-1"
        )
    }
}
