@testable import DocumentIntakeMacOS
import Foundation
import XCTest

/// The reviewed scope policy is a pure decision over native facts, so every
/// exclusion branch — including the ones real filesystem behaviour cannot make
/// deterministic, such as a nested mount — is proven directly here. The
/// traversal that establishes these facts from resource values is covered by
/// `FolderOneOffIntakeTests`.
final class FolderScopePolicyTests: XCTestCase {
    private func decide(
        _ facts: FolderEntryFacts,
        recursive: Bool = true,
        includeHidden: Bool = false,
        limits: FolderScopeLimits = .standard
    ) -> FolderScopeDecision {
        FolderScopePolicy.decide(
            facts,
            recursive: recursive,
            includeHidden: includeHidden,
            limits: limits
        )
    }

    private func file(
        _ name: String,
        depth: Int = 1,
        bytes: UInt64? = 128
    ) -> FolderEntryFacts {
        FolderEntryFacts(
            relativeComponents: Array(repeating: "d", count: depth - 1) + [name],
            isRegularFile: true,
            byteLength: bytes
        )
    }

    private func directory(_ name: String, depth: Int = 1) -> FolderEntryFacts {
        FolderEntryFacts(
            relativeComponents: Array(repeating: "d", count: depth - 1) + [name],
            isDirectory: true
        )
    }

    func testTheStandardLimitsAreTheReviewedDesignBounds() {
        XCTAssertEqual(FolderScopeLimits.standard.maximumEligibleFiles, 10_000)
        XCTAssertEqual(FolderScopeLimits.standard.maximumExaminedEntries, 100_000)
        XCTAssertEqual(FolderScopeLimits.standard.maximumDepth, 32)
        XCTAssertEqual(FolderCaptureSubmission.maximumMembers, 10_000)
    }

    func testAnApprovedSupportedFileIsEligibleWithItsExactLength() {
        XCTAssertEqual(
            decide(file("brief.pdf", bytes: 4096)),
            .eligible(kind: .pdf, byteLength: 4096)
        )
        XCTAssertEqual(decide(directory("nested")), .descend)
    }

    func testCoreOwnedStoresAreExcludedBeforeEveryOtherRule() {
        // Not a toggle: a controlled store is refused even when it would
        // otherwise pass every visible policy choice.
        var facts = file("notes.md")
        facts = FolderEntryFacts(
            relativeComponents: facts.relativeComponents,
            isRegularFile: true,
            byteLength: 10,
            isControlledStore: true
        )
        XCTAssertEqual(decide(facts, includeHidden: true), .skip(.controlledStore))
    }

    func testSymbolicLinksAndFinderAliasesAreNeverFollowed() {
        XCTAssertEqual(
            decide(
                FolderEntryFacts(
                    relativeComponents: ["escape"],
                    isDirectory: true,
                    isSymbolicLink: true
                ),
                includeHidden: true
            ),
            .skip(.link)
        )
        XCTAssertEqual(
            decide(
                FolderEntryFacts(
                    relativeComponents: ["shortcut.pdf"],
                    isRegularFile: true,
                    isAliasFile: true,
                    byteLength: 64
                )
            ),
            .skip(.link)
        )
    }

    func testPackagesAndBundlesAreNotDescendedIntoOrAccepted() {
        XCTAssertEqual(
            decide(
                FolderEntryFacts(
                    relativeComponents: ["Report.rtfd"],
                    isDirectory: true,
                    isPackage: true
                )
            ),
            .skip(.package)
        )
    }

    func testANestedMountIsNotCrossed() {
        XCTAssertEqual(
            decide(
                FolderEntryFacts(
                    relativeComponents: ["share"],
                    isDirectory: true,
                    onRootVolume: false
                )
            ),
            .skip(.otherVolume)
        )
        // Nor is a file sitting directly on a mount beneath the root.
        XCTAssertEqual(
            decide(
                FolderEntryFacts(
                    relativeComponents: ["share", "remote.pdf"],
                    isRegularFile: true,
                    onRootVolume: false,
                    byteLength: 32
                )
            ),
            .skip(.otherVolume)
        )
    }

