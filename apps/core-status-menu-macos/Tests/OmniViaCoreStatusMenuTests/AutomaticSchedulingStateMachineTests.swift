@testable import DocumentIntakeMacOS
import XCTest

/// The two timing rules a background signal is subject to: the quiet period measured from the
/// latest signal, and the maximum wait measured from the first.
final class AutomaticSchedulingStateMachineTests: XCTestCase {
    private static let key = AutomaticSchedulingKey(
        source: AutomaticSchedulingSource(workspaceRef: "workspace-a", sourceRef: "source-a"),
        scopeKey: "scope-a"
    )
    /// The opaque queue ref a publication carries. Never a path: see `markPublished`.
    private static let ref = "intent-a"

    func testBackgroundSignalIsClaimableOnlyOnceTheQuietPeriodHasElapsed() throws {
        let machine = AutomaticSchedulingStateMachine()
        let generation = try machine.signal(Self.key, priority: .background, at: 0)

        XCTAssertNil(try machine.claim(at: 2_999))

        let claim = try machine.claim(at: 3_000)
        XCTAssertEqual(claim?.key, Self.key)
        XCTAssertEqual(claim?.generation, generation)
        XCTAssertEqual(claim?.priority, .background)
        XCTAssertEqual(claim?.latestSignalMilliseconds, 0)
    }

    func testRepeatedBackgroundSignalsAreClaimableAtTheMaximumWaitFromTheFirst() throws {
        let machine = AutomaticSchedulingStateMachine()
        var generation = try machine.signal(Self.key, priority: .background, at: 0)
        // Every repeat lands inside the quiet period, so only the maximum wait can release this.
        for now in stride(from: Int64(2_500), through: 27_500, by: 2_500) {
            generation = try machine.signal(Self.key, priority: .background, at: now)
        }
        XCTAssertEqual(generation, 12)

        XCTAssertNil(try machine.claim(at: 29_999))

        let claim = try machine.claim(at: 30_000)
        XCTAssertEqual(claim?.key, Self.key)
        XCTAssertEqual(claim?.generation, generation)
        XCTAssertEqual(claim?.firstSignalMilliseconds, 0)
        XCTAssertEqual(claim?.latestSignalMilliseconds, 27_500)
    }

    func testSignalBehindAPublishedClaimWaitsForCompletionBeforeItIsClaimable() throws {
        let machine = AutomaticSchedulingStateMachine()
        let published = try machine.signal(Self.key, priority: .background, at: 0)
        XCTAssertEqual(try machine.claim(at: 3_000)?.generation, published)
        try machine.markPublished(Self.key, generation: published, intentRef: Self.ref, at: 3_100)

        let later = try machine.signal(Self.key, priority: .background, at: 4_000)
        XCTAssertEqual(later, published + 1)
        let pending = try XCTUnwrap(machine.state(of: Self.key))
        XCTAssertEqual(pending.dirtyGeneration, later)
        XCTAssertEqual(pending.activeGeneration, published)
        XCTAssertTrue(pending.published)

        // Quiet-ineligible, then quiet-eligible: the published intent blocks selection either way.
        XCTAssertNil(try machine.claim(at: 6_999))
        XCTAssertNil(try machine.claim(at: 7_000))

        try machine.markCompleted(Self.key, generation: published, at: 7_500)
        let claim = try machine.claim(at: 8_000)
        XCTAssertEqual(claim?.generation, later)
        XCTAssertEqual(claim?.firstSignalMilliseconds, 4_000)
        XCTAssertEqual(claim?.latestSignalMilliseconds, 4_000)
    }

    func testCancellingAnUnpublishedClaimReturnsTheSameGenerationAsImmediatelyDue() throws {
        let machine = AutomaticSchedulingStateMachine()
        let generation = try machine.signal(Self.key, priority: .background, at: 0)
        XCTAssertEqual(try machine.claim(at: 3_000)?.generation, generation)

        XCTAssertEqual(try machine.cancel(Self.key, generation: generation, at: 3_500), generation)
        let state = try XCTUnwrap(machine.state(of: Self.key))
        XCTAssertEqual(state.dirtyGeneration, generation)
        XCTAssertNil(state.activeGeneration)
        XCTAssertFalse(state.published)

        // The cancelled signal times survived, so the work is as due as it was when claimed.
        let reclaim = try machine.claim(at: 3_600)
        XCTAssertEqual(reclaim?.generation, generation)
        XCTAssertEqual(reclaim?.firstSignalMilliseconds, 0)
        XCTAssertEqual(reclaim?.latestSignalMilliseconds, 0)
    }

