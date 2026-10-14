import Foundation

/// Work the driver owed and could not arm. Nothing here is a completion: an attention value says
/// the schedule was not told about work, never that the work happened.
public enum FolderReconciliationAttention: Equatable, Sendable {
    /// An immediate trigger could not be armed. The subscriptions still owe it, and no later tick
    /// will re-derive it, so this is the only place it surfaces.
    case trigger(FolderReconciliationTrigger)
    /// The due schedule could not be armed. Whatever was already armed stands; what came due on
    /// this tick did not land.
    case schedule
}

/// Turns the companion's lifecycle into `FolderReconciliationScheduler` calls.
///
/// It decides *when* to ask, never *what* is owed: the scheduler keeps the filter on paused,
/// stopped and not-yet-active subscriptions, so this hands it every registered subscription ID and
/// lets the one owner of that rule apply it. Every entry point is idempotent, because the
/// scheduler leaves already-armed work alone.
///
/// This schedules; it reconciles nothing and acknowledges nothing.
public final class FolderReconciliationDriver: @unchecked Sendable {
    /// How often the host should call `tick()`. It is far below the shortest interval the schedule
    /// owns, so a due instant is reached by its own interval rather than stretched to the host
    /// timer's period. A tick with nothing due performs no durable write.
    public static let tickInterval: TimeInterval = 60

    private let subscriptions: FolderSubscriptionStore
    private let scheduler: FolderReconciliationScheduler
    private let onAttention: @Sendable (FolderReconciliationAttention) -> Void
    private let lock = NSLock()
    private var attentionValues: [FolderReconciliationAttention] = []

    public var attention: [FolderReconciliationAttention] { lock.withLock { attentionValues } }

    public init(
        subscriptions: FolderSubscriptionStore,
        scheduler: FolderReconciliationScheduler,
        onAttention: @escaping @Sendable (FolderReconciliationAttention) -> Void = { _ in }
    ) {
        self.subscriptions = subscriptions
        self.scheduler = scheduler
        self.onAttention = onAttention
    }

    /// Arms `.startup`, then surfaces everything the persisted schedule already owed. The tick runs
    /// even when the trigger failed: surfacing due work is the conservative half of the two.
    @discardableResult
    public func startup() -> [FolderReconciliationScheduler.Demand] {
        request(.startup)
        return tick()
    }

    @discardableResult
    public func wake() -> [FolderReconciliationScheduler.Demand] { request(.wake) }

    @discardableResult
    public func remount() -> [FolderReconciliationScheduler.Demand] { request(.remount) }

    /// The periodic tick. Nothing due means no durable write, so this is cheap to call often — and
    /// it must be called more often than the shortest interval, or a due instant waits for the
    /// next tick rather than for its own.
    @discardableResult
    public func tick() -> [FolderReconciliationScheduler.Demand] {
        do {
            return try scheduler.schedule()
        } catch {
            report(.schedule)
            return []
        }
    }

    @discardableResult
    private func request(
        _ trigger: FolderReconciliationTrigger
    ) -> [FolderReconciliationScheduler.Demand] {
        let ids = Set(subscriptions.all().map(\.subscriptionID))
        guard !ids.isEmpty else { return [] }
        do {
            return try scheduler.request(subscriptionIDs: ids, trigger: trigger)
        } catch {
            report(.trigger(trigger))
            return []
        }
    }

    private func report(_ value: FolderReconciliationAttention) {
        lock.withLock { attentionValues.append(value) }
        onAttention(value)
    }
}
