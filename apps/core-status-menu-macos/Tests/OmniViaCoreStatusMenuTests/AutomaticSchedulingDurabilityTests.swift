@testable import DocumentIntakeMacOS
import Foundation
import XCTest

/// WP-12 slice 2: the durable, restart-safe side of `AutomaticSchedulingStateMachine`.
///
/// Every test drives a real owner-only state directory, and rebuilds the owner from disk wherever
/// restart matters — a second `AutomaticSchedulingStateMachine(stateDirectory:)` over the same
/// directory *is* the next process, with a clock whose zero has moved. The restart assertions all
/// claim at small `now` values on purpose: a fresh process's clock starts near zero, so work that
/// had already served its quiet period before the restart could only be stranded there.
final class AutomaticSchedulingDurabilityTests: XCTestCase {
    private var root: URL!

    private var stateDirectory: URL { root.appendingPathComponent("automatic-scheduling", isDirectory: true) }
    private var stateFile: URL { stateDirectory.appendingPathComponent("automatic-scheduling-v1.json") }

    private static let key = AutomaticSchedulingKey(
        source: AutomaticSchedulingSource(workspaceRef: "workspace-a", sourceRef: "source-a"),
        scopeKey: "scope-a"
    )

    override func setUpWithError() throws {
        root = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("omnivia-automatic-scheduling-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        // A read-only state directory from a write-failure test has to be writable again to go.
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o700], ofItemAtPath: stateDirectory.path
        )
        try? FileManager.default.removeItem(at: root)
    }

    /// The next process over the same durable state.
    private func machine() throws -> AutomaticSchedulingStateMachine {
        try AutomaticSchedulingStateMachine(stateDirectory: stateDirectory, gate: PublicationGate())
    }

    // MARK: - Restart recovery

    func testRestartKeepsDirtyWorkDirtyAndImmediatelyDue() throws {
        let first = try machine()
        let generation = try first.signal(Self.key, priority: .user, at: 1_000)
        // Still inside the quiet period when the process went away.
        XCTAssertNil(try first.claim(at: 2_000))

        let second = try machine()
        let state = try XCTUnwrap(second.state(of: Self.key))
        XCTAssertEqual(state.dirtyGeneration, generation)
        XCTAssertEqual(state.priority, .user)
        XCTAssertNil(state.activeGeneration)
        XCTAssertFalse(state.published)
        XCTAssertTrue(state.restoredFromRestart)
        // No instant from the previous process's clock came across as if it were one of ours.
        XCTAssertEqual(state.firstSignalMilliseconds, 0)
        XCTAssertEqual(state.latestSignalMilliseconds, 0)

        // Due the first time the new process looks, at a `now` where nothing raised in it could be.
        let claim = try XCTUnwrap(second.claim(at: 0))
        XCTAssertEqual(claim.generation, generation)
        XCTAssertEqual(claim.priority, .user)
        XCTAssertTrue(claim.restoredFromRestart)
    }

    func testRecoveredWorkStaysImmediatelyDueThroughAClaimAndCancel() throws {
        let first = try machine()
        let generation = try first.signal(Self.key, priority: .background, at: 0)

        let second = try machine()
        XCTAssertEqual(try second.claim(at: 0)?.generation, generation)
        // Cancelled recovered work does not quietly acquire this process's clock and start waiting.
        XCTAssertEqual(try second.cancel(Self.key, generation: generation, at: 1), generation)
        XCTAssertTrue(try XCTUnwrap(second.state(of: Self.key)).restoredFromRestart)
        XCTAssertEqual(try second.claim(at: 1)?.generation, generation)
    }

    func testSignalsRaisedAfterARestartServeTheFullQuietPeriod() throws {
        let first = try machine()
        let resolved = try first.signal(Self.key, priority: .background, at: 0)
        XCTAssertEqual(try first.claim(at: 3_000)?.generation, resolved)
        try first.markPublished(Self.key, generation: resolved, at: 3_100)
        try first.markCompleted(Self.key, generation: resolved, at: 3_200)

        let second = try machine()
        // A fully clean scope is not retained, so it spends no place in the bounds and its next
        // signal is a first signal again.
        XCTAssertNil(second.state(of: Self.key))
        XCTAssertEqual(second.trackedScopes, 0)
        XCTAssertEqual(try second.signal(Self.key, priority: .background, at: 0), 1)

        // The 3-second quiet period governs it in full: recovery relaxes nothing for new work.
        XCTAssertNil(try second.claim(at: 2_999))
        XCTAssertEqual(try second.claim(at: 3_000)?.restoredFromRestart, false)
    }

    func testRepeatedSignalsRaisedAfterARestartStillWaitOutTheMaximumWait() throws {
        let first = try machine()
        let resolved = try first.signal(Self.key, priority: .background, at: 0)
        XCTAssertEqual(try first.claim(at: 3_000)?.generation, resolved)
        try first.markPublished(Self.key, generation: resolved, at: 3_100)
        try first.markCompleted(Self.key, generation: resolved, at: 3_200)

        let second = try machine()
        try second.signal(Self.key, priority: .background, at: 0)
        for now in stride(from: Int64(2_500), through: 27_500, by: 2_500) {
            try second.signal(Self.key, priority: .background, at: now)
        }
        XCTAssertNil(try second.claim(at: 29_999))
        XCTAssertNotNil(try second.claim(at: 30_000))
    }

    func testAnUnpublishedCandidateIsReturnedToDirtyByARestart() throws {
        let first = try machine()
        let generation = try first.signal(Self.key, priority: .user, at: 0)
        XCTAssertEqual(try first.claim(at: 3_000)?.generation, generation)
        // The process went away carrying the candidate; nothing published it.

        let second = try machine()
        let state = try XCTUnwrap(second.state(of: Self.key))
        XCTAssertEqual(state.dirtyGeneration, generation)
        XCTAssertEqual(state.priority, .user)
        XCTAssertNil(state.activeGeneration)
        XCTAssertFalse(state.published)
        XCTAssertTrue(state.restoredFromRestart)
        // Eligible, not stranded behind a claim no process is carrying.
        XCTAssertEqual(try second.claim(at: 0)?.generation, generation)
    }

    func testAnUnpublishedCandidateWithWorkBehindItKeepsTheLaterGeneration() throws {
        let first = try machine()
        let claimed = try first.signal(Self.key, priority: .user, at: 0)
        XCTAssertEqual(try first.claim(at: 3_000)?.generation, claimed)
        let later = try first.signal(Self.key, priority: .background, at: 4_000)

        let second = try machine()
        let state = try XCTUnwrap(second.state(of: Self.key))
        XCTAssertEqual(state.dirtyGeneration, later)
        // The abandoned candidate's user priority was merged into it, not dropped with it.
        XCTAssertEqual(state.priority, .user)
        XCTAssertNil(state.activeGeneration)
        XCTAssertEqual(try second.claim(at: 0)?.generation, later)
        // Recovery minted no generation of its own: the next signal is simply the one after.
        XCTAssertEqual(try second.signal(Self.key, priority: .background, at: 1), later + 1)
    }

    func testAPublishedGenerationStaysActiveAndBlocksADuplicateClaim() throws {
        let first = try machine()
        let published = try first.signal(Self.key, priority: .background, at: 0)
        XCTAssertEqual(try first.claim(at: 3_000)?.generation, published)
        try first.markPublished(Self.key, generation: published, at: 3_100)

        let second = try machine()
        let state = try XCTUnwrap(second.state(of: Self.key))
        XCTAssertEqual(state.activeGeneration, published)
        XCTAssertTrue(state.published)
        XCTAssertNil(state.dirtyGeneration)
        // The published intent is still the scope's one unresolved intent, so nothing duplicates it
        // — not at the new clock's zero and not long after the old periods would have elapsed.
        XCTAssertNil(try second.claim(at: 0))
        XCTAssertNil(try second.claim(at: 60_000))

        // The queue/outcome reconciliation still resolves it, exactly as it would have before.
        try second.markCompleted(Self.key, generation: published, at: 60_000)
        XCTAssertNil(second.state(of: Self.key))
    }

    func testALaterDirtyGenerationBehindAPublishedOneSurvivesARestart() throws {
        let first = try machine()
        let published = try first.signal(Self.key, priority: .background, at: 0)
        XCTAssertEqual(try first.claim(at: 3_000)?.generation, published)
        try first.markPublished(Self.key, generation: published, at: 3_100)
        let later = try first.signal(Self.key, priority: .user, at: 4_000)

        let second = try machine()
        let state = try XCTUnwrap(second.state(of: Self.key))
        XCTAssertEqual(state.activeGeneration, published)
        XCTAssertTrue(state.published)
        XCTAssertEqual(state.dirtyGeneration, later)
        XCTAssertEqual(state.priority, .user)
        XCTAssertNil(try second.claim(at: 0))

        try second.markCompleted(Self.key, generation: published, at: 100)
        let claim = try XCTUnwrap(second.claim(at: 100))
        XCTAssertEqual(claim.generation, later)
        XCTAssertEqual(claim.priority, .user)
        XCTAssertTrue(claim.restoredFromRestart)
    }

    func testGenerationContinuityRunsThroughARecoveredPublishedClaim() throws {
        let first = try machine()
        let published = try first.signal(Self.key, priority: .background, at: 0)
        XCTAssertEqual(try first.claim(at: 3_000)?.generation, published)
        try first.markPublished(Self.key, generation: published, at: 3_100)

        // The published generation is the latest one issued even though no dirty record holds it,
        // so a signal after the restart is the one after it rather than a first signal again.
        let second = try machine()
        XCTAssertEqual(try second.signal(Self.key, priority: .background, at: 0), published + 1)
    }

    func testGenerationsKeepRisingAcrossRepeatedRestarts() throws {
        var generation: UInt64 = 0
        for restart in 0..<4 {
            generation = try machine().signal(Self.key, priority: .background, at: 0)
            XCTAssertEqual(generation, UInt64(restart) + 1)
        }
        // And a claim across the last restart still sees the one accumulated record, not four.
        XCTAssertEqual(try machine().claim(at: 0)?.generation, 4)
        XCTAssertEqual(try machine().trackedScopes, 1)
    }

    func testRecoveredUserWorkIsStillSelectedBeforeRecoveredBackgroundWork() throws {
        let background = Self.makeKey("scope-a")
        // Last in the rotation order, so the rotation cannot be what explains the choice.
        let user = Self.makeKey("scope-z")
        let first = try machine()
        try first.signal(background, priority: .background, at: 0)
        try first.signal(user, priority: .user, at: 0)

        let second = try machine()
        let claim = try XCTUnwrap(second.claim(at: 0))
        XCTAssertEqual(claim.key, user)
        XCTAssertEqual(claim.priority, .user)
        // The background work was passed over, not dropped.
        XCTAssertEqual(try second.claim(at: 1)?.key, background)
    }

    func testTheBackgroundRotationCursorSurvivesARestart() throws {
        let keys = ["scope-a", "scope-b", "scope-c"].map { Self.makeKey($0) }
        let first = try machine()
        for key in keys { try first.signal(key, priority: .background, at: 0) }
        XCTAssertEqual(try first.claim(at: 3_000)?.key, keys[0])
        try first.cancel(keys[0], generation: 1, at: 3_100)

        // The new process resumes after scope-a instead of starting the rotation over, so no scope
        // is served twice before the others have had their turn.
        let second = try machine()
        XCTAssertEqual(try second.claim(at: 0)?.key, keys[1])
        try second.cancel(keys[1], generation: 1, at: 1)
        XCTAssertEqual(try second.claim(at: 2)?.key, keys[2])
        XCTAssertEqual(try second.claim(at: 3)?.key, keys[0])
    }

    // MARK: - Write failures

    func testAFailedWriteLeavesThePriorDurableAndInMemoryStateUnchanged() throws {
        let machine = try machine()
        let generation = try machine.signal(Self.key, priority: .user, at: 0)
        let before = try XCTUnwrap(machine.state(of: Self.key))
        let bytes = try Data(contentsOf: stateFile)
        // The atomic temp write cannot be created in a read-only directory.
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o500], ofItemAtPath: stateDirectory.path
        )

        XCTAssertThrowsError(try machine.signal(Self.key, priority: .user, at: 1_000)) {
            XCTAssertEqual($0 as? AutomaticSchedulingError, .stateUnavailable)
        }
        XCTAssertEqual(machine.state(of: Self.key), before)
        XCTAssertEqual(try Data(contentsOf: stateFile), bytes)

        try FileManager.default.setAttributes(
            [.posixPermissions: 0o700], ofItemAtPath: stateDirectory.path
        )
        // The refused call did not move the clock on either, so the quiet period is still measured
        // from the signal that was accepted rather than from the one that failed.
        XCTAssertNil(try machine.claim(at: 2_999))
        XCTAssertEqual(try machine.claim(at: 3_000)?.generation, generation)
        XCTAssertEqual(try self.machine().state(of: Self.key)?.dirtyGeneration, generation)
    }

    func testAFailedWriteWhileClaimingLeavesNothingClaimed() throws {
        let machine = try machine()
        try machine.signal(Self.key, priority: .user, at: 0)
        let before = try XCTUnwrap(machine.state(of: Self.key))
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o500], ofItemAtPath: stateDirectory.path
        )

        XCTAssertThrowsError(try machine.claim(at: 3_000)) {
            XCTAssertEqual($0 as? AutomaticSchedulingError, .stateUnavailable)
        }
        XCTAssertEqual(machine.state(of: Self.key), before)

        try FileManager.default.setAttributes(
            [.posixPermissions: 0o700], ofItemAtPath: stateDirectory.path
        )
        // The rotation cursor rolled back with the rest, so the refused claim is still the next one.
        XCTAssertEqual(try machine.claim(at: 3_000)?.key, Self.key)
    }

    func testAClaimThatFindsNothingDueWritesNothing() throws {
        let machine = try machine()
        try machine.signal(Self.key, priority: .user, at: 0)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o500], ofItemAtPath: stateDirectory.path
        )
        // Nothing changed, so there is nothing to write and nothing to fail — a poll loop against
        // an idle table does not touch the disk once per tick.
        XCTAssertNil(try machine.claim(at: 1_000))
    }

    func testTheInMemoryInitializerWritesNothing() throws {
        let machine = AutomaticSchedulingStateMachine()
        try machine.signal(Self.key, priority: .user, at: 0)
        XCTAssertNotNil(try machine.claim(at: 3_000))
        XCTAssertFalse(FileManager.default.fileExists(atPath: stateDirectory.path))
    }

    // MARK: - Permissions

    func testTheStateFileAndDirectoryAreOwnerOnly() throws {
        let machine = try machine()
        try machine.signal(Self.key, priority: .user, at: 0)
        XCTAssertEqual(try permissions(stateFile), 0o600)
        XCTAssertEqual(try permissions(stateDirectory), 0o700)
        // And every rewrite after the first, which replaces an existing file rather than moving a
        // new one into place, publishes the mode the temporary file was given.
        try machine.signal(Self.key, priority: .user, at: 1_000)
        XCTAssertEqual(try permissions(stateFile), 0o600)
    }

    func testAStateFileOthersCanReadIsRefused() throws {
        let machine = try machine()
        try machine.signal(Self.key, priority: .user, at: 0)
        for mode in [0o640, 0o604, 0o666] {
            try FileManager.default.setAttributes(
                [.posixPermissions: mode], ofItemAtPath: stateFile.path
            )
            expectOpenFails(.invalidState, "mode \(String(mode, radix: 8))")
        }
    }

    func testAStateDirectoryThatIsASymbolicLinkIsRefused() throws {
        let target = root.appendingPathComponent("elsewhere", isDirectory: true)
        try FileManager.default.createDirectory(at: target, withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(at: stateDirectory, withDestinationURL: target)
        expectOpenFails(.stateUnavailable)
    }

    // MARK: - Schema and tampering

    func testANewerBuildsDocumentIsRefusedAndLeftExactlyAsItWas() throws {
        var document = try base()
        document["schema"] = "omnivia.automatic-scheduling.v2"
        document["unknownFutureField"] = ["whatever a later slice adds"]
        let bytes = try write(document)

        expectOpenFails(.unsupportedSchema)
        // Refused, not downgraded and not repaired: the newer build's document is byte for byte
        // what it was, so that build still reads its own state.
        XCTAssertEqual(try Data(contentsOf: stateFile), bytes)
    }

    func testMalformedAndTamperedStateIsRefusedWithoutRewritingTheFile() throws {
        let cases: [(String, AutomaticSchedulingError, (inout [String: Any]) -> Void)] = [
            ("missing schema", .invalidState, { $0.removeValue(forKey: "schema") }),
            ("schema of the wrong type", .invalidState, { $0["schema"] = 7 }),
            ("older schema", .unsupportedSchema, { $0["schema"] = "omnivia.automatic-scheduling.v0" }),
            ("entries of the wrong type", .invalidState, { $0["entries"] = "nope" }),
            ("duplicate keys", .invalidState, {
                let entries = $0["entries"] as? [[String: Any]] ?? []
                $0["entries"] = entries + entries
            }),
            ("entries out of order", .invalidState, {
                let entries = $0["entries"] as? [[String: Any]] ?? []
                var later = entries[0]
                later["key"] = ["workspaceRef": "workspace-a", "sourceRef": "source-a", "scopeKey": "scope-z"]
                $0["entries"] = [later] + entries
            }),
            ("a record for a clean scope", .invalidState, {
                Self.editFirstEntry(&$0) {
                    for field in ["dirtyGeneration", "dirtyPriority", "activeGeneration", "activePriority"] {
                        $0.removeValue(forKey: field)
                    }
                    $0["published"] = false
                }
            }),
            ("published with nothing in flight", .invalidState, {
                Self.editFirstEntry(&$0) {
                    $0.removeValue(forKey: "activeGeneration")
                    $0.removeValue(forKey: "activePriority")
                }
            }),
            ("a dirty generation with no priority", .invalidState, {
                Self.editFirstEntry(&$0) { $0.removeValue(forKey: "dirtyPriority") }
            }),
            ("a dirty priority with no generation", .invalidState, {
                Self.editFirstEntry(&$0) { $0.removeValue(forKey: "dirtyGeneration") }
            }),
            ("an active generation with no priority", .invalidState, {
                Self.editFirstEntry(&$0) { $0.removeValue(forKey: "activePriority") }
            }),
            ("dirty work no later than the claim it accumulated behind", .invalidState, {
                Self.editFirstEntry(&$0) { $0["dirtyGeneration"] = 1 }
            }),
            ("a claim later than the dirty work behind it", .invalidState, {
                Self.editFirstEntry(&$0) { $0["activeGeneration"] = 3 }
            }),
            ("generation zero", .invalidState, {
                Self.editFirstEntry(&$0) { $0["activeGeneration"] = 0 }
            }),
            ("a generation past the bound", .invalidState, {
                Self.editFirstEntry(&$0) {
                    $0["dirtyGeneration"] = AutomaticSchedulingStateMachine.maximumGeneration + 1
                }
            }),
            ("an unknown priority", .invalidState, {
                Self.editFirstEntry(&$0) { $0["dirtyPriority"] = "urgent" }
            }),
            ("a published marker of the wrong type", .invalidState, {
                Self.editFirstEntry(&$0) { $0["published"] = "yes" }
            }),
            ("a ref shaped like a path", .invalidState, {
                Self.editFirstEntry(&$0) {
                    $0["key"] = ["workspaceRef": "workspace-a", "sourceRef": "source-a", "scopeKey": "a/b"]
                }
            }),
            ("an empty ref", .invalidState, {
                Self.editFirstEntry(&$0) {
                    $0["key"] = ["workspaceRef": "", "sourceRef": "source-a", "scopeKey": "scope-a"]
                }
            }),
            ("an over-long ref", .invalidState, {
                Self.editFirstEntry(&$0) {
                    $0["key"] = [
                        "workspaceRef": "workspace-a",
                        "sourceRef": String(
                            repeating: "a", count: AutomaticSchedulingStateMachine.maximumRefBytes + 1
                        ),
                        "scopeKey": "scope-a",
                    ]
                }
            }),
            ("a missing ref", .invalidState, {
                Self.editFirstEntry(&$0) { $0["key"] = ["workspaceRef": "workspace-a"] }
            }),
            ("a rotation cursor that is not an opaque ref", .invalidState, {
                $0["backgroundCursor"] = [
                    "workspaceRef": "workspace-a", "sourceRef": "source-a", "scopeKey": "/etc",
                ]
            }),
        ]

        for (name, expected, change) in cases {
            var document = try base()
            change(&document)
            let bytes = try write(document)
            expectOpenFails(expected, name)
            // Fail closed means fail quietly: the file a human or another build may still need to
            // look at is not rewritten on the way out.
            XCTAssertEqual(try Data(contentsOf: stateFile), bytes, name)
        }
    }

    func testStateThatIsNotJSONAtAllIsRefused() throws {
        _ = try base()
        for junk in ["", "{", "[]", "null", "{\"schema\""] {
            try Data(junk.utf8).write(to: stateFile)
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o600], ofItemAtPath: stateFile.path
            )
            expectOpenFails(.invalidState, junk)
        }
    }

    // MARK: - Bounds

    func testADocumentOverTheSourceOrScopeBoundsIsRefused() throws {
        _ = try base()
        let sources = AutomaticSchedulingStateMachine.maximumTrackedSources
        let scopes = AutomaticSchedulingStateMachine.maximumScopesPerSource

        // One source over the bound, each with one scope. Padded so the on-disk order, which is by
        // scope key then source, is the lexicographic one the loader insists on.
        var document = try base()
        document["entries"] = (0...sources).map { Self.record(scope: "scope-a", source: "source-\(Self.pad($0))") }
        document["backgroundCursor"] = nil
        try write(document)
        expectOpenFails(.invalidState, "sources")

        // One scope over the bound on a single source.
        document = try base()
        document["entries"] = (0...scopes).map { Self.record(scope: "scope-\(Self.pad($0))", source: "source-a") }
        document["backgroundCursor"] = nil
        try write(document)
        expectOpenFails(.invalidState, "scopes")
    }

    func testAStateFileOverTheSizeBoundIsRefusedBeforeItIsParsed() throws {
        _ = try base()
        try Data(
            repeating: UInt8(ascii: "x"), count: AutomaticSchedulingStateMachine.maximumStateBytes + 1
        ).write(to: stateFile)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateFile.path)
        expectOpenFails(.invalidState)
    }

    func testASignalPastTheGenerationBoundIsRefusedRatherThanWrappedRound() throws {
        let machine = try machine()
        try machine.signal(Self.key, priority: .background, at: 0)
        var document = try self.document()
        Self.editFirstEntry(&document) {
            $0["dirtyGeneration"] = AutomaticSchedulingStateMachine.maximumGeneration
        }
        try write(document)

        let reopened = try self.machine()
        let bound = AutomaticSchedulingStateMachine.maximumGeneration
        XCTAssertEqual(reopened.state(of: Self.key)?.dirtyGeneration, bound)
        XCTAssertThrowsError(try reopened.signal(Self.key, priority: .background, at: 0)) {
            XCTAssertEqual($0 as? AutomaticSchedulingError, .generationExhausted)
        }
        // The work that is there is untouched and still claimable; only the new signal was refused.
        XCTAssertEqual(reopened.state(of: Self.key)?.dirtyGeneration, bound)
        XCTAssertEqual(try reopened.claim(at: 0)?.generation, bound)
    }

    // MARK: - Environment

    func testTheEnvironmentOwnsADurableSchedulerThatSurvivesARestart() throws {
        let stateRoot = root.appendingPathComponent("state", isDirectory: true)
        let generation = try DocumentIntakeEnvironment(stateRoot: stateRoot)
            .automaticScheduling.signal(Self.key, priority: .user, at: 0)
        let second = try DocumentIntakeEnvironment(stateRoot: stateRoot)
        XCTAssertEqual(second.automaticScheduling.state(of: Self.key)?.dirtyGeneration, generation)
        XCTAssertEqual(try second.automaticScheduling.claim(at: 0)?.generation, generation)
    }

    // MARK: - Helpers

    private static func makeKey(
        _ scopeKey: String,
        workspace: String = "workspace-a",
        source: String = "source-a"
    ) -> AutomaticSchedulingKey {
        AutomaticSchedulingKey(
            source: AutomaticSchedulingSource(workspaceRef: workspace, sourceRef: source),
            scopeKey: scopeKey
        )
    }

    /// A real document holding one entry with every field populated: a published claim with a later
    /// dirty generation accumulated behind it, plus a rotation cursor. Every tampering case starts
    /// from this, so each one changes exactly the thing it is named for.
    private func base() throws -> [String: Any] {
        try? FileManager.default.removeItem(at: stateDirectory)
        let machine = try machine()
        let published = try machine.signal(Self.key, priority: .background, at: 0)
        XCTAssertEqual(try machine.claim(at: 3_000)?.generation, published)
        try machine.markPublished(Self.key, generation: published, at: 3_100)
        try machine.signal(Self.key, priority: .user, at: 4_000)
        return try document()
    }

    /// A minimal valid record, for the bounds cases that need more entries than a real run makes.
    private static func record(scope: String, source: String) -> [String: Any] {
        [
            "key": ["workspaceRef": "workspace-a", "sourceRef": source, "scopeKey": scope],
            "dirtyGeneration": 1,
            "dirtyPriority": "background",
            "published": false,
        ]
    }

    private static func pad(_ value: Int) -> String { String(format: "%04d", value) }

    private static func editFirstEntry(
        _ document: inout [String: Any],
        _ change: (inout [String: Any]) -> Void
    ) {
        var entries = document["entries"] as? [[String: Any]] ?? []
        guard !entries.isEmpty else { return }
        change(&entries[0])
        document["entries"] = entries
    }

    private func document() throws -> [String: Any] {
        try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: stateFile)) as? [String: Any]
        )
    }

    @discardableResult
    private func write(_ document: [String: Any]) throws -> Data {
        let data = try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
        try data.write(to: stateFile)
        // Owner-only, so the permission check is never what a tampering case trips over.
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateFile.path)
        return data
    }

    private func permissions(_ url: URL) throws -> Int? {
        (try FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions] as? NSNumber)?
            .intValue
    }

    private func expectOpenFails(
        _ expected: AutomaticSchedulingError,
        _ message: String = "",
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        XCTAssertThrowsError(try machine(), message, file: file, line: line) {
            XCTAssertEqual($0 as? AutomaticSchedulingError, expected, message, file: file, line: line)
        }
    }
}