    func testCancellingAClaimWithALaterSignalKeepsTheLaterGenerationAndMergesPriority() throws {
        let machine = AutomaticSchedulingStateMachine()
        let claimed = try machine.signal(Self.key, priority: .user, at: 0)
        XCTAssertEqual(try machine.claim(at: 3_000)?.priority, .user)

        let later = try machine.signal(Self.key, priority: .background, at: 4_000)
        XCTAssertEqual(later, claimed + 1)
        XCTAssertEqual(try machine.cancel(Self.key, generation: claimed, at: 5_000), later)

        let state = try XCTUnwrap(machine.state(of: Self.key))
        XCTAssertEqual(state.dirtyGeneration, later)
        XCTAssertEqual(state.priority, .user)
        XCTAssertEqual(state.firstSignalMilliseconds, 0)
        XCTAssertEqual(state.latestSignalMilliseconds, 4_000)

        XCTAssertNil(try machine.claim(at: 6_999))
        let claim = try machine.claim(at: 7_000)
        XCTAssertEqual(claim?.generation, later)
        XCTAssertEqual(claim?.priority, .user)
        XCTAssertEqual(claim?.firstSignalMilliseconds, 0)
        XCTAssertEqual(claim?.latestSignalMilliseconds, 4_000)
        // No generation was minted by the cancel: the next signal is simply the one after.
        XCTAssertEqual(try machine.signal(Self.key, priority: .background, at: 7_000), later + 1)
    }

    // MARK: - The published intent ref

    func testPublishingAttachesTheIntentRefAndResolvingTheIntentTakesItAway() throws {
        let machine = AutomaticSchedulingStateMachine()
        let generation = try machine.signal(Self.key, priority: .user, at: 0)
        XCTAssertEqual(try machine.claim(at: 3_000)?.generation, generation)
        // A claimed candidate has published nothing, so there is no intent to name yet.
        let claimed = try XCTUnwrap(machine.state(of: Self.key))
        XCTAssertNil(claimed.intentRef)
        XCTAssertFalse(claimed.legacyUnresolvedIntent)

        try machine.markPublished(Self.key, generation: generation, intentRef: Self.ref, at: 3_100)
        let published = try XCTUnwrap(machine.state(of: Self.key))
        XCTAssertTrue(published.published)
        XCTAssertEqual(published.intentRef, Self.ref)
        // A ref is something to reconcile against, which is exactly what legacy means it lacks.
        XCTAssertFalse(published.legacyUnresolvedIntent)

        // Work accumulated behind the published intent waits for it rather than duplicating it.
        let later = try machine.signal(Self.key, priority: .user, at: 3_200)
        XCTAssertNil(try machine.claim(at: 10_000))

        try machine.markCompleted(Self.key, generation: generation, at: 10_100)
        let resolved = try XCTUnwrap(machine.state(of: Self.key))
        XCTAssertFalse(resolved.published)
        XCTAssertNil(resolved.intentRef)
        XCTAssertEqual(try machine.claim(at: 10_200)?.generation, later)
    }

    func testCancellingAPublishedIntentTakesItsRefAwayWithIt() throws {
        let machine = AutomaticSchedulingStateMachine()
        let generation = try machine.signal(Self.key, priority: .user, at: 0)
        XCTAssertEqual(try machine.claim(at: 3_000)?.generation, generation)
        try machine.markPublished(Self.key, generation: generation, intentRef: Self.ref, at: 3_100)

        XCTAssertEqual(try machine.cancel(Self.key, generation: generation, at: 3_200), generation)
        let state = try XCTUnwrap(machine.state(of: Self.key))
        XCTAssertFalse(state.published)
        XCTAssertNil(state.intentRef)
        XCTAssertFalse(state.legacyUnresolvedIntent)

        // The abandoned ref is not inherited: the next attempt publishes under its own.
        XCTAssertEqual(try machine.claim(at: 3_300)?.generation, generation)
        try machine.markPublished(Self.key, generation: generation, intentRef: "intent-b", at: 3_400)
        XCTAssertEqual(machine.state(of: Self.key)?.intentRef, "intent-b")
    }

