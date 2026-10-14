import Foundation

public enum AutomaticSchedulingError: Error, Equatable {
    case malformedIdentity
    case malformedScopeKey
    /// A monotonic clock never runs backwards, so a regression is a caller defect, not a late event.
    case timeRegressed
    case malformedTime
    case tooManySources
    case tooManyScopes
    /// There is no claim in flight for this key, so there is nothing to publish, complete or cancel.
    case notClaimed
    /// A claim is in flight, but for a different generation than the caller is reporting on.
    case staleGeneration
    case alreadyPublished
    case notPublished
}

/// Which queue a signal belongs to. `user` is always selected ahead of `background`.
public enum AutomaticSchedulingPriority: String, Comparable, Sendable {
    case background
    case user

    public static func < (lhs: Self, rhs: Self) -> Bool { lhs == .background && rhs == .user }
}

/// An identity the caller has already verified (workspace membership, source ownership). Only the
/// two opaque refs are kept: never a path, a bookmark, a display name or any content.
public struct AutomaticSchedulingSource: Hashable, Comparable, Sendable {
    public let workspaceRef: String
    public let sourceRef: String

    public init(workspaceRef: String, sourceRef: String) {
        self.workspaceRef = workspaceRef
        self.sourceRef = sourceRef
    }

    public static func < (lhs: Self, rhs: Self) -> Bool {
        (lhs.workspaceRef, lhs.sourceRef) < (rhs.workspaceRef, rhs.sourceRef)
    }
}

/// One scheduling unit: a verified source and the scope key the signal is about.
public struct AutomaticSchedulingKey: Hashable, Comparable, Sendable {
    public let source: AutomaticSchedulingSource
    public let scopeKey: String

    public init(source: AutomaticSchedulingSource, scopeKey: String) {
        self.source = source
        self.scopeKey = scopeKey
    }

    /// Ordered by scope key first. Background selection rotates through this order, so every scope
    /// key takes a turn before any of them repeats.
    public static func < (lhs: Self, rhs: Self) -> Bool {
        (lhs.scopeKey, lhs.source) < (rhs.scopeKey, rhs.source)
    }
}

/// The one active, not-yet-published candidate a claim created. The claim is identified by its key
/// and generation; the machine mints no identifiers of its own.
public struct AutomaticSchedulingClaim: Equatable, Sendable {
    public let key: AutomaticSchedulingKey
    public let generation: UInt64
    public let priority: AutomaticSchedulingPriority
    public let firstSignalMilliseconds: Int64
    public let latestSignalMilliseconds: Int64
    public let claimedMilliseconds: Int64
}

public struct AutomaticSchedulingState: Equatable, Sendable {
    /// Unclaimed work. While a claim is in flight this is the one later generation that
    /// accumulated behind it.
    public let dirtyGeneration: UInt64?
    public let priority: AutomaticSchedulingPriority?
    public let firstSignalMilliseconds: Int64?
    public let latestSignalMilliseconds: Int64?
    /// The generation a claim is in flight for, published or not.
    public let activeGeneration: UInt64?
    public let published: Bool
}

/// Decides, in memory only, when a verified source's scope is due for an automatic update.
///
/// Signals coalesce: repeats bump the scope's generation but stay one dirty record, keeping the
/// first-signal time (the maximum wait is measured from it) and tracking the latest-signal time
/// (the quiet period is measured from it). A claim moves that dirty record to the single in-flight
/// slot, so a scope with a claim in flight is never selected again; signals arriving meanwhile
/// accumulate as one later dirty generation. Terminal completion clears the in-flight slot and
/// exposes that later generation; cancellation returns the unfinished work to it.
///
/// Nothing here is persisted and no coordinator is called: the caller drives it and owns the clock,
/// which it supplies as integer monotonic milliseconds. A fully clean scope stops being tracked,
/// which returns its place in the bounds; a later signal for it is a first signal again.
public final class AutomaticSchedulingStateMachine: @unchecked Sendable {
    public static let quietPeriodMilliseconds: Int64 = 3_000
    public static let maximumWaitMilliseconds: Int64 = 30_000
    public static let maximumTrackedSources = 256
    public static let maximumScopesPerSource = 64
    public static let maximumRefBytes = 128

