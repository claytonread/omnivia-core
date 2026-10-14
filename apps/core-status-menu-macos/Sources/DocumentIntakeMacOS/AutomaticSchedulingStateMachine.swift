import Darwin
import Foundation

public enum AutomaticSchedulingError: Error, Equatable {
    case malformedIdentity
    case malformedScopeKey
    /// The intent ref a publication would carry is empty, over the bound, or shaped like a path.
    case malformedIntentRef
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
    /// The durable document is malformed, tampered with, over a bound, or describes a combination
    /// this machine cannot hold. Nothing is rewritten: the file is left exactly as it was found.
    case invalidState
    /// A newer build wrote the document. It is refused before its shape is judged, and left alone.
    case unsupportedSchema
    case stateUnavailable
    case tooLarge
    /// The scope has issued every generation one entry may hold.
    case generationExhausted
}

/// Which queue a signal belongs to. `user` is always selected ahead of `background`.
public enum AutomaticSchedulingPriority: String, Codable, Comparable, Sendable {
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
    /// This work was recovered from the durable store, so the two signal times above are zero
    /// rather than instants in this process's clock: no age may be computed from them. See
    /// `AutomaticSchedulingStateMachine`'s note on restart timing.
    public let restoredFromRestart: Bool
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
    /// The opaque ref naming the published intent in `CaptureIntentQueue`, so a restart can
    /// reconcile the one unresolved intent instead of guessing. Present only while published, and
    /// absent on a legacy publication: see `legacyUnresolvedIntent`.
    public let intentRef: String?
    /// This published intent comes from a document written before publications carried a ref, so
    /// there is nothing to reconcile it against by ref. It still blocks a duplicate claim, so the
    /// caller resolves it by its generation (complete or cancel) rather than repeating the work.
    public let legacyUnresolvedIntent: Bool
    /// This entry was recovered from the durable store. Its signal times are zero, and any dirty
    /// work it holds is due now.
    public let restoredFromRestart: Bool
}

/// The three opaque refs that name one scheduling unit on disk. Nothing else about the source is
/// persisted, because nothing else is held.
private struct AutomaticSchedulingKeyRecord: Codable, Equatable {
    var workspaceRef: String
    var sourceRef: String
    var scopeKey: String

    init(_ key: AutomaticSchedulingKey) {
        workspaceRef = key.source.workspaceRef
        sourceRef = key.source.sourceRef
        scopeKey = key.scopeKey
    }

    var key: AutomaticSchedulingKey {
        AutomaticSchedulingKey(
            source: AutomaticSchedulingSource(workspaceRef: workspaceRef, sourceRef: sourceRef),
            scopeKey: scopeKey
        )
    }
}

/// One tracked scope's durable scheduling truth. No time is persisted: see the restart note on
/// `AutomaticSchedulingStateMachine`.
private struct AutomaticSchedulingEntryRecord: Codable, Equatable {
    var key: AutomaticSchedulingKeyRecord
    /// Unclaimed work. Generation and priority are present together or not at all.
    var dirtyGeneration: UInt64?
    var dirtyPriority: AutomaticSchedulingPriority?
    /// The one claim in flight, published or not. Required when `published`.
    var activeGeneration: UInt64?
    var activePriority: AutomaticSchedulingPriority?
    var published: Bool
    /// The published intent's opaque queue ref. Added after v1 shipped, so it is optional in the
    /// schema: a document written before it shows a publication with neither this nor the marker
    /// below, and is recovered as a legacy unresolved intent.
    var intentRef: String?
    /// Set only on a publication this owner recovered from such a document and then rewrote, so
    /// what it writes keeps saying "published, no ref" without that being a shape a new
    /// publication could reach.
    var legacyIntent: Bool?
}

private struct AutomaticSchedulingDocument: Codable, Equatable {
    var schema: String
    var entries: [AutomaticSchedulingEntryRecord]
    /// Where background rotation stands. It need not name a tracked scope: the entry it names may
    /// since have gone clean, and rotation simply resumes after whatever it names.
    var backgroundCursor: AutomaticSchedulingKeyRecord?
}

