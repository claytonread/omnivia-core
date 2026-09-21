// ============================================================================
// OmniVia Core Settings — readiness coordinator (§8, §12.1).
//
// Owns check selection, scheduling, evidence freshness and generation fencing.
// Passive refresh coalesces simultaneous triggers, bounds each provider, and
// publishes progressively. A late or stale result can never overwrite the
// current view (MT-036): every observation is committed only when its context
// and generation still match.
// ============================================================================

import Foundation

/// What triggered a passive refresh (§8.1).
enum ReadinessRefreshTrigger: Equatable, Sendable {
    case windowOpened
    case foregroundReturn
    case explicitRefresh
    case contextChanged
}

@MainActor
final class ReadinessCoordinator: ObservableObject {
    /// Published for the settings window. Replaced wholesale per generation.
    @Published private(set) var snapshot: ReadinessSnapshot
    @Published private(set) var refreshInFlight: Bool = false

    private let providers: [ReadinessProviding]
    private var context: ReadinessContext
    private var generation: Int = 0
    private var everObserved = false

    /// Coalescing window (§8.2): one refresh per 300 ms of activation churn.
    private let coalesceInterval: TimeInterval
    private var lastRefreshStarted: Date?
    private var coalesceTask: Task<Void, Never>?
    /// Soft deadline per independent provider (§8.2). Presentation deadline,
    /// not proof the underlying call was cancelled.
    private let providerTimeout: TimeInterval
    private let maxConcurrentProviders = 4

    /// Local, in-memory only (§14.1). Nothing is persisted to disk.
    private var lastObserved: [ReadinessCheckID: ReadinessCheck] = [:]

    init(
        providers: [ReadinessProviding] = ReadinessProviderRegistry.providers(),
        context: ReadinessContext,
        coalesceInterval: TimeInterval = 0.3,
        providerTimeout: TimeInterval = 2.0
    ) {
        self.providers = providers
        self.context = context
        self.coalesceInterval = coalesceInterval
        self.providerTimeout = providerTimeout
        let baseChecks = providers.map { ReadinessProviderRegistry.baseCheck(for: $0) }
        snapshot = ReadinessSnapshot(
            generation: 0,
            context: context,
            checks: baseChecks,
            observedAt: nil
        )
    }

    /// The window asks for this when the user changes preferences or the
    /// workspace/target changes. Identity revisions are revalidated before any
    /// commit, so results can never cross contexts (MT-036).
    func updateContext(_ newContext: ReadinessContext) {
        guard newContext != context else { return }
        context = newContext
        // Configuration/workspace change invalidates dependent evidence (§14.1):
        // last-observed facts about old subjects must not decorate new ones.
        invalidate(reason: .contextChanged)
        scheduleRefresh(.contextChanged)
    }

    func invalidate(reason: ReadinessRefreshTrigger) {
        lastObserved.removeAll()
        everObserved = false
        snapshot = ReadinessSnapshot(
            generation: snapshot.generation,
            context: context,
            checks: snapshot.checks.map { check in
                ReadinessCheck(
                    checkID: check.checkID,
                    subject: check.subject,
                    availability: check.availability,
                    applicability: check.applicability,
                    userIntent: check.userIntent,
                    observedState: .accessUncertain,
                    evidence: ReadinessEvidence(
                        kind: .unknown,
                        observedAt: nil,
                        ownerRevision: nil,
                        freshness: .invalidated,
                        confidence: "none"
                    ),
                    activity: check.activity,
                    reasonCode: .unsupportedState,
                    allowedActions: check.allowedActions,
                    affectsFeatureRefs: check.affectsFeatureRefs
                )
            },
            observedAt: nil
        )
    }

    /// Passive refresh entry point. Coalesces rapid triggers, runs providers
    /// concurrently with a soft timeout each, and publishes one new snapshot.
    func scheduleRefresh(_ trigger: ReadinessRefreshTrigger) {
        // Coalesce: if a refresh started within the coalescing window, skip.
        if let last = lastRefreshStarted, Date().timeIntervalSince(last) < coalesceInterval {
            return
        }
        coalesceTask?.cancel()
        lastRefreshStarted = Date()
        let targetGeneration = generation + 1
        generation = targetGeneration
        refreshInFlight = true

        let providers = providers
        let context = context
        let timeout = providerTimeout
        let maxConcurrent = maxConcurrentProviders

        coalesceTask = Task { [weak self] in
            var results: [ReadinessCheckID: ReadinessProviderOutcome] = [:]
            // Bounded concurrency (§12.3): at most `maxConcurrentProviders` at once.
            for batch in stride(from: 0, to: providers.count, by: maxConcurrent)
                .map({ Array(providers[$0..<min($0 + maxConcurrent, providers.count)]) }) {
                if Task.isCancelled { return }
                let batchResults = await withTaskGroup(
                    of: (ReadinessCheckID, ReadinessProviderOutcome).self
                ) { group in
                    for provider in batch {
                        group.addTask {
                            // Soft timeout: a hung provider becomes unknown,
                            // it never blocks rendering (MT-003).
                            let outcome = await withTimeout(seconds: timeout) {
                                await provider.read()
                            }
                            return (provider.checkID, outcome)
                        }
                    }
                    var collected: [(ReadinessCheckID, ReadinessProviderOutcome)] = []
                    for await pair in group { collected.append(pair) }
                    return collected
                }
                for pair in batchResults { results[pair.0] = pair.1 }
                // Publish progressively: render what is known so far.
                await MainActor.run { [weak self] in
                    self?.commit(results: results, generation: targetGeneration, context: context)
                }
            }
            await MainActor.run { [weak self] in
                guard let self else { return }
                self.commit(results: results, generation: targetGeneration, context: context, final: true)
            }
        }
    }