    func testMalformedIntentRefsAreRefusedAndPublishNothing() throws {
        let machine = AutomaticSchedulingStateMachine()
        let maximum = AutomaticSchedulingStateMachine.maximumRefBytes
        let generation = try machine.signal(Self.key, priority: .user, at: 0)
        XCTAssertEqual(try machine.claim(at: 3_000)?.generation, generation)

        let malformed = [
            "",
            // A ref shaped like a path is refused rather than stored: this machine holds no paths.
            "intent/a",
            "/",
            String(repeating: "a", count: maximum + 1),
            String(repeating: "é", count: maximum / 2 + 1),
            "a\u{0}b",
            "a\nb",
            "a\u{7F}b",
        ]
        for value in malformed {
            assertThrows(.malformedIntentRef) {
                try machine.markPublished(
                    Self.key, generation: generation, intentRef: value, at: 3_100
                )
            }
        }
        // Every one was refused before anything moved, so the candidate is still unpublished.
        let state = try XCTUnwrap(machine.state(of: Self.key))
        XCTAssertFalse(state.published)
        XCTAssertNil(state.intentRef)

        // The length bound is inclusive and in bytes, as it is for every other ref.
        let atBound = String(repeating: "é", count: maximum / 2)
        XCTAssertEqual(atBound.utf8.count, maximum)
        try machine.markPublished(Self.key, generation: generation, intentRef: atBound, at: 3_100)
        XCTAssertEqual(machine.state(of: Self.key)?.intentRef, atBound)
    }

    // MARK: - Selection order

    func testUserReadyWorkIsSelectedBeforeOlderBackgroundReadyWork() throws {
        let machine = AutomaticSchedulingStateMachine()
        let background = Self.makeKey("scope-a")
        // Last in the rotation order, so the rotation cannot be what explains the choice.
        let user = Self.makeKey("scope-z")
        try machine.signal(background, priority: .background, at: 0)
        try machine.signal(user, priority: .user, at: 5_000)

        let first = try XCTUnwrap(machine.claim(at: 8_000))
        XCTAssertEqual(first.key, user)
        XCTAssertEqual(first.priority, .user)
        XCTAssertEqual(first.firstSignalMilliseconds, 5_000)

        // The older background work was passed over, not dropped: it is next.
        XCTAssertEqual(try machine.claim(at: 8_100)?.key, background)
    }

    func testThreeBackgroundKeysAreClaimedInRoundRobinOrderAcrossASecondCycle() throws {
        let machine = AutomaticSchedulingStateMachine()
        let keys = ["scope-a", "scope-b", "scope-c"].map { Self.makeKey($0) }
        var selected: [AutomaticSchedulingKey] = []
        var now: Int64 = 0

        for cycle in 0..<2 {
            for key in keys { try machine.signal(key, priority: .background, at: now) }
            now += AutomaticSchedulingStateMachine.quietPeriodMilliseconds
            for _ in keys {
                let claim = try XCTUnwrap(machine.claim(at: now), "cycle \(cycle)")
                selected.append(claim.key)
                // Each claim finishes before the next selection, so the rotation is what orders
                // them rather than one claim blocking its own key.
                try machine.markPublished(
                    claim.key, generation: claim.generation, intentRef: Self.ref, at: now + 10
                )
                try machine.markCompleted(claim.key, generation: claim.generation, at: now + 20)
                XCTAssertNil(machine.state(of: claim.key))
                now += 100
            }
        }

        // Every key takes a turn before any repeats, and the second cycle wraps to the first key.
        XCTAssertEqual(selected, keys + keys)
        XCTAssertEqual(machine.trackedScopes, 0)
    }

    // MARK: - Transition errors