private struct AutomaticSchedulingHeader: Codable {
    let schema: String
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
/// No coordinator is called: the caller drives it and owns the clock, which it supplies as integer
/// monotonic milliseconds. A fully clean scope stops being tracked, which returns its place in the
/// bounds; a later signal for it is a first signal again.
///
/// ## Durability and the clock
///
/// Built with a state directory, every tracked entry's generations, priorities, one active claim,
/// published marker and the background rotation cursor are written to an owner-only versioned JSON
/// document after each accepted transition, so no automatic work is dropped or duplicated by a
/// restart. The in-memory initializer persists nothing.
///
/// **No time is persisted.** The clock the caller supplies is process-monotonic milliseconds, whose
/// zero is this process's start; a time written by a previous process names an instant in an epoch
/// that no longer exists, and comparing the two would be meaningless arithmetic rather than an
/// age. So instead of persisting incomparable instants, recovered dirty work is restored as
/// *immediately due*: it is selected the first time the new process looks, whatever `now` it
/// supplies. Its signal times read as zero and `restoredFromRestart` is set, so no caller mistakes
/// them for instants. That is conservative in the only safe direction — recovered work runs no
/// later than it would have before the restart, never later — and the 3-second quiet period and
/// 30-second maximum wait continue to govern every signal raised within one process.
///
/// Recovery also resolves each in-flight claim by what it reached. An unpublished candidate has no
/// process carrying it, so it returns to dirty — same generation if nothing accumulated behind it,
/// the later one if something did — and is immediately eligible rather than stranded. A published
/// one is the scope's one unresolved intent: it stays active and published, still blocking a
/// duplicate claim, until the queue reports its outcome. It also carries across the opaque ref
/// `markPublished` attached, which is what lets the next process find that intent in
/// `CaptureIntentQueue` rather than guess at it. A publication recovered from a document written
/// before the ref existed has none, so it is recovered as an explicitly legacy unresolved intent:
/// still blocking, still resolvable by generation, but with nothing to reconcile by ref.
///
/// Each transition rewrites the whole document. Mutations are debounced and the table is bounded,
/// so this is bounded work; switch to an append-and-compact log if a profile ever shows the
/// rewrite mattering.
public final class AutomaticSchedulingStateMachine: @unchecked Sendable {
    public static let quietPeriodMilliseconds: Int64 = 3_000
    public static let maximumWaitMilliseconds: Int64 = 30_000
    public static let maximumTrackedSources = 256
    public static let maximumScopesPerSource = 64
    public static let maximumRefBytes = 128
    /// Beyond any plausible number of signals for one scope, and the largest integer every JSON
    /// reader round-trips exactly. Bounding it is what keeps a tampered generation from overflowing
    /// the next one.
    public static let maximumGeneration: UInt64 = 1 << 53

    static let schema = "omnivia.automatic-scheduling.v1"
    /// Comfortably above the worst case the bounds allow: `maximumTrackedSources *
    /// maximumScopesPerSource` records, each with four maximum-length refs and two maximum
    /// generations, encode to well under 1024 bytes apiece.
    static let maximumStateBytes = maximumTrackedSources * maximumScopesPerSource * 1024 + 1024

    private struct Pending {
        var generation: UInt64
        var priority: AutomaticSchedulingPriority
        var firstSignal: Int64
        var latestSignal: Int64
        /// Recovered from the durable store, so the two times above belong to a previous process's
        /// monotonic epoch. Never persisted — everything loaded is recovered — and sticky until the
        /// work is claimed, so recovered work is never made to wait out a period it already served.
        var restoredDue = false
    }

    private struct Entry {
        var dirty: Pending?
        var active: Pending?
        var published = false
        /// The published intent's queue ref. Nil while published is a legacy publication, which is
        /// the only way this pair can be published without one.
        var intentRef: String?

        var isLegacyPublication: Bool { published && intentRef == nil }

        /// The dirty record, when there is one, always holds the latest generation issued.
        var lastGeneration: UInt64 { dirty?.generation ?? active?.generation ?? 0 }
        var isClean: Bool { dirty == nil && active == nil }
    }

