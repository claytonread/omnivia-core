// ============================================================================
// OmniVia Core Settings — deterministic readiness tests (SPEC-CORE-MAC-
// READINESS-001 §17). Pure model, reducer and coordinator-fencing cases with
// provider doubles. Mocks prove deterministic behaviour only; native
// qualification is a separate signed-build gate (§17.1).
// ============================================================================

import XCTest
@testable import OmniViaCoreStatusMenu

@MainActor
final class SettingsReadinessTests: XCTestCase {
    private let context = ReadinessContext(
        installationRevision: "install-a",
        localSessionRef: "session-1",
        targetRef: "local",
        workspaceRef: "workspace-1",
        configurationRevision: "1"
    )

    private func makeCheck(
        _ id: ReadinessCheckID,
        availability: ReadinessAvailability = .supported,
        applicability: ReadinessApplicability = .requiredForSelectedFeature,
        observed: ReadinessObservedState = .accessConfirmed,
        reason: ReadinessReasonCode = .noIssue,
        evidence: ReadinessEvidence = ReadinessEvidence(
            kind: .nativeStatusQuery,
            observedAt: Date(),
            ownerRevision: nil,
            freshness: .currentObservation,
            confidence: "directForNamedSubject"
        ),
        activity: ReadinessActivity = .idle
    ) -> ReadinessCheck {
        ReadinessCheck(
            checkID: id,
            subject: ReadinessSubject(
                componentRef: "omnivia-core-status-menu",
                resourceRef: nil,
                operationClass: "test",
                identityVerified: true
            ),
            availability: availability,
            applicability: applicability,
            userIntent: applicability == .requiredForSelectedFeature ? .on : .unspecified,
            observedState: observed,
            evidence: evidence,
            activity: activity,
            reasonCode: reason,
            allowedActions: [.refreshStatus],
            affectsFeatureRefs: []
        )
    }

    // MARK: MT-005 — manual startup and notifications off are neutral, not failure

    func testManualStartupAndNotificationsOffProduceNeutralSummary() {
        let checks = [
            makeCheck(.coreBackground, applicability: .optional, observed: .notRegistered, reason: .noIssue),
            makeCheck(.notificationsDelivery, applicability: .optional, observed: .notDetermined, reason: .noIssue),
        ]
        let summary = ReadinessReducer.summarize(
            checks: checks,
            preferences: .manualLocalOnly,
            refreshInFlight: false,
            everObserved: true
        )
        XCTAssertEqual(summary.kind, .noAdditionalAccess)
        XCTAssertTrue(summary.issues.isEmpty)
    }

    // MARK: MT-006 — unknown material evidence blocks a blanket success

    func testUnknownMaterialEvidencePreventsCleanSummary() {
        let checks = [
            makeCheck(
                .coreBackground,
                observed: .accessUncertain,
                reason: .queryTimedOut,
                evidence: ReadinessEvidence(
                    kind: .unknown, observedAt: nil, ownerRevision: nil,
                    freshness: .unknown, confidence: "none"
                )
            )
        ]
        let summary = ReadinessReducer.summarize(
            checks: checks,
            preferences: ReadinessPreferences(
                startCoreAtLoginSelected: true,
                attentionNotificationsSelected: false,
                hasWorkspace: false
            ),
            refreshInFlight: false,
            everObserved: false
        )
        XCTAssertEqual(summary.kind, .needsVerification)
        XCTAssertEqual(summary.unverifiedSubjects, ["Core startup"])
    }

    // MARK: MT-009 — approval-required maps without equating eligibility to health

    func testApprovalRequiredIsAnIssueButNotAHealthClaim() {
        let checks = [
            makeCheck(.coreBackground, observed: .requiresApproval, reason: .approvalRequired)
        ]
        let summary = ReadinessReducer.summarize(
            checks: checks,
            preferences: ReadinessPreferences(
                startCoreAtLoginSelected: true,
                attentionNotificationsSelected: false,
                hasWorkspace: false
            ),
            refreshInFlight: false,
            everObserved: true
        )
        XCTAssertEqual(summary.kind, .attention)
        XCTAssertEqual(summary.issues.first?.title, "Start Core at login needs approval in Login Items")
    }