    func testHiddenAndDotPrefixedEntriesAreExcludedUnlessReviewedIn() {
        XCTAssertEqual(decide(file(".secret.txt")), .skip(.hidden))
        XCTAssertEqual(
            decide(
                FolderEntryFacts(
                    relativeComponents: ["flagged.txt"],
                    isRegularFile: true,
                    isHidden: true,
                    byteLength: 8
                )
            ),
            .skip(.hidden)
        )
        XCTAssertEqual(
            decide(file(".secret.txt"), includeHidden: true),
            .eligible(kind: .text, byteLength: 128)
        )
    }

    func testTheTemporaryPatternListIsExactAndFinite() {
        for name in ["~$draft.docx", "sheet.swp", "sheet.SWO", "a.tmp", "b.part",
                     "c.crdownload", ".DS_Store"] {
            XCTAssertEqual(decide(file(name), includeHidden: true), .skip(.temporary), name)
        }
        // No arbitrary substring matching: a document that merely contains one
        // of those words stays eligible.
        XCTAssertEqual(
            decide(file("tmp-notes.md")),
            .eligible(kind: .markdown, byteLength: 128)
        )
        XCTAssertEqual(
            decide(file("partial-report.pdf")),
            .eligible(kind: .pdf, byteLength: 128)
        )
    }

    func testNamedDependencyAndCacheTreesAreExcludedAndOthersAreNot() {
        for name in ["node_modules", "__pycache__", "DerivedData", ".git", ".venv"] {
            XCTAssertEqual(
                decide(directory(name), includeHidden: true),
                .skip(.toolCache),
                name
            )
        }
        // Not every folder called build or dist is assumed disposable.
        XCTAssertEqual(decide(directory("build")), .descend)
        XCTAssertEqual(decide(directory("dist")), .descend)
        // And a document whose name matches a cache tree is still a document.
        XCTAssertEqual(
            decide(file("node_modules.md")),
            .eligible(kind: .markdown, byteLength: 128)
        )
    }

    func testRecursionOffNeverEvenListsASubfolder() {
        XCTAssertEqual(decide(directory("nested"), recursive: false), .skip(.notRecursive))
        XCTAssertEqual(
            decide(file("top.txt"), recursive: false),
            .eligible(kind: .text, byteLength: 128)
        )
    }

    func testDepthBeyondTheReviewedLimitIsExcluded() {
        let limits = FolderScopeLimits(
            maximumEligibleFiles: 10,
            maximumExaminedEntries: 10,
            maximumDepth: 3
        )
        XCTAssertEqual(
            decide(file("deep.txt", depth: 3), limits: limits),
            .eligible(kind: .text, byteLength: 128)
        )
        XCTAssertEqual(decide(file("deep.txt", depth: 4), limits: limits), .skip(.depthLimit))
    }

    func testOnlyApprovedFormatsQualifyAndNothingIsSniffed() {
        XCTAssertEqual(decide(file("archive.bin")), .skip(.unsupportedFormat))
        XCTAssertEqual(decide(file("script.sh")), .skip(.unsupportedFormat))
        XCTAssertEqual(decide(file("noextension")), .skip(.unsupportedFormat))
    }

    func testIrregularKindsAreNeverOpened() {
        XCTAssertEqual(
            decide(FolderEntryFacts(relativeComponents: ["pipe.txt"], byteLength: 8)),
            .skip(.irregularKind)
        )
    }

    func testPerFormatSizeBoundsRefuseRatherThanTruncate() {
        XCTAssertEqual(FolderScopePolicy.maximumSourceBytes(for: .text), 1 * 1024 * 1024)
        XCTAssertEqual(FolderScopePolicy.maximumSourceBytes(for: .html), 20 * 1024 * 1024)
        XCTAssertEqual(FolderScopePolicy.maximumSourceBytes(for: .pdf), 50 * 1024 * 1024)
        XCTAssertEqual(
            decide(file("notes.txt", bytes: 1 * 1024 * 1024 + 1)),
            .skip(.tooLarge)
        )
        XCTAssertEqual(
            decide(file("page.html", bytes: 20 * 1024 * 1024 + 1)),
            .skip(.tooLarge)
        )
        XCTAssertEqual(
            decide(file("big.pdf", bytes: 50 * 1024 * 1024)),
            .eligible(kind: .pdf, byteLength: 50 * 1024 * 1024)
        )
        XCTAssertEqual(decide(file("big.pdf", bytes: 50 * 1024 * 1024 + 1)), .skip(.tooLarge))
    }