    func testTransitionErrorsAreRefusedAndLeaveTheClaimWhereItWas() throws {
        let machine = AutomaticSchedulingStateMachine()
        let key = Self.makeKey("scope-a")

        // Publish without a claim: first with no entry at all, then with dirty-but-unclaimed work.
        assertThrows(.notClaimed) {
            try machine.markPublished(key, generation: 1, intentRef: Self.ref, at: 0)
        }
        let generation = try machine.signal(key, priority: .background, at: 0)
        assertThrows(.notClaimed) {
            try machine.markPublished(key, generation: generation, intentRef: Self.ref, at: 1_000)
        }

        XCTAssertEqual(try machine.claim(at: 3_000)?.generation, generation)
        assertThrows(.staleGeneration) {
            try machine.markPublished(key, generation: generation + 1, intentRef: Self.ref, at: 3_100)
        }
        assertThrows(.notPublished) { try machine.markCompleted(key, generation: generation, at: 3_200) }

        try machine.markPublished(key, generation: generation, intentRef: Self.ref, at: 3_300)
        assertThrows(.alreadyPublished) {
            try machine.markPublished(key, generation: generation, intentRef: Self.ref, at: 3_400)
        }
        assertThrows(.staleGeneration) {
            try machine.markCompleted(key, generation: generation + 1, at: 3_500)
        }

        let state = try XCTUnwrap(machine.state(of: key))
        XCTAssertEqual(state.activeGeneration, generation)
        XCTAssertTrue(state.published)

        try machine.markCompleted(key, generation: generation, at: 3_600)
        // The resolved generation is gone, so completing it again is not a stale report but a
        // report about nothing.
        assertThrows(.notClaimed) { try machine.markCompleted(key, generation: generation, at: 3_700) }
        assertThrows(.notClaimed) { try machine.cancel(key, generation: generation, at: 3_800) }
    }

    // MARK: - Malformed input

    func testMalformedAndRegressingTimeAreRefused() throws {
        let machine = AutomaticSchedulingStateMachine()
        let key = Self.makeKey("scope-a")

        assertThrows(.malformedTime) { try machine.signal(key, priority: .user, at: -1) }
        assertThrows(.malformedTime) { _ = try machine.claim(at: .min) }

        try machine.signal(key, priority: .user, at: 1_000)
        assertThrows(.timeRegressed) { try machine.signal(key, priority: .user, at: 999) }
        assertThrows(.timeRegressed) { _ = try machine.claim(at: 0) }

        // The refused calls left the clock where the accepted signal put it.
        XCTAssertNil(try machine.claim(at: 1_000))
        XCTAssertNotNil(try machine.claim(at: 4_000))
    }

    func testMalformedSourceAndScopeRefsAreRefused() throws {
        let machine = AutomaticSchedulingStateMachine()
        let maximum = AutomaticSchedulingStateMachine.maximumRefBytes
        let malformed = [
            "",
            "a/b",
            "/",
            String(repeating: "a", count: maximum + 1),
            // The bound is in bytes, so a ref can be short in characters and still too long.
            String(repeating: "é", count: maximum / 2 + 1),
            "a\u{0}b",
            "a\nb",
            "a\u{7F}b",
        ]

        for value in malformed {
            assertThrows(.malformedIdentity) {
                try machine.signal(Self.makeKey("scope-a", workspace: value), priority: .user, at: 0)
            }
            assertThrows(.malformedIdentity) {
                try machine.signal(Self.makeKey("scope-a", source: value), priority: .user, at: 0)
            }
            assertThrows(.malformedScopeKey) {
                try machine.signal(Self.makeKey(value), priority: .user, at: 0)
            }
        }
        XCTAssertEqual(machine.trackedScopes, 0)

        // The length bound is inclusive, in bytes, for every ref.
        let atBound = String(repeating: "é", count: maximum / 2)
        XCTAssertEqual(atBound.utf8.count, maximum)
        try machine.signal(
            Self.makeKey(atBound, workspace: atBound, source: atBound), priority: .user, at: 0
        )
        XCTAssertEqual(machine.trackedScopes, 1)
    }

    // MARK: - Capacity