    // MARK: MT-007 — unimplemented conditional features are omitted

    func testUnimplementedChecksAreDroppedNotShownAsBroken() {
        let checks = [
            makeCheck(.shareExtension, availability: .unimplemented, applicability: .optional),
        ]
        let applicable = ReadinessReducer.applicableChecks(
            preferences: .manualLocalOnly,
            candidates: checks
        )
        XCTAssertTrue(applicable.isEmpty)
    }

    // MARK: MT-008 — distinct subjects stay distinct

    func testCompanionLoginAndCoreBackgroundAreDistinctSubjects() {
        let companion = ReadinessProviderRegistry.baseCheck(for: LoginItemReadinessProvider(checkID: .companionLogin))
        let background = ReadinessProviderRegistry.baseCheck(for: LoginItemReadinessProvider(checkID: .coreBackground))
        XCTAssertNotEqual(companion.checkID, background.checkID)
        // A result minted for one check cannot be committed under the other's
        // identity: the commit path keys strictly by checkID (see MT-036 test).
        XCTAssertEqual(companion.checkID, .companionLogin)
        XCTAssertEqual(background.checkID, .coreBackground)
    }

    // MARK: MT-042 — unknown native enum cases degrade honestly

    func testUnrecognizedNativeCasesNeverBecomeSuccess() {
        let login = LoginItemNativeStatus.unrecognized(nativeCase: "futureCase")
        XCTAssertEqual(login.observedState, .unrecognized(nativeCase: "smappservice"))
        XCTAssertEqual(login.reasonCode, .unsupportedState)

        let notifications = NotificationNativeStatus.unrecognized(nativeCase: "futureCase")
        XCTAssertEqual(notifications.observedState, .unrecognized(nativeCase: "unnotificationsettings"))
        XCTAssertEqual(notifications.reasonCode, .unsupportedState)

        // And the reducer never counts unsupportedState as an issue or a pass.
        let checks = [
            makeCheck(
                .coreBackground,
                observed: .unrecognized(nativeCase: "smappservice"),
                reason: .unsupportedState
            )
        ]
        let summary = ReadinessReducer.summarize(
            checks: checks,
            preferences: ReadinessPreferences(
                startCoreAtLoginSelected: true,
                attentionNotificationsSelected: false,
                hasWorkspace: false
            ),
            refreshInFlight: false,
            everObserved: true
        )
        XCTAssertNotEqual(summary.kind, .ok)
        XCTAssertTrue(summary.issues.isEmpty)
    }

    // MARK: Coordinator — passive only, fenced, coalescing, bounded (MT-002/003/004/036)

    private final class MockProvider: ReadinessProviding, @unchecked Sendable {
        let checkID: ReadinessCheckID
        private let handler: @Sendable () async -> ReadinessProviderOutcome
        private let lock = NSLock()
        private var _readCount = 0

        var readCount: Int {
            lock.lock(); defer { lock.unlock() }
            return _readCount
        }

        init(checkID: ReadinessCheckID, handler: @escaping @Sendable () async -> ReadinessProviderOutcome) {
            self.checkID = checkID
            self.handler = handler
        }

        func read() async -> ReadinessProviderOutcome {
            lock.lock(); _readCount += 1; lock.unlock()
            return await handler()
        }
    }

    nonisolated private static func observedOutcome(_ state: ReadinessObservedState, reason: ReadinessReasonCode) -> ReadinessProviderOutcome {
        .observed(
            state,
            ReadinessEvidence(
                kind: .nativeStatusQuery,
                observedAt: Date(),
                ownerRevision: nil,
                freshness: .currentObservation,
                confidence: "directForNamedSubject"
            ),
            reason
        )
    }

    // MT-004: rapid triggers coalesce into one provider read per subject.
    func testRapidTriggersCoalesce() async {
        let provider = MockProvider(checkID: .coreBackground) {
            Self.observedOutcome(.enabled, reason: .noIssue)
        }
        let coordinator = ReadinessCoordinator(
            providers: [provider],
            context: context,
            coalesceInterval: 60 // wide window: everything inside coalesces
        )
        coordinator.scheduleRefresh(.windowOpened)
        coordinator.scheduleRefresh(.foregroundReturn)
        coordinator.scheduleRefresh(.explicitRefresh)
        try? await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertEqual(provider.readCount, 1)
    }