    func testAnUnreadableSizeIsUnavailableAndAnEmptyFileIsUnsupported() {
        XCTAssertEqual(decide(file("cloud.pdf", bytes: nil)), .skip(.unknownSize))
        XCTAssertEqual(FolderExclusionReason.unknownSize.bucket, .unavailable)
        XCTAssertEqual(decide(file("empty.txt", bytes: 0)), .skip(.emptyFile))
        XCTAssertEqual(FolderExclusionReason.emptyFile.bucket, .unsupported)
    }

    func testEveryReasonReportsUnderExactlyOneReviewedBucketWithDetail() {
        for reason in FolderExclusionReason.allCases {
            XCTAssertFalse(reason.reviewDetail.isEmpty, reason.rawValue)
        }
        XCTAssertEqual(FolderExclusionReason.hidden.bucket, .excluded)
        XCTAssertEqual(FolderExclusionReason.unsupportedFormat.bucket, .unsupported)
    }

    func testRefusedRootsAskForANarrowerDocumentFolder() {
        let store = URL(fileURLWithPath: "/private/tmp/omnivia-state", isDirectory: true)
        for path in ["/", "/System", "/Users", "/Volumes", "/Library"] {
            XCTAssertTrue(
                FolderScopePolicy.isRefusedRoot(
                    URL(fileURLWithPath: path, isDirectory: true),
                    controlledStores: [store]
                ),
                path
            )
        }
        XCTAssertTrue(
            FolderScopePolicy.isRefusedRoot(
                FileManager.default.homeDirectoryForCurrentUser
                    .resolvingSymlinksInPath().standardizedFileURL,
                controlledStores: []
            )
        )
        XCTAssertTrue(
            FolderScopePolicy.isRefusedRoot(
                URL(fileURLWithPath: "/private/tmp/omnivia-state/native-access", isDirectory: true),
                controlledStores: [store]
            )
        )
        XCTAssertTrue(
            FolderScopePolicy.isRefusedRoot(
                FileManager.default.homeDirectoryForCurrentUser
                    .appendingPathComponent(".Trash/old", isDirectory: true),
                controlledStores: []
            )
        )
        XCTAssertFalse(
            FolderScopePolicy.isRefusedRoot(
                URL(fileURLWithPath: "/private/tmp/omnivia-documents", isDirectory: true),
                controlledStores: [store]
            )
        )
    }

    // MARK: - Truthful coverage

    func testACleanScanIsCompleteAndRecordsNoCoverageFailure() throws {
        let root = try temporaryDirectory()
        try write("a", to: root.appendingPathComponent("a.txt"))
        try write("b", to: root.appendingPathComponent("b.md"))
        let nested = root.appendingPathComponent("nested", isDirectory: true)
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        try write("c", to: nested.appendingPathComponent("c.txt"))

        let scan = try scan(root)

        XCTAssertEqual(scan.members.map(\.displayName), ["a.txt", "b.md", "nested/c.txt"])
        XCTAssertTrue(scan.coverageFailures.isEmpty)
        XCTAssertTrue(scan.isComplete)
        XCTAssertNil(FolderScopeCoverageFailure.reviewDetail(scan.coverageFailures))
    }

    func testARootTheTraversalCannotListIsNotReportedAsAnEmptyFolder() throws {
        let root = try temporaryDirectory()
        try write("a", to: root.appendingPathComponent("a.txt"))

        // An injected root failure is a typed refusal, never a scan that
        // happens to hold no members.
        XCTAssertThrowsError(try scan(root, failing: {
            $0.lastPathComponent == root.lastPathComponent ? .enumerationFailed : nil
        })) {
            XCTAssertEqual($0 as? FolderScopeScanError, .rootUnavailable)
        }

        // And the same for a real unlistable root, which reaches the traversal
        // through the enumerator's own error handler rather than the probe.
        try XCTSkipUnless(makeUnreadable(root), "This system still lists an unreadable directory.")
        XCTAssertThrowsError(try scan(root)) {
            XCTAssertEqual($0 as? FolderScopeScanError, .rootUnavailable)
        }
    }