    private let gate: PublicationGate
    private let directory: URL?
    private let stateFile: URL?
    /// What is on disk right now, so an unchanged transition writes nothing. Nil in memory only.
    private var persisted: AutomaticSchedulingDocument?
    private var entries: [AutomaticSchedulingKey: Entry] = [:]
    private var lastObserved: Int64 = 0
    private var lastBackgroundKey: AutomaticSchedulingKey?

    /// In memory only: nothing is loaded and nothing is written.
    public init() {
        gate = PublicationGate()
        directory = nil
        stateFile = nil
        persisted = nil
    }

    /// Durable: owner-only state under `stateDirectory`, recovered on open.
    ///
    /// Recovery reads and never writes, so a state directory that cannot be written still opens
    /// with its work intact; the first accepted transition is what persists the recovered shape.
    /// Recovery is idempotent, so a crash before that loses nothing.
    public init(stateDirectory: URL, gate: PublicationGate = .processShared) throws {
        self.gate = gate
        directory = stateDirectory
        let file = stateDirectory.appendingPathComponent("automatic-scheduling-v1.json")
        stateFile = file
        try Self.prepareDirectory(stateDirectory)
        let document = try Self.load(file)
            ?? AutomaticSchedulingDocument(schema: Self.schema, entries: [], backgroundCursor: nil)
        persisted = document
        (entries, lastBackgroundKey) = Self.restore(document)
    }

    public var trackedSources: Int { gate.withLock { Set(entries.keys.map(\.source)).count } }
    public var trackedScopes: Int { gate.withLock { entries.count } }