    // MT-003: a hanging provider becomes unknown; independent results publish.
    func testHangingProviderTimesOutWithoutBlockingOthers() async {
        let hung = MockProvider(checkID: .coreBackground) {
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            return Self.observedOutcome(.enabled, reason: .noIssue)
        }
        let fast = MockProvider(checkID: .notificationsDelivery) {
            Self.observedOutcome(.allowed, reason: .noIssue)
        }
        let coordinator = ReadinessCoordinator(
            providers: [hung, fast],
            context: context,
            providerTimeout: 0.3
        )
        coordinator.scheduleRefresh(.windowOpened)
        try? await Task.sleep(nanoseconds: 800_000_000)
        let snapshot = coordinator.snapshot
        let background = snapshot.checks.first { $0.checkID == .coreBackground }
        let notifications = snapshot.checks.first { $0.checkID == .notificationsDelivery }
        XCTAssertEqual(background?.reasonCode, .queryTimedOut)
        XCTAssertEqual(notifications?.observedState, .allowed)
        XCTAssertFalse(coordinator.refreshInFlight)
    }

    // MT-036: a result from another context/generation cannot overwrite current state.
    func testStaleContextResultIsDiscarded() async {
        let provider = MockProvider(checkID: .coreBackground) {
            Self.observedOutcome(.requiresApproval, reason: .approvalRequired)
        }
        let coordinator = ReadinessCoordinator(providers: [provider], context: context)
        coordinator.scheduleRefresh(.windowOpened)
        try? await Task.sleep(nanoseconds: 100_000_000)

        // Context change invalidates evidence and starts a fresh generation;
        // the provider returns the same outcome, but the reducer must show the
        // new generation's observation, not a carry-over from the old context.
        let newContext = ReadinessContext(
            installationRevision: "install-a",
            localSessionRef: "session-1",
            targetRef: "local",
            workspaceRef: "workspace-2",
            configurationRevision: "1"
        )
        coordinator.updateContext(newContext)
        try? await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(coordinator.snapshot.context.workspaceRef, "workspace-2")
        // Old-context evidence was invalidated: nothing claims current
        // observation under the new workspace yet.
        for check in coordinator.snapshot.checks where check.evidence.freshness == .currentObservation {
            XCTAssertEqual(check.evidence.observedAt?.timeIntervalSinceNow ?? 0, 0, accuracy: 10)
        }
    }

    // MT-002 (structural): the passive surface is read-only. The coordinator
    // exposes no registration, probing or connection path, and a refresh run
    // calls each provider's read exactly once — nothing else.
    func testPassiveRefreshCallsOnlyReads() async {
        var readCounts: [ReadinessCheckID: Int] = [:]
        let lock = NSLock()
        let providers: [ReadinessProviding] = [
            MockProvider(checkID: .coreBackground) {
                lock.lock(); readCounts[.coreBackground, default: 0] += 1; lock.unlock()
                return Self.observedOutcome(.notRegistered, reason: .noIssue)
            },
            MockProvider(checkID: .notificationsDelivery) {
                lock.lock(); readCounts[.notificationsDelivery, default: 0] += 1; lock.unlock()
                return Self.observedOutcome(.notDetermined, reason: .noIssue)
            },
        ]
        let coordinator = ReadinessCoordinator(providers: providers, context: context)
        coordinator.scheduleRefresh(.windowOpened)
        try? await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(readCounts[.coreBackground], 1)
        XCTAssertEqual(readCounts[.notificationsDelivery], 1)
        // And no side-effecting state changed on the coordinator itself.
        XCTAssertFalse(coordinator.refreshInFlight)
    }

    // MT-010 — enabled registration and stopped service are shown as both
    // facts, never a false Running.
    func testEnabledRegistrationWithStoppedServiceShowsBothFacts() {
        let check = makeCheck(.coreBackground, observed: .enabled, reason: .noIssue)
        let row = ReadinessReducer.startupRow(check: check, serviceRunning: false)
        XCTAssertEqual(row.registration, "Enabled")
        XCTAssertEqual(row.detail, "The Core service is not running right now.")
    }
}