    func testScopeCapacityIsBoundedPerSourceAndReusedAfterCompletion() throws {
        let machine = AutomaticSchedulingStateMachine()
        let limit = AutomaticSchedulingStateMachine.maximumScopesPerSource
        for index in 0..<limit {
            try machine.signal(Self.makeKey("scope-\(index)"), priority: .background, at: 0)
        }
        XCTAssertEqual(machine.trackedScopes, limit)
        XCTAssertEqual(machine.trackedSources, 1)

        let overflow = Self.makeKey("scope-overflow")
        assertThrows(.tooManyScopes) { try machine.signal(overflow, priority: .background, at: 0) }
        // A second source is unaffected by one source's scope bound.
        try machine.signal(Self.makeKey("scope-overflow", source: "source-b"), priority: .background, at: 0)

        // Completing the last active generation of a scope drops its entry, which returns its
        // place in the bound.
        let claim = try XCTUnwrap(machine.claim(at: 3_000))
        try machine.markPublished(claim.key, generation: claim.generation, intentRef: Self.ref, at: 3_100)
        try machine.markCompleted(claim.key, generation: claim.generation, at: 3_200)
        XCTAssertNil(machine.state(of: claim.key))
        XCTAssertEqual(machine.trackedScopes, limit)

        try machine.signal(overflow, priority: .background, at: 3_300)
        XCTAssertEqual(machine.trackedScopes, limit + 1)
    }

    func testSourceCapacityIsBoundedAndReusedAfterCompletion() throws {
        let machine = AutomaticSchedulingStateMachine()
        let limit = AutomaticSchedulingStateMachine.maximumTrackedSources
        for index in 0..<limit {
            try machine.signal(
                Self.makeKey("scope-a", source: "source-\(index)"), priority: .background, at: 0
            )
        }
        XCTAssertEqual(machine.trackedSources, limit)

        let overflow = Self.makeKey("scope-a", source: "source-overflow")
        assertThrows(.tooManySources) { try machine.signal(overflow, priority: .background, at: 0) }

        let claim = try XCTUnwrap(machine.claim(at: 3_000))
        try machine.markPublished(claim.key, generation: claim.generation, intentRef: Self.ref, at: 3_100)
        try machine.markCompleted(claim.key, generation: claim.generation, at: 3_200)
        XCTAssertNil(machine.state(of: claim.key))
        XCTAssertEqual(machine.trackedSources, limit - 1)

        try machine.signal(overflow, priority: .background, at: 3_300)
        XCTAssertEqual(machine.trackedSources, limit)

        // At the source bound an already-tracked source can still take another scope: only new
        // sources are refused.
        try machine.signal(
            Self.makeKey("scope-b", source: "source-1"), priority: .background, at: 3_400
        )
        XCTAssertEqual(machine.trackedSources, limit)
        XCTAssertEqual(machine.trackedScopes, limit + 1)
    }

    // MARK: - Identity

    func testIndependentWorkspaceAndSourceIdentitiesDoNotCoalesce() throws {
        let machine = AutomaticSchedulingStateMachine()
        let base = Self.makeKey("scope-a")
        let otherSource = Self.makeKey("scope-a", source: "source-b")
        let otherWorkspace = Self.makeKey("scope-a", workspace: "workspace-b")

        // Same scope key throughout: only the identity differs, and each gets its own generation.
        XCTAssertEqual(try machine.signal(base, priority: .background, at: 0), 1)
        XCTAssertEqual(try machine.signal(otherSource, priority: .background, at: 0), 1)
        XCTAssertEqual(try machine.signal(otherWorkspace, priority: .user, at: 0), 1)
        XCTAssertEqual(machine.trackedScopes, 3)
        XCTAssertEqual(machine.trackedSources, 3)

        let claim = try XCTUnwrap(machine.claim(at: 3_000))
        XCTAssertEqual(claim.key, otherWorkspace)
        for untouched in [base, otherSource] {
            let state = try XCTUnwrap(machine.state(of: untouched))
            XCTAssertEqual(state.dirtyGeneration, 1)
            XCTAssertNil(state.activeGeneration)
        }
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

    private func assertThrows(
        _ expected: AutomaticSchedulingError,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ body: () throws -> Void
    ) {
        XCTAssertThrowsError(try body(), file: file, line: line) {
            XCTAssertEqual($0 as? AutomaticSchedulingError, expected, file: file, line: line)
        }
    }
}