    private struct Pending {
        var generation: UInt64
        var priority: AutomaticSchedulingPriority
        var firstSignal: Int64
        var latestSignal: Int64
    }

    private struct Entry {
        var dirty: Pending?
        var active: Pending?
        var published = false

        /// The dirty record, when there is one, always holds the latest generation issued.
        var lastGeneration: UInt64 { dirty?.generation ?? active?.generation ?? 0 }
        var isClean: Bool { dirty == nil && active == nil }
    }

    private let lock = NSLock()
    private var entries: [AutomaticSchedulingKey: Entry] = [:]
    private var lastObserved: Int64 = 0
    private var lastBackgroundKey: AutomaticSchedulingKey?

    public init() {}

    public var trackedSources: Int { lock.withLock { Set(entries.keys.map(\.source)).count } }
    public var trackedScopes: Int { lock.withLock { entries.count } }

    public func state(of key: AutomaticSchedulingKey) -> AutomaticSchedulingState? {
        lock.withLock {
            entries[key].map {
                AutomaticSchedulingState(
                    dirtyGeneration: $0.dirty?.generation,
                    priority: $0.dirty?.priority,
                    firstSignalMilliseconds: $0.dirty?.firstSignal,
                    latestSignalMilliseconds: $0.dirty?.latestSignal,
                    activeGeneration: $0.active?.generation,
                    published: $0.published
                )
            }
        }
    }

    /// Records a change signal and returns the dirty generation it produced.
    @discardableResult
    public func signal(
        _ key: AutomaticSchedulingKey,
        priority: AutomaticSchedulingPriority,
        at now: Int64
    ) throws -> UInt64 {
        try Self.validate(key)
        return try lock.withLock {
            try advance(now)
            if entries[key] == nil { try admit(key) }
            var entry = entries[key] ?? Entry()
            let generation = entry.lastGeneration + 1
            if var dirty = entry.dirty {
                dirty.generation = generation
                dirty.latestSignal = now
                // A user signal folded into background work carries the whole record up with it.
                dirty.priority = max(dirty.priority, priority)
                entry.dirty = dirty
            } else {
                entry.dirty = Pending(
                    generation: generation, priority: priority, firstSignal: now, latestSignal: now
                )
            }
            entries[key] = entry
            return generation
        }
    }

    /// Claims the one scope that is due now, or nil when none is. User-priority work goes first,
    /// oldest signal first; background work rotates through scope keys.
    public func claim(at now: Int64) throws -> AutomaticSchedulingClaim? {
        try lock.withLock {
            try advance(now)
            let ready: [(key: AutomaticSchedulingKey, dirty: Pending)] = entries.compactMap { key, entry in
                guard entry.active == nil, let dirty = entry.dirty, Self.due(dirty, at: now) else { return nil }
                return (key, dirty)
            }
            guard !ready.isEmpty else { return nil }
            let chosen: (key: AutomaticSchedulingKey, dirty: Pending)
            // The key breaks ties so the choice never depends on dictionary order.
            if let user = ready.filter({ $0.dirty.priority == .user })
                .min(by: { ($0.dirty.firstSignal, $0.key) < ($1.dirty.firstSignal, $1.key) }) {
                chosen = user
            } else {
                let ordered = ready.sorted { $0.key < $1.key }
                chosen = lastBackgroundKey.flatMap { last in ordered.first { $0.key > last } } ?? ordered[0]
                lastBackgroundKey = chosen.key
            }
            var entry = entries[chosen.key] ?? Entry()
            entry.dirty = nil
            entry.active = chosen.dirty
            entry.published = false
            entries[chosen.key] = entry
            return AutomaticSchedulingClaim(
                key: chosen.key,
                generation: chosen.dirty.generation,
                priority: chosen.dirty.priority,
                firstSignalMilliseconds: chosen.dirty.firstSignal,
                latestSignalMilliseconds: chosen.dirty.latestSignal,
                claimedMilliseconds: now
            )
        }
    }