    func testASubtreeThatFailsAfterSomeMembersLeavesTheScanIncomplete() throws {
        let root = try temporaryDirectory()
        try write("a", to: root.appendingPathComponent("a.txt"))
        try write("z", to: root.appendingPathComponent("z.txt"))
        let nested = root.appendingPathComponent("nested", isDirectory: true)
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        try write("hidden-by-failure", to: nested.appendingPathComponent("c.txt"))

        let scan = try scan(root, failing: {
            $0.lastPathComponent == "nested" ? .enumerationFailed : nil
        })

        // The members that were readable are still reported — and the subtree
        // that was not is reported too, instead of simply being absent.
        XCTAssertEqual(scan.members.map(\.displayName), ["a.txt", "z.txt"])
        XCTAssertEqual(
            scan.coverageFailures,
            [FolderScopeCoverageFailure(kind: .enumerationFailed, relativeComponents: ["nested"])]
        )
        XCTAssertFalse(scan.isComplete)
        XCTAssertNil(scan.limit)
    }

    func testARealUnreadableSubtreeIsRecordedRatherThanOmitted() throws {
        let root = try temporaryDirectory()
        try write("a", to: root.appendingPathComponent("a.txt"))
        let nested = root.appendingPathComponent("nested", isDirectory: true)
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        try write("c", to: nested.appendingPathComponent("c.txt"))
        try XCTSkipUnless(makeUnreadable(nested), "This system still lists an unreadable directory.")

        let scan = try scan(root)

        XCTAssertEqual(scan.members.map(\.displayName), ["a.txt"])
        XCTAssertEqual(scan.coverageFailures.map(\.relativeComponents), [["nested"]])
        XCTAssertFalse(scan.isComplete)
    }

    func testAProviderStyleEntryFailureIsCoverageLossNotAnOrdinaryExclusion() throws {
        let root = try temporaryDirectory()
        try write("a", to: root.appendingPathComponent("a.txt"))
        try write("cloud", to: root.appendingPathComponent("cloud.pdf"))

        let scan = try scan(root, failing: {
            $0.lastPathComponent == "cloud.pdf" ? .entryUnreadable : nil
        })

        XCTAssertEqual(scan.members.map(\.displayName), ["a.txt"])
        XCTAssertEqual(
            scan.coverageFailures,
            [FolderScopeCoverageFailure(kind: .entryUnreadable, relativeComponents: ["cloud.pdf"])]
        )
        // A reviewed exclusion says the scan saw the item and decided about it.
        // This one was never read, so it is counted under neither.
        XCTAssertTrue(scan.exclusionCounts.isEmpty)
        XCTAssertFalse(scan.isComplete)
    }

    func testTheReviewedCoverageSentenceCountsItemsWithoutNamingAPath() throws {
        let root = try temporaryDirectory()
        try write("secret", to: root.appendingPathComponent("payroll-2026.pdf"))

        let scan = try scan(root, failing: {
            $0.lastPathComponent == "payroll-2026.pdf" ? .entryUnreadable : nil
        })
        let detail = try XCTUnwrap(FolderScopeCoverageFailure.reviewDetail(scan.coverageFailures))

        XCTAssertTrue(detail.contains("1 item"))
        XCTAssertFalse(detail.contains(root.path))
        XCTAssertFalse(detail.contains("payroll"))
    }

    func testContainmentIsComponentBoundedNotAStringPrefix() {
        let ancestor = URL(fileURLWithPath: "/private/tmp/a/b", isDirectory: true)
        XCTAssertTrue(
            FolderScopePolicy.isContained(
                URL(fileURLWithPath: "/private/tmp/a/b/c.txt"),
                in: ancestor
            )
        )
        XCTAssertFalse(
            FolderScopePolicy.isContained(
                URL(fileURLWithPath: "/private/tmp/a/bc/c.txt"),
                in: ancestor
            )
        )
        XCTAssertFalse(FolderScopePolicy.isContained(ancestor, in: ancestor))
    }

    // MARK: - Helpers

    private func scan(
        _ root: URL,
        failing probe: FolderScanFailureProbe? = nil
    ) throws -> FolderScopeScan {
        try FolderScopePolicy.scan(
            root: root.resolvingSymlinksInPath().standardizedFileURL,
            recursive: true,
            includeHidden: false,
            controlledStores: [],
            failureProbe: probe
        )
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

    private func write(_ contents: String, to url: URL) throws {
        try Data(contents.utf8).write(to: url)
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("omnivia-scope-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o700],
                ofItemAtPath: url.path
            )
            try? FileManager.default.removeItem(at: url)
        }
        return url
    }
}