    private func commit(
        results: [ReadinessCheckID: ReadinessProviderOutcome],
        generation targetGeneration: Int,
        context resultContext: ReadinessContext,
        final: Bool = false
    ) {
        // Generation + context fencing (§8.3, MT-036): a result from another
        // workspace, installation, session or generation cannot overwrite us.
        guard targetGeneration == generation, resultContext == context else { return }

        let checks = providers.map { provider -> ReadinessCheck in
            let base = ReadinessProviderRegistry.baseCheck(for: provider)
            guard let outcome = results[provider.checkID] else {
                return lastObserved[provider.checkID] ?? pendingCheck(base)
            }
            let check: ReadinessCheck
            switch outcome {
            case .observed(let state, let evidence, let reason):
                check = ReadinessCheck(
                    checkID: base.checkID,
                    subject: base.subject,
                    availability: .supported,
                    applicability: base.applicability,
                    userIntent: base.userIntent,
                    observedState: state,
                    evidence: evidence,
                    activity: .idle,
                    reasonCode: reason,
                    allowedActions: base.allowedActions,
                    affectsFeatureRefs: base.affectsFeatureRefs
                )
            case .unimplemented(let explanation):
                check = unimplementedCheck(base, explanation: explanation)
            case .failed(let code):
                check = failedCheck(base, code: code)
            }
            return check
        }

        var stored: [ReadinessCheckID: ReadinessCheck] = [:]
        for check in checks {
            if check.availability == .supported, check.evidence.freshness == .currentObservation {
                stored[check.checkID] = check
            }
        }
        lastObserved.merge(stored) { _, new in new }
        if !stored.isEmpty { everObserved = true }

        snapshot = ReadinessSnapshot(
            generation: targetGeneration,
            context: context,
            checks: checks,
            observedAt: everObserved ? Date() : nil
        )
        if final { refreshInFlight = false }
    }

    private func pendingCheck(_ base: ReadinessCheck) -> ReadinessCheck {
        ReadinessCheck(
            checkID: base.checkID,
            subject: base.subject,
            availability: base.availability,
            applicability: base.applicability,
            userIntent: base.userIntent,
            observedState: .accessUncertain,
            evidence: ReadinessEvidence(
                kind: .unknown, observedAt: nil, ownerRevision: nil,
                freshness: lastObserved[base.checkID]?.evidence.freshness ?? .unknown,
                confidence: "none"
            ),
            activity: .checking,
            reasonCode: .componentUnavailable,
            allowedActions: base.allowedActions,
            affectsFeatureRefs: base.affectsFeatureRefs
        )
    }

    private func unimplementedCheck(_ base: ReadinessCheck, explanation: String) -> ReadinessCheck {
        ReadinessCheck(
            checkID: base.checkID,
            subject: base.subject,
            availability: .unimplemented,
            applicability: base.applicability,
            userIntent: base.userIntent,
            observedState: .accessUncertain,
            evidence: ReadinessEvidence(
                kind: .unknown, observedAt: nil, ownerRevision: nil,
                freshness: .unknown, confidence: "none"
            ),
            activity: .idle,
            reasonCode: .unsupportedState,
            allowedActions: base.allowedActions,
            affectsFeatureRefs: base.affectsFeatureRefs
        )
    }

    private func failedCheck(_ base: ReadinessCheck, code: ReadinessReasonCode) -> ReadinessCheck {
        ReadinessCheck(
            checkID: base.checkID,
            subject: base.subject,
            availability: .supported,
            applicability: base.applicability,
            userIntent: base.userIntent,
            observedState: .accessUncertain,
            evidence: ReadinessEvidence(
                kind: .unknown, observedAt: nil, ownerRevision: nil,
                freshness: .unknown, confidence: "none"
            ),
            activity: .idle,
            reasonCode: code,
            allowedActions: base.allowedActions,
            affectsFeatureRefs: base.affectsFeatureRefs
        )
    }
}

/// Soft deadline wrapper (§8.2): the await gives up presenting, it does not
/// claim the underlying call was cancelled.
private func withTimeout(
    seconds: TimeInterval,
    _ operation: @escaping @Sendable () async -> ReadinessProviderOutcome
) async -> ReadinessProviderOutcome {
    await withTaskGroup(
        of: ReadinessProviderOutcome.self,
        returning: ReadinessProviderOutcome.self
    ) { group in
        group.addTask { await operation() }
        group.addTask {
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            return .failed(code: .queryTimedOut)
        }
        // First finished child wins; a hung provider yields the timeout outcome
        // (MT-003) without blocking the batch.
        guard let first = await group.next() else { return .failed(code: .queryTimedOut) }
        group.cancelAll()
        return first
    }
}