    public func state(of key: AutomaticSchedulingKey) -> AutomaticSchedulingState? {
        gate.withLock {
            entries[key].map {
                AutomaticSchedulingState(
                    dirtyGeneration: $0.dirty?.generation,
                    priority: $0.dirty?.priority,
                    firstSignalMilliseconds: $0.dirty?.firstSignal,
                    latestSignalMilliseconds: $0.dirty?.latestSignal,
                    activeGeneration: $0.active?.generation,
                    published: $0.published,
                    intentRef: $0.intentRef,
                    legacyUnresolvedIntent: $0.isLegacyPublication,
                    restoredFromRestart: $0.dirty?.restoredDue ?? $0.active?.restoredDue ?? false
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
        return try mutate {
            try advance(now)
            if entries[key] == nil { try admit(key) }
            var entry = entries[key] ?? Entry()
            let generation = entry.lastGeneration + 1
            guard generation <= Self.maximumGeneration else {
                throw AutomaticSchedulingError.generationExhausted
            }
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
        try mutate {
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
            entry.intentRef = nil
            entries[chosen.key] = entry
            return AutomaticSchedulingClaim(
                key: chosen.key,
                generation: chosen.dirty.generation,
                priority: chosen.dirty.priority,
                firstSignalMilliseconds: chosen.dirty.firstSignal,
                latestSignalMilliseconds: chosen.dirty.latestSignal,
                claimedMilliseconds: now,
                restoredFromRestart: chosen.dirty.restoredDue
            )
        }
    }

    /// The candidate reached a published intent, which is now the scope's one unresolved intent.
    ///
    /// `intentRef` is the opaque ref that names it in `CaptureIntentQueue`, and it is attached in
    /// the same transition that publishes: a crash leaves either a publication with its ref or no
    /// publication at all, never one the next process cannot reconcile.
    public func markPublished(
        _ key: AutomaticSchedulingKey,
        generation: UInt64,
        intentRef: String,
        at now: Int64
    ) throws {
        guard Self.validRef(intentRef) else { throw AutomaticSchedulingError.malformedIntentRef }
        try mutate {
            try advance(now)
            var entry = try claimed(key, generation).entry
            guard !entry.published else { throw AutomaticSchedulingError.alreadyPublished }
            entry.published = true
            entry.intentRef = intentRef
            entries[key] = entry
        }
    }

    /// The published intent reached a terminal outcome. Its generation is resolved and dropped; any
    /// later dirty generation is left exposed for the next claim.
    public func markCompleted(_ key: AutomaticSchedulingKey, generation: UInt64, at now: Int64) throws {
        try mutate {
            try advance(now)
            var entry = try claimed(key, generation).entry
            guard entry.published else { throw AutomaticSchedulingError.notPublished }
            entry.active = nil
            entry.published = false
            entry.intentRef = nil
            entries[key] = entry.isClean ? nil : entry
        }
    }

    /// Abandons the in-flight generation, published or not, and returns the generation of the dirty
    /// work that is now pending again.
    @discardableResult
    public func cancel(_ key: AutomaticSchedulingKey, generation: UInt64, at now: Int64) throws -> UInt64 {
        try mutate {
            try advance(now)
            var (entry, active) = try claimed(key, generation)
            // Unfinished work goes back to dirty. A generation that accumulated behind it is the
            // later one and keeps its number, but the cancelled generation's signal times survive,
            // so the work stays as due as it already was instead of waiting out a fresh debounce.
            var dirty = entry.dirty ?? active
            dirty.firstSignal = min(dirty.firstSignal, active.firstSignal)
            dirty.latestSignal = max(dirty.latestSignal, active.latestSignal)
            dirty.priority = max(dirty.priority, active.priority)
            // Recovered work that was claimed and then cancelled is still recovered work: it keeps
            // being immediately due rather than silently acquiring this process's clock.
            dirty.restoredDue = dirty.restoredDue || active.restoredDue
            entry.dirty = dirty
            entry.active = nil
            // The abandoned intent is no longer this scope's unresolved one, so its ref goes with
            // it: the caller that cancelled owns whatever the queue still holds under it.
            entry.published = false
            entry.intentRef = nil
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
        // Recovered work carries no time comparable with this process's clock, so neither period
        // can be measured for it and it is due the first time anything looks. See `restoredDue`.
        dirty.restoredDue
            || now - dirty.latestSignal >= quietPeriodMilliseconds
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

    // MARK: - Durability

    /// Applies one transition and persists the result, or leaves both the durable document and
    /// every byte of in-memory state exactly as they were. The clock cursor rolls back too, so a
    /// refused call never moves the machine on.
    private func mutate<T>(_ change: () throws -> T) throws -> T {
        try gate.withLock {
            let entriesBefore = entries
            let observedBefore = lastObserved
            let cursorBefore = lastBackgroundKey
            do {
                let result = try change()
                try persist()
                return result
            } catch {
                entries = entriesBefore
                lastObserved = observedBefore
                lastBackgroundKey = cursorBefore
                throw error
            }
        }
    }

    private func persist() throws {
        guard let stateFile, let directory, let current = persisted else { return }
        let document = snapshot()
        // An unchanged transition — a claim that found nothing due, most often — writes nothing.
        guard document != current else { return }
        try Self.validate(document, allowingLegacyPublished: false)
        try Self.write(document, to: stateFile, directory: directory)
        persisted = document
    }

    private func snapshot() -> AutomaticSchedulingDocument {
        AutomaticSchedulingDocument(
            schema: Self.schema,
            entries: entries.keys.sorted().map { key in
                let entry = entries[key]
                return AutomaticSchedulingEntryRecord(
                    key: AutomaticSchedulingKeyRecord(key),
                    dirtyGeneration: entry?.dirty?.generation,
                    dirtyPriority: entry?.dirty?.priority,
                    activeGeneration: entry?.active?.generation,
                    activePriority: entry?.active?.priority,
                    published: entry?.published ?? false,
                    intentRef: entry?.intentRef,
                    legacyIntent: entry?.isLegacyPublication == true ? true : nil
                )
            },
            backgroundCursor: lastBackgroundKey.map { AutomaticSchedulingKeyRecord($0) }
        )
    }

    /// Rebuilds the in-memory table from a validated document, resolving each in-flight claim by
    /// what it reached. See the restart note on the type.
    private static func restore(
        _ document: AutomaticSchedulingDocument
    ) -> ([AutomaticSchedulingKey: Entry], AutomaticSchedulingKey?) {
        var entries: [AutomaticSchedulingKey: Entry] = [:]
        for record in document.entries {
            var entry = Entry()
            if let generation = record.dirtyGeneration, let priority = record.dirtyPriority {
                entry.dirty = Pending(
                    generation: generation, priority: priority,
                    firstSignal: 0, latestSignal: 0, restoredDue: true
                )
            }
            if let generation = record.activeGeneration, let priority = record.activePriority {
                let active = Pending(
                    generation: generation, priority: priority,
                    firstSignal: 0, latestSignal: 0, restoredDue: true
                )
                if record.published {
                    // The scope's one unresolved intent, wherever it got to. It keeps blocking a
                    // duplicate claim until the queue reports its outcome. A nil ref here is a
                    // publication from before the field existed: `legacyIntent` adds nothing to
                    // recover, since the missing ref is itself what makes it legacy.
                    entry.active = active
                    entry.published = true
                    entry.intentRef = record.intentRef
                } else {
                    // No process is carrying this candidate any more, so it goes back to dirty
                    // exactly as `cancel` would: the later generation if one accumulated behind
                    // it, its own otherwise, and the merged priority.
                    var dirty = entry.dirty ?? active
                    dirty.priority = max(dirty.priority, active.priority)
                    dirty.restoredDue = true
                    entry.dirty = dirty
                }
            }
            entries[record.key.key] = entry
        }
        return (entries, document.backgroundCursor?.key)
    }

    // MARK: - Durable validation

    /// Every rule the in-memory machine upholds, re-checked on the way in and on the way out. A
    /// document that breaks one is refused rather than repaired: this owner cannot tell a tampered
    /// generation from a real one, and guessing would be the thing that duplicates work.
    ///
    /// `allowingLegacyPublished` is set only on the way in, where a document written before
    /// publications carried a ref shows one with neither a ref nor the legacy marker. On the way
    /// out it is not, so no publication this owner writes can reach that shape by accident.
    private static func validate(
        _ document: AutomaticSchedulingDocument,
        allowingLegacyPublished: Bool
    ) throws {
        guard document.schema == schema,
              document.entries.count <= maximumTrackedSources * maximumScopesPerSource,
              document.backgroundCursor.map(validKey) ?? true
        else { throw AutomaticSchedulingError.invalidState }
        var scopesPerSource: [AutomaticSchedulingSource: Int] = [:]
        var seen = Set<AutomaticSchedulingKey>()
        for record in document.entries {
            let key = record.key.key
            guard validKey(record.key), seen.insert(key).inserted,
                  (record.dirtyGeneration == nil) == (record.dirtyPriority == nil),
                  (record.activeGeneration == nil) == (record.activePriority == nil),
                  // A clean scope is not tracked at all, so a record for one is not a state this
                  // machine can hold — and keeping it would spend a place in the bounds.
                  record.dirtyGeneration != nil || record.activeGeneration != nil,
                  // Nothing is published without a claim in flight that published it.
                  !record.published || record.activeGeneration != nil,
                  validPublication(record, allowingLegacy: allowingLegacyPublished),
                  [record.dirtyGeneration, record.activeGeneration]
                      .allSatisfy({ $0.map(validGeneration) ?? true })
            else { throw AutomaticSchedulingError.invalidState }
            // Dirty work accumulated behind the claim, so it is strictly the later generation.
            if let dirty = record.dirtyGeneration, let active = record.activeGeneration,
               dirty <= active {
                throw AutomaticSchedulingError.invalidState
            }
            scopesPerSource[key.source, default: 0] += 1
        }
        guard scopesPerSource.count <= maximumTrackedSources,
              scopesPerSource.values.allSatisfy({ $0 <= maximumScopesPerSource }),
              // One canonical order on disk, so what this owner writes round-trips byte for byte.
              document.entries.indices.dropFirst()
                  .allSatisfy({ document.entries[$0 - 1].key.key < document.entries[$0].key.key })
        else { throw AutomaticSchedulingError.invalidState }
    }

    /// A publication names its one unresolved intent, and only a publication names one: a ref
    /// without a publication is as refused as a publication that should have one and does not.
    private static func validPublication(
        _ record: AutomaticSchedulingEntryRecord,
        allowingLegacy: Bool
    ) -> Bool {
        switch (record.published, record.intentRef, record.legacyIntent == true) {
        case (false, nil, false): return true
        case (true, let ref?, false): return validRef(ref)
        case (true, nil, true): return true
        // Published before the ref existed. Accepted on the way in, refused on the way out.
        case (true, nil, false): return allowingLegacy
        default: return false
        }
    }

    private static func validKey(_ record: AutomaticSchedulingKeyRecord) -> Bool {
        validRef(record.workspaceRef) && validRef(record.sourceRef) && validRef(record.scopeKey)
    }

    /// Generations start at one and are bounded; see `maximumGeneration`.
    private static func validGeneration(_ value: UInt64) -> Bool {
        value >= 1 && value <= maximumGeneration
    }

    // MARK: - Durable persistence

    private static func load(_ stateFile: URL) throws -> AutomaticSchedulingDocument? {
        guard FileManager.default.fileExists(atPath: stateFile.path) else { return nil }
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: stateFile.path)
            guard (attributes[.type] as? FileAttributeType) == .typeRegular,
                  let size = attributes[.size] as? NSNumber,
                  size.uint64Value <= UInt64(maximumStateBytes),
                  let owner = attributes[.ownerAccountID] as? NSNumber,
                  owner.uint32Value == Darwin.getuid(),
                  let permissions = attributes[.posixPermissions] as? NSNumber,
                  permissions.intValue & 0o077 == 0
            else { throw AutomaticSchedulingError.invalidState }
            let data = try Data(contentsOf: stateFile)
            // A newer build's document is refused by schema before its shape is judged.
            let header = try JSONDecoder().decode(AutomaticSchedulingHeader.self, from: data)
            guard header.schema == schema else { throw AutomaticSchedulingError.unsupportedSchema }
            let document = try JSONDecoder().decode(AutomaticSchedulingDocument.self, from: data)
            try validate(document, allowingLegacyPublished: true)
            return document
        } catch let error as AutomaticSchedulingError {
            throw error
        } catch {
            throw AutomaticSchedulingError.invalidState
        }
    }

    private static func write(
        _ document: AutomaticSchedulingDocument,
        to stateFile: URL,
        directory: URL
    ) throws {
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
            let data = try encoder.encode(document)
            guard data.count <= maximumStateBytes else { throw AutomaticSchedulingError.tooLarge }
            let temporary = directory.appendingPathComponent(".automatic-scheduling-\(UUID().uuidString).tmp")
            try data.write(to: temporary, options: [.atomic])
            // Owner-only before publication, so nothing that can throw is left to do after it: the
            // replace or move is the commit point, and `mutate` rolls in-memory state back on a
            // throw. New metadata only, so the published file carries the mode set here.
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: temporary.path)
            if FileManager.default.fileExists(atPath: stateFile.path) {
                _ = try FileManager.default.replaceItemAt(
                    stateFile, withItemAt: temporary, options: [.usingNewMetadataOnly]
                )
            } else {
                try FileManager.default.moveItem(at: temporary, to: stateFile)
            }
            // Non-throwing on purpose: a durable document is already published at this point.
            let descriptor = Darwin.open(directory.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
            if descriptor >= 0 {
                _ = Darwin.fsync(descriptor)
                Darwin.close(descriptor)
            }
        } catch let error as AutomaticSchedulingError {
            throw error
        } catch {
            throw AutomaticSchedulingError.stateUnavailable
        }
    }

    private static func prepareDirectory(_ directory: URL) throws {
        do {
            try FileManager.default.createDirectory(
                at: directory,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let values = try directory.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
            guard values.isDirectory == true, values.isSymbolicLink != true else {
                throw AutomaticSchedulingError.stateUnavailable
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        } catch let error as AutomaticSchedulingError {
            throw error
        } catch {
            throw AutomaticSchedulingError.stateUnavailable
        }
    }
}
