import CryptoKit
import Foundation

/// Whether one member's bytes are already local. Residency is a separate consent: a pass never
/// downloads provider-only content to close a coverage gap, so a member that is not resident stays
/// uncovered — and visibly so.
public enum FolderContentResidency: String, Equatable, Sendable {
    case resident
    case notResident
    /// Should have been local and could not be qualified or opened. Reported uncovered rather than
    /// guessed at, which is what keeps an unqualifiable member from being claimed as verified.
    case unreadable
    /// The volume or file provider behind the root is not there at all, so nothing can be covered.
    case providerUnavailable
}

/// Qualifies residency without hydrating anything. Production passes
/// `FolderReconciliationExecutor.systemResidency`; tests inject outcomes so resident,
/// not-resident, unreadable and provider-unavailable behaviour is proven without depending on real
/// file-provider behaviour or UI.
public typealias FolderContentResidencyProbe = @Sendable (URL) -> FolderContentResidency

/// Why a demand read nothing at all. Every value leaves the demand armed and the durable
/// obligation outstanding: a skip is never a completion.
public enum FolderReconciliationSkip: String, Equatable, Sendable {
    case unknownSubscription
    /// Paused, stopped, waiting for Core, review-required or needs-access. No automatic metadata
    /// or content read is performed for any of them.
    case notActive
    /// Its root group's establishment never finished, so nothing may discover through it yet.
    case notEstablished
    /// The schedule no longer owes this demand, because a pass already reported it. This is what
    /// makes the same demand from two callers one pass rather than two.
    case notOwed
    /// The subscription's state or config generation, or the stream's observation epoch, moved
    /// while the pass was reading. Nothing it saw describes what is owed now.
    case raced
}

/// What one attempted demand did. Nothing here is a promise of coverage on its own: `result`
/// carries the facts and the schedule derives the outcome from them.
public struct FolderReconciliationRun: Equatable, Sendable {
    public let subscriptionID: String
    public let kind: FolderReconciliationKind
    /// What the pass covered, or nil when nothing was read.
    public let result: FolderReconciliationResult?
    /// Why nothing was read, or nil when the pass ran and was reported to the schedule.
    public let skipped: FolderReconciliationSkip?
    /// True only when the obligation revision captured before the pass was acknowledged as fully
    /// covered. False when there was nothing outstanding to acknowledge.
    public let acknowledged: Bool
    /// True when the pass saw membership or content the capture path still owes work for.
    public let drifted: Bool

    public var outcome: FolderReconciliationOutcome? { result?.outcome }

    static func skipped(
        _ subscriptionID: String,
        _ kind: FolderReconciliationKind,
        _ reason: FolderReconciliationSkip
    ) -> Self {
        Self(
            subscriptionID: subscriptionID,
            kind: kind,
            result: nil,
            skipped: reason,
            acknowledged: false,
            drifted: false
        )
    }
}