    /// The candidate reached a published intent, which is now the scope's one unresolved intent.
    public func markPublished(_ key: AutomaticSchedulingKey, generation: UInt64, at now: Int64) throws {
        try lock.withLock {
            try advance(now)
            var entry = try claimed(key, generation).entry
            guard !entry.published else { throw AutomaticSchedulingError.alreadyPublished }
            entry.published = true
            entries[key] = entry
        }
    }

    /// The published intent reached a terminal outcome. Its generation is resolved and dropped; any
    /// later dirty generation is left exposed for the next claim.
    public func markCompleted(_ key: AutomaticSchedulingKey, generation: UInt64, at now: Int64) throws {
        try lock.withLock {
            try advance(now)
            var entry = try claimed(key, generation).entry
            guard entry.published else { throw AutomaticSchedulingError.notPublished }
            entry.active = nil
            entry.published = false
            entries[key] = entry.isClean ? nil : entry
        }
    }

    /// Abandons the in-flight generation, published or not, and returns the generation of the dirty
    /// work that is now pending again.
    @discardableResult
    public func cancel(_ key: AutomaticSchedulingKey, generation: UInt64, at now: Int64) throws -> UInt64 {
        try lock.withLock {
            try advance(now)
            var (entry, active) = try claimed(key, generation)
            // Unfinished work goes back to dirty. A generation that accumulated behind it is the
            // later one and keeps its number, but the cancelled generation's signal times survive,
            // so the work stays as due as it already was instead of waiting out a fresh debounce.
            var dirty = entry.dirty ?? active
            dirty.firstSignal = min(dirty.firstSignal, active.firstSignal)
            dirty.latestSignal = max(dirty.latestSignal, active.latestSignal)
            dirty.priority = max(dirty.priority, active.priority)
            entry.dirty = dirty
            entry.active = nil
            entry.published = false
            entries[key] = entry
            return dirty.generation
        }
    }

    private func claimed(
        _ key: AutomaticSchedulingKey,
        _ generation: UInt64
    ) throws -> (entry: Entry, active: Pending) {
        guard let entry = entries[key], let active = entry.active else {
            throw AutomaticSchedulingError.notClaimed
        }
        guard active.generation == generation else { throw AutomaticSchedulingError.staleGeneration }
        return (entry, active)
    }

    private static func due(_ dirty: Pending, at now: Int64) -> Bool {
        now - dirty.latestSignal >= quietPeriodMilliseconds
            || now - dirty.firstSignal >= maximumWaitMilliseconds
    }

    private func advance(_ now: Int64) throws {
        guard now >= 0 else { throw AutomaticSchedulingError.malformedTime }
        guard now >= lastObserved else { throw AutomaticSchedulingError.timeRegressed }
        lastObserved = now
    }

    /// Both bounds are scans over an already-bounded table, so the cost is capped by the bounds.
    private func admit(_ key: AutomaticSchedulingKey) throws {
        let scopes = entries.keys.filter { $0.source == key.source }.count
        guard scopes > 0 || Set(entries.keys.map(\.source)).count < Self.maximumTrackedSources else {
            throw AutomaticSchedulingError.tooManySources
        }
        guard scopes < Self.maximumScopesPerSource else { throw AutomaticSchedulingError.tooManyScopes }
    }

    private static func validate(_ key: AutomaticSchedulingKey) throws {
        guard validRef(key.source.workspaceRef), validRef(key.source.sourceRef) else {
            throw AutomaticSchedulingError.malformedIdentity
        }
        guard validRef(key.scopeKey) else { throw AutomaticSchedulingError.malformedScopeKey }
    }

    /// An opaque ref: non-empty, bounded, and free of control characters and path separators. A ref
    /// shaped like a path is refused rather than stored, because this machine must never hold one.
    private static func validRef(_ value: String) -> Bool {
        guard !value.isEmpty, value.utf8.count <= maximumRefBytes else { return false }
        return !value.unicodeScalars.contains { $0.value < 0x20 || $0.value == 0x7F || $0 == "/" }
    }
}