/// Runs the demands `FolderReconciliationScheduler` armed, and acknowledges only what one pass
/// actually covered.
///
/// The two kinds of work stay distinct, because conflating them is what makes a cheap pass
/// expensive and a truthful one impossible:
///
/// - Metadata is a bounded enumeration of the subscription's own authorised root through
///   `FolderScopePolicy`. It reads no content bytes at all.
/// - Content re-verifies the digests of recorded members whose bytes are already local, through
///   each member's own frozen grant. It never enumerates and never hydrates.
///
/// It queues no capture and rewrites no recorded member: a pass that sees drift sets the
/// subscription's `needsFullReconciliation` flag and leaves the capture path to own the rest.
///
/// Being an actor is the whole of the no-overlap guarantee: one demand is executed at a time, off
/// the main actor, and a demand the first caller already reported is no longer due for the second.
public actor FolderReconciliationExecutor {
    private let accessStore: NativeAccessStore
    private let subscriptions: FolderSubscriptionStore
    private let observation: FolderObservationLedger
    private let scheduler: FolderReconciliationScheduler
    private let reader: any CoordinatedOriginalReading
    private let residency: FolderContentResidencyProbe
    private let controlledStores: [URL]
    private let limits: FolderScopeLimits
    /// The one gate every store this executor finalises against also takes. It is what keeps the
    /// revalidation and the durable writes from interleaving with a pause, a reconfigure or a
    /// second pass; it is not a transaction across their separate files. See `finalize`.
    private let gate: PublicationGate
    /// Fires once, in the window between a pass's last read and the finalization boundary. Tests
    /// install one to land a state or epoch change in exactly that window; production installs
    /// none, so the production path is one nil check.
    private var finalizationHook: (@Sendable () -> Void)?

    init(
        accessStore: NativeAccessStore,
        subscriptions: FolderSubscriptionStore,
        observation: FolderObservationLedger,
        scheduler: FolderReconciliationScheduler,
        reader: any CoordinatedOriginalReading,
        gate: PublicationGate,
        residency: @escaping FolderContentResidencyProbe = FolderReconciliationExecutor.systemResidency,
        controlledStores: [URL] = [],
        limits: FolderScopeLimits = .standard
    ) {
        self.accessStore = accessStore
        self.subscriptions = subscriptions
        self.observation = observation
        self.scheduler = scheduler
        self.reader = reader
        self.gate = gate
        self.residency = residency
        self.controlledStores = controlledStores
        self.limits = limits
    }

    func onFinalization(_ value: @escaping @Sendable () -> Void) { finalizationHook = value }

    /// The production residency qualifier. It reads attributes only: nothing here opens a file,
    /// so no provider placeholder is ever materialised to find out what it was.
    ///
    /// A member is resident only once both questions have an answer that says so. A ubiquitous
    /// item has to publish a current local copy; any item at all has to be free of the kernel's
    /// dataless mark, which is how a File Provider that publishes no ubiquitous status reports
    /// content it has not materialised. An attribute that cannot be read qualifies nothing, so it
    /// is `unreadable` rather than assumed local. An ordinary local file answers both and stays
    /// resident.
    ///
    /// Production never reports `providerUnavailable` from here: a root whose provider is gone
    /// fails at the grant, which is where the metadata pass reports it. A member that cannot be
    /// reached while its root can is this one member being unreadable.
    public static let systemResidency: FolderContentResidencyProbe = { url in
        guard let values = try? url.resourceValues(forKeys: [
            .isUbiquitousItemKey,
            .ubiquitousItemDownloadingStatusKey,
        ]) else { return .unreadable }
        if values.isUbiquitousItem == true {
            guard values.ubiquitousItemDownloadingStatus == .current else { return .notResident }
        }
        // `lstat` answers from the directory entry, without following a link and without asking
        // the provider for content. A path that will not stat is not evidence of anything.
        var attributes = Darwin.stat()
        guard Darwin.lstat(url.path, &attributes) == 0 else { return .unreadable }
        return attributes.st_flags & UInt32(bitPattern: SF_DATALESS) == 0 ? .resident : .notResident
    }

    /// Executes `demands` in order and reports each one. Safe to call with demands that are
    /// already finished or no longer owed: those are reported as skipped, without reading
    /// anything a second time.
    @discardableResult
    public func run(_ demands: [FolderReconciliationScheduler.Demand]) -> [FolderReconciliationRun] {
        demands.map(execute)
    }

    // MARK: - One demand

    private func execute(
        _ demand: FolderReconciliationScheduler.Demand
    ) -> FolderReconciliationRun {
        let id = demand.subscriptionID
        guard let subscription = subscriptions.subscription(id: id) else {
            return .skipped(id, demand.kind, .unknownSubscription)
        }
        guard subscription.state == .active else { return .skipped(id, demand.kind, .notActive) }
        // A subscription whose root group is still only prepared belongs to an establishment that
        // has not finished. Nothing may discover through it, and reporting that as a failed pass
        // would record a coverage truth about work that was never attempted.
        guard FolderMonitoringCoordinator.isEstablished(subscription, in: accessStore) else {
            return .skipped(id, demand.kind, .notEstablished)
        }
        // Two lifecycle entry points can hold the same demand — a wake that arrives on the tick
        // that armed it, a startup whose trigger and schedule both surface it. The schedule is the
        // one owner of what is still owed, so a demand it no longer owes is not read a second
        // time. Overlap cannot happen at all: this is an actor.
        guard scheduler.due().contains(where: { $0.subscriptionID == id && $0.kind == demand.kind })
        else { return .skipped(id, demand.kind, .notOwed) }
        // Captured before any read. Anything merged into the ledger while the pass runs takes a
        // higher revision, so acknowledging this one cannot erase it.
        let configGeneration = subscription.configGeneration
        let revision = observation.obligation(subscriptionID: id)?.revision
        let epoch = observationEpoch(of: id)

        let pass = demand.kind == .metadata
            ? metadataPass(subscription)
            : contentPass(subscription)

        if let hook = finalizationHook {
            finalizationHook = nil
            hook()
        }
        return finalize(
            demand,
            pass: pass,
            configGeneration: configGeneration,
            revision: revision,
            epoch: epoch
        )
    }

    /// Commits what one pass earned, behind one linearization boundary.
    ///
    /// Pause, Stop, a review, a re-grant or a new config generation may have landed while the pass
    /// was reading, and the event stream's epoch may have been invalidated under it. An epoch
    /// change means nothing before it is comparable any more, so what this pass read describes work
    /// that is no longer the work that is owed: the schedule is not told it finished, and nothing
    /// is acknowledged. The demand stays armed for the next tick, which is the conservative half of
    /// the two.
    ///
    /// Revalidating and then committing in two steps would leave exactly the window this guards
    /// against: every one of those mutations takes the shared `PublicationGate`, so one pause
    /// between the recheck and the acknowledgement, or between the acknowledgement and the
    /// completion, would clear an armed demand or record coverage for work that is no longer owed.
    /// Holding the gate across the recheck and every write is what makes that unobservable. Nothing
    /// here enumerates a folder or reads a content byte — the pass is already over — so the gate is
    /// held only for the durable writes.
    ///
    /// The gate is a linearization boundary, not a transaction across three separate durable
    /// files, so what it buys is an ordering rule rather than atomicity: every write that must be
    /// durable before coverage may be claimed happens first, and any refusal returns before the
    /// schedule is told the demand finished — which leaves it armed. So the drift handoff is
    /// written first, the obligation is acknowledged next, and the schedule is completed last.
    /// A failure at any step leaves the earlier writes standing and costs at worst a redundant
    /// pass; it never clears an armed demand for work that was not durably handed over. Nothing
    /// is rolled back across files, and nothing needs to be.
    private func finalize(
        _ demand: FolderReconciliationScheduler.Demand,
        pass: Pass,
        configGeneration: UInt64,
        revision: UInt64?,
        epoch: UInt64?
    ) -> FolderReconciliationRun {
        let id = demand.subscriptionID
        return gate.withLock { () -> FolderReconciliationRun in
            guard let current = subscriptions.subscription(id: id),
                  current.state == .active,
                  current.configGeneration == configGeneration,
                  observationEpoch(of: id) == epoch
            else { return .skipped(id, demand.kind, .raced) }

            if pass.drifted {
                // The whole handoff to the capture path, and the first of the durable writes: this
                // executor queues nothing and rewrites no recorded member, so the flag is the only
                // record that the capture path still owes work here. It is written before anything
                // is acknowledged and before the schedule hears the demand finished, because a
                // handoff that did not reach disk is drift this pass saw and no one else will.
                let handed = try? subscriptions.update(id: id) { value in
                    guard value.state == .active, value.configGeneration == configGeneration else {
                        return
                    }
                    value.needsFullReconciliation = true
                }
                guard handed?.needsFullReconciliation == true else {
                    // The store refused the write, or the subscription moved under it so the
                    // change did not apply. Nothing is acknowledged and the schedule is not told
                    // anything finished, so the demand stays armed and runs again.
                    return FolderReconciliationRun(
                        subscriptionID: id,
                        kind: demand.kind,
                        result: nil,
                        skipped: .raced,
                        acknowledged: false,
                        drifted: true
                    )
                }
            }
            var acknowledged = false
            // Only a metadata pass that enumerated the whole authorised root is the full coverage
            // the obligation asks for. A content pass re-verifies recorded members without
            // re-reading membership, so it never claims it — and a partial, unavailable or failed
            // pass of either kind leaves the obligation exactly where it was.
            if demand.kind == .metadata, pass.result.outcome == .complete,
               let revision, let epoch
            {
                do {
                    try observation.acknowledge(
                        subscriptionID: id,
                        configGeneration: configGeneration,
                        observationEpoch: epoch,
                        completion: .full(throughRevision: revision)
                    )
                } catch {
                    // A stale generation or epoch the ledger tracks separately, or a ledger this
                    // process may no longer write. The schedule is not told the demand finished,
                    // so it stays armed; a drift handoff already written stands and costs at worst
                    // a redundant pass.
                    return FolderReconciliationRun(
                        subscriptionID: id,
                        kind: demand.kind,
                        result: nil,
                        skipped: .raced,
                        acknowledged: false,
                        drifted: pass.drifted
                    )
                }
                acknowledged = true
            }
            guard (try? scheduler.complete(
                subscriptionID: id,
                kind: demand.kind,
                result: pass.result
            )) != nil else {
                // The schedule refused the report, so this demand is still owed and runs again.
                // What was acknowledged stays acknowledged: a redundant pass is extra work, never
                // lost work.
                return FolderReconciliationRun(
                    subscriptionID: id,
                    kind: demand.kind,
                    result: nil,
                    skipped: .raced,
                    acknowledged: acknowledged,
                    drifted: pass.drifted
                )
            }
            return FolderReconciliationRun(
                subscriptionID: id,
                kind: demand.kind,
                result: pass.result,
                skipped: nil,
                acknowledged: acknowledged,
                drifted: pass.drifted
            )
        }
    }

    private struct Pass {
        let result: FolderReconciliationResult
        let drifted: Bool
    }

    // MARK: - Metadata

    /// Bounded enumeration of the one authorised root, at the subscription's own binding
    /// generation. No content byte is read here at all.
    private func metadataPass(_ subscription: FolderSubscription) -> Pass {
        let scan: FolderScopeScan
        do {
            scan = try accessStore.withRootReadAccess(
                rootRef: subscription.rootRef,
                expectedGeneration: subscription.rootBindingGeneration,
                purpose: .memberDiscovery
            ) { root in
                try FolderScopePolicy.scan(
                    root: root,
                    recursive: subscription.recursive,
                    includeHidden: subscription.includeHidden,
                    controlledStores: self.controlledStores,
                    limits: self.limits
                )
            }
        } catch {
            return Pass(result: Self.unreached(error), drifted: false)
        }
        // A bound reached mid-traversal means the pass stopped early, so what it holds is a prefix
        // of the folder rather than the folder. Reported as a pass that did not finish, which can
        // never be read as complete coverage.
        let result = Self.bounded(
            examined: scan.members.count,
            unreadable: scan.coverageFailures.count,
            failed: scan.limit != nil
        )
        // Nothing is inferred from a scan that is not the whole folder: a subtree that could not
        // be listed looks exactly like one the owner deleted.
        return Pass(
            result: result,
            drifted: result.outcome == .complete && Self.drifted(scan, against: subscription)
        )
    }

    /// Membership or recorded metadata the capture path has not caught up with. Byte length is the
    /// same credible-change signal the monitoring scan uses, so nothing here reads content either.
    private static func drifted(
        _ scan: FolderScopeScan,
        against subscription: FolderSubscription
    ) -> Bool {
        let recorded = Dictionary(
            subscription.members.lazy
                .filter { $0.state == .present }
                .map { (pathKey($0.relativeComponents), $0) },
            uniquingKeysWith: { first, _ in first }
        )
        let seen = Set(scan.members.map { pathKey($0.relativeComponents) })
        if recorded.keys.contains(where: { !seen.contains($0) }) { return true }
        return scan.members.contains {
            recorded[pathKey($0.relativeComponents)]?.lastObservedByteLength != $0.byteLength
        }
    }

    // MARK: - Content

    /// Re-verifies the digests of recorded present members whose bytes are already local, each
    /// through its own frozen member grant. A member whose baseline has not been admitted yet has
    /// no recorded digest to verify against: that is the capture path's outstanding work, not this
    /// pass's coverage gap, so it is not part of the population at all.
    private func contentPass(_ subscription: FolderSubscription) -> Pass {
        var examined = 0
        var notResident = 0
        var unreadable = 0
        var drifted = false
        for member in subscription.members
            where member.state == .present && member.lastObservedDigest != nil
        {
            let qualified: (residency: FolderContentResidency, digest: String?)
            do {
                qualified = try accessStore.withReadAccess(
                    accessRef: member.accessRef,
                    expectedGeneration: member.membershipGeneration,
                    expectedRootBindingGeneration: subscription.rootBindingGeneration
                ) { url -> (FolderContentResidency, String?) in
                    // Residency is qualified before anything is opened, so provider-only content
                    // is never hydrated to close a gap.
                    let residency = self.residency(url)
                    guard residency == .resident else { return (residency, nil) }
                    return (
                        .resident,
                        Self.digest(
                            try self.reader.readStableBytes(
                                from: url,
                                maximumBytes: CaptureIntentV3.maximumPayloadBytes
                            )
                        )
                    )
                }
            } catch {
                // A grant that will not resolve, or bytes that will not read, is not evidence
                // about this member either way — least of all that it was deleted.
                unreadable += 1
                continue
            }
            switch qualified.residency {
            case .providerUnavailable:
                // Nothing could be covered at all, so the pass reports no counts.
                return Pass(
                    result: FolderReconciliationResult(examined: 0, providerAvailable: false),
                    drifted: false
                )
            case .notResident:
                notResident += 1
            case .unreadable:
                unreadable += 1
            case .resident:
                guard let digest = qualified.digest else {
                    unreadable += 1
                    continue
                }
                examined += 1
                drifted = drifted || digest != member.lastObservedDigest
            }
        }
        return Pass(
            result: Self.bounded(
                examined: examined,
                unreadable: unreadable,
                notResident: notResident,
                failed: false
            ),
            drifted: drifted
        )
    }

    // MARK: - Facts

    /// A pass that never reached the scope. A grant that cannot resolve is the root not being
    /// there; anything else is this process's own state being wrong, which is a failure rather
    /// than an absent provider.
    private static func unreached(_ error: Error) -> FolderReconciliationResult {
        let unavailable: Bool
        switch error {
        case FolderScopeScanError.rootUnavailable:
            unavailable = true
        case let access as NativeAccessError:
            unavailable = [.staleBookmark, .disconnected, .accessDenied].contains(access)
        default:
            unavailable = false
        }
        return FolderReconciliationResult(
            examined: 0,
            providerAvailable: !unavailable,
            failed: !unavailable
        )
    }

    /// The schedule refuses counts whose total describes more items than one scope may hold, and
    /// coverage failures are not themselves bounded by that limit: a root where every entry is
    /// unreadable reports more failures than the scope has eligible files. Clamping keeps the
    /// report recordable, and it only ever shrinks what the pass claims to have covered.
    static func bounded(
        examined: Int,
        unreadable: Int,
        notResident: Int = 0,
        failed: Bool
    ) -> FolderReconciliationResult {
        let limit = FolderReconciliationScheduler.maximumCount
        let notResident = min(notResident, limit)
        let unreadable = min(unreadable, limit - notResident)
        return FolderReconciliationResult(
            examined: min(examined, limit - notResident - unreadable),
            notResident: notResident,
            unreadable: unreadable,
            failed: failed
        )
    }

    private func observationEpoch(of subscriptionID: String) -> UInt64? {
        observation.streams()
            .first { $0.subscriptions.contains { $0.subscriptionID == subscriptionID } }?
            .observationEpoch
    }

    private static func pathKey(_ components: [String]) -> String {
        components.joined(separator: "\u{0}")
    }

    private static func digest(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
