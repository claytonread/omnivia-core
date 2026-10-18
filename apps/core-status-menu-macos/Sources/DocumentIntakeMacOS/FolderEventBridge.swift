import CoreServices
import Darwin
import Foundation

/// One native FSEvents record: the event ID, the absolute path FSEvents reported, and its flags.
/// The path never leaves the translator; only root-relative components are persisted.
struct FolderNativeEvent: Equatable, Sendable {
    let eventID: UInt64
    let path: String
    let flags: UInt32

    private func has(_ flag: Int) -> Bool { flags & UInt32(flag) != 0 }

    var mustScanSubDirs: Bool { has(kFSEventStreamEventFlagMustScanSubDirs) }
    var dropped: Bool { has(kFSEventStreamEventFlagUserDropped) || has(kFSEventStreamEventFlagKernelDropped) }
    var wrapped: Bool { has(kFSEventStreamEventFlagEventIdsWrapped) }
    var rootChanged: Bool { has(kFSEventStreamEventFlagRootChanged) }
    /// Replay of stored history has finished. It says nothing about the filesystem or captures.
    var historyDone: Bool { has(kFSEventStreamEventFlagHistoryDone) }
    var isFile: Bool { has(kFSEventStreamEventFlagItemIsFile) }
    var isDirectory: Bool { has(kFSEventStreamEventFlagItemIsDir) }
}

/// A registered active root, already canonical (symlinks resolved, as FSEvents reports paths).
struct FolderEventBinding: Equatable, Sendable {
    let subscriptionID: String
    let configGeneration: UInt64
    let root: [String]
    let recursive: Bool
    let includeHidden: Bool

    var path: String { "/" + root.dropFirst().joined(separator: "/") }
}

struct FolderEventTranslation: Equatable {
    var events: [FolderObservationEvent] = []
    /// The batch carried a wrapped-ID flag, so its IDs are not comparable with the covered cursor.
    var wrapped = false
}

/// Pure path-to-obligation mapping. No filesystem, no ledger, no clock.
enum FolderEventTranslator {
    static func translate(_ native: [FolderNativeEvent], bindings: [FolderEventBinding]) -> FolderEventTranslation {
        var result = FolderEventTranslation()
        let ordered = native.enumerated().filter { !$0.element.historyDone }
            .sorted { ($0.element.eventID, $0.offset) < ($1.element.eventID, $1.offset) }
            .map(\.element)
        for event in ordered {
            let components = URL(fileURLWithPath: event.path).standardizedFileURL.pathComponents
            if event.wrapped { result.wrapped = true }
            var full: [(FolderEventBinding, FolderObservationReason)] = []
            if event.dropped { full += bindings.map { ($0, .eventsDropped) } }
            if event.mustScanSubDirs {
                full += affected(components, bindings).map { ($0, .mustScanSubDirs) }
            }
            if event.rootChanged {
                full += affected(components, bindings).map { ($0, .rootChanged) }
            }
            if !full.isEmpty || event.wrapped {
                result.events += full.map { make(event.eventID, $0.0, .full($0.1)) }
                continue
            }
            for binding in bindings where components.starts(with: binding.root) {
                let reason = reason(
                    Array(components.dropFirst(binding.root.count)),
                    directory: event.isDirectory || !event.isFile,
                    binding
                )
                if let reason { result.events.append(make(event.eventID, binding, reason)) }
            }
        }
        return coalesced(result, bindings)
    }

    /// Roots the event path lies in, or that lie in the event path. A path that matches none is a
    /// coalesced signal about the whole stream, so every root owes the scan; the path is not kept.
    private static func affected(_ components: [String], _ bindings: [FolderEventBinding]) -> [FolderEventBinding] {
        let matched = bindings.filter { components.starts(with: $0.root) || $0.root.starts(with: components) }
        return matched.isEmpty ? bindings : matched
    }

    private static func make(
        _ id: UInt64,
        _ binding: FolderEventBinding,
        _ reason: FolderObservationEventReason
    ) -> FolderObservationEvent {
        FolderObservationEvent(
            eventID: id,
            subscriptionID: binding.subscriptionID,
            configGeneration: binding.configGeneration,
            reason: reason
        )
    }

    /// Nil discards the event: the subscription never approved that name or depth.
    private static func reason(
        _ relative: [String],
        directory: Bool,
        _ binding: FolderEventBinding
    ) -> FolderObservationEventReason? {
        guard !relative.isEmpty else { return .full(.mustScanSubDirs) }
        guard binding.includeHidden || !relative.contains(where: { $0.hasPrefix(".") }) else { return nil }
        guard binding.recursive else {
            guard relative.count == 1 else { return nil }
            return FolderObservationLedger.validComponent(relative[0])
                ? .scope(FolderObservationScope(kind: .member, components: relative))
                : .full(.mustScanSubDirs)
        }
        // A name the ledger would refuse, or a path deeper than it records, is still work: widen
        // to the deepest valid ancestor rather than lose it.
        let valid = relative.prefix(FolderObservationLedger.maximumComponentDepth)
            .prefix { FolderObservationLedger.validComponent($0) }
        guard !valid.isEmpty else { return .full(.mustScanSubDirs) }
        let widened = valid.count < relative.count
        return .scope(
            FolderObservationScope(kind: directory || widened ? .subtree : .member, components: Array(valid))
        )
    }

    /// Drops repeats and bounds the batch so one callback is always one atomic ledger write.
    private static func coalesced(_ value: FolderEventTranslation, _ bindings: [FolderEventBinding]) -> FolderEventTranslation {
        var seen = Set<String>()
        var events = Array(value.events.reversed().filter {
            seen.insert("\($0.subscriptionID)\u{0}\($0.reason)").inserted
        }.reversed())
        if events.count > FolderObservationLedger.maximumEventsPerBatch, let last = events.last?.eventID {
            let ids = Set(events.map(\.subscriptionID))
            events = bindings.filter { ids.contains($0.subscriptionID) }
                .map { make(last, $0, .full(.eventsDropped)) }
        }
        return FolderEventTranslation(events: events, wrapped: value.wrapped)
    }
}

/// A live stream the bridge can stop. Tests substitute a recorder.
protocol FolderEventStreamHandle: AnyObject, Sendable {
    func stop()
}

/// Everything the bridge needs from the operating system, so tests need no FSEvents stream.
struct FolderEventPlatform: Sendable {
    /// Canonical root components and the device they live on.
    var locate: @Sendable (URL) -> (components: [String], deviceID: Int64)?
    var streamUUID: @Sendable (Int64) -> String?
    var currentEventID: @Sendable () -> UInt64
    var startStream: @Sendable (
        _ paths: [String],
        _ since: UInt64,
        _ deliver: @escaping @Sendable ([FolderNativeEvent]) -> Void
    ) -> (any FolderEventStreamHandle)?

    static let system = FolderEventPlatform(
        locate: { url in
            var info = stat()
            guard let resolved = Darwin.realpath(url.path, nil) else { return nil }
            defer { free(resolved) }
            let path = String(cString: resolved)
            guard stat(path, &info) == 0 else { return nil }
            return (URL(fileURLWithPath: path).pathComponents, Int64(info.st_dev))
        },
        streamUUID: { device in
            guard let uuid = FSEventsCopyUUIDForDevice(dev_t(truncatingIfNeeded: device)),
                  let text = CFUUIDCreateString(nil, uuid) as String?
            else { return nil }
            return UUID(uuidString: text)?.uuidString
        },
        currentEventID: { FSEventsGetCurrentEventId() },
        startStream: { paths, since, deliver in SystemFolderEventStream(paths: paths, since: since, deliver: deliver) }
    )
}

private final class SystemFolderEventStreamBox: @unchecked Sendable {
    let deliver: @Sendable ([FolderNativeEvent]) -> Void
    init(_ deliver: @escaping @Sendable ([FolderNativeEvent]) -> Void) { self.deliver = deliver }
}

/// One FSEvents stream over the roots of a single device, started from an explicit event ID.
private final class SystemFolderEventStream: FolderEventStreamHandle, @unchecked Sendable {
    private let stream: FSEventStreamRef
    private let box: Unmanaged<SystemFolderEventStreamBox>
    private let lock = NSLock()
    private var stopped = false

    init?(paths: [String], since: UInt64, deliver: @escaping @Sendable ([FolderNativeEvent]) -> Void) {
        box = Unmanaged.passRetained(SystemFolderEventStreamBox(deliver))
        var context = FSEventStreamContext(
            version: 0, info: box.toOpaque(), retain: nil, release: nil, copyDescription: nil
        )
        let flags = FSEventStreamCreateFlags(
            kFSEventStreamCreateFlagUseCFTypes
                | kFSEventStreamCreateFlagFileEvents
                | kFSEventStreamCreateFlagWatchRoot
        )
        guard let created = FSEventStreamCreate(
            nil, Self.callback, &context, paths as CFArray, FSEventStreamEventId(since), 0.25, flags
        ) else {
            box.release()
            return nil
        }
        stream = created
        FSEventStreamSetDispatchQueue(stream, DispatchQueue(label: "com.omnivia.core.folder-events.bridge"))
        guard FSEventStreamStart(stream) else {
            FSEventStreamInvalidate(stream)
            FSEventStreamRelease(stream)
            box.release()
            return nil
        }
    }

    func stop() {
        let first = lock.withLock { () -> Bool in
            defer { stopped = true }
            return !stopped
        }
        guard first else { return }
        FSEventStreamStop(stream)
        FSEventStreamInvalidate(stream)
    }

    deinit {
        stop()
        FSEventStreamRelease(stream)
        box.release()
    }

    private static let callback: FSEventStreamCallback = { _, info, count, eventPaths, eventFlags, eventIDs in
        guard let info else { return }
        let box = Unmanaged<SystemFolderEventStreamBox>.fromOpaque(info).takeUnretainedValue()
        let paths = unsafeBitCast(eventPaths, to: NSArray.self) as? [String] ?? []
        guard paths.count >= count else { return }
        box.deliver((0..<count).map {
            FolderNativeEvent(eventID: eventIDs[$0], path: paths[$0], flags: eventFlags[$0])
        })
    }
}

public enum FolderObservationAttention: Equatable, Sendable {
    /// The ledger refused or failed a write. That device's observer is stopped and nothing it
    /// carried was signalled as covered; the periodic reconciliation remains the safety net.
    case storage(deviceID: Int64)
    /// A root could not be located, or its event stream could not start.
    case streamUnavailable(deviceID: Int64?)
    /// The reconciliation schedule refused or failed to arm the full work a signal owes. What the
    /// batch persisted stands: the observation obligation is not withdrawn and nothing was
    /// signalled as covered. Only the schedule's own arming is missing, so these subscriptions owe
    /// work no periodic tick has been told about yet.
    case schedule(subscriptionIDs: Set<String>)
}

/// Feeds native FSEvents batches into `FolderObservationLedger`. Per device it starts from the
/// persisted covered event ID only while the stream UUID still matches; otherwise it opens a new
/// observation epoch, which owes every subscription a full reconciliation. A batch is persisted
/// in one atomic ledger write before the handler hears of it.
public final class FolderEventBridge: @unchecked Sendable {
    private final class Group: @unchecked Sendable {
        let deviceID: Int64
        let uuid: String?
        let bindings: [FolderEventBinding]
        var epoch: UInt64
        var covered: UInt64
        var disabled = false
        var stream: (any FolderEventStreamHandle)?
        let lock = NSLock()

        init(deviceID: Int64, uuid: String?, bindings: [FolderEventBinding], status: FolderObservationStreamStatus) {
            self.deviceID = deviceID
            self.uuid = uuid
            self.bindings = bindings
            epoch = status.observationEpoch
            covered = status.lastCoveredEventID
        }

        var identity: FolderObservationStreamIdentity {
            FolderObservationStreamIdentity(deviceID: deviceID, streamUUID: uuid)
        }

        var applicable: [FolderObservationApplicableSubscription] {
            bindings.map {
                FolderObservationApplicableSubscription(
                    subscriptionID: $0.subscriptionID, configGeneration: $0.configGeneration
                )
            }
        }
    }

    private let ledger: FolderObservationLedger
    /// Hears every signal before the handler does, so a signal that lost observation is durably
    /// owed as full work and not only as this process's in-memory dirty set.
    private let reconciliation: FolderReconciliationScheduler?
    private let handler: @Sendable (FolderEventSignal) -> Void
    private let onAttention: @Sendable (FolderObservationAttention) -> Void
    private let lock = NSLock()
    private var groups: [Int64: Group] = [:]
    private var attentionValues: [FolderObservationAttention] = []

    public var attention: [FolderObservationAttention] { lock.withLock { attentionValues } }

    /// `roots` are the active folder subscriptions' roots, keyed by their `FolderSubscriptionStore`
    /// IDs. Returns nil when there is nothing to observe.
    public convenience init?(
        roots: [FolderObservationRoot],
        ledger: FolderObservationLedger,
        subscriptions: FolderSubscriptionStore,
        reconciliation: FolderReconciliationScheduler? = nil,
        onAttention: @escaping @Sendable (FolderObservationAttention) -> Void = { _ in },
        handler: @escaping @Sendable (FolderEventSignal) -> Void
    ) {
        self.init(
            roots: roots, ledger: ledger, subscriptions: subscriptions, platform: .system,
            reconciliation: reconciliation, onAttention: onAttention, handler: handler
        )
    }

    init?(
        roots: [FolderObservationRoot],
        ledger: FolderObservationLedger,
        subscriptions: FolderSubscriptionStore,
        platform: FolderEventPlatform,
        reconciliation: FolderReconciliationScheduler? = nil,
        onAttention: @escaping @Sendable (FolderObservationAttention) -> Void = { _ in },
        handler: @escaping @Sendable (FolderEventSignal) -> Void
    ) {
        guard !roots.isEmpty else { return nil }
        self.ledger = ledger
        self.reconciliation = reconciliation
        self.handler = handler
        self.onAttention = onAttention
        var byDevice: [Int64: [FolderEventBinding]] = [:]
        for root in roots {
            guard let subscription = subscriptions.subscription(id: root.subscriptionID),
                  let located = platform.locate(root.url)
            else {
                report(.streamUnavailable(deviceID: nil))
                continue
            }
            byDevice[located.deviceID, default: []].append(
                FolderEventBinding(
                    subscriptionID: root.subscriptionID,
                    configGeneration: subscription.configGeneration,
                    root: located.components,
                    recursive: subscription.recursive,
                    includeHidden: subscription.includeHidden
                )
            )
        }
        for (device, bindings) in byDevice.sorted(by: { $0.key < $1.key }) {
            start(device, bindings.sorted { $0.subscriptionID < $1.subscriptionID }, platform)
        }
    }

    deinit {
        for group in groups.values { group.lock.withLock { group.stream?.stop() } }
    }

    private func start(_ device: Int64, _ bindings: [FolderEventBinding], _ platform: FolderEventPlatform) {
        let uuid = platform.streamUUID(device)
        let current = platform.currentEventID()
        let known = ledger.stream(deviceID: device)
        // Raw IDs mean something only against the same device and UUID, and never beyond "now".
        let resumable = known.map {
            uuid != nil && $0.identity.streamUUID == uuid && $0.lastCoveredEventID > 0
                && $0.lastCoveredEventID <= current
        } ?? false
        let since = resumable ? known?.lastCoveredEventID ?? current : current
        let status: FolderObservationStreamStatus
        do {
            status = try ledger.beginStream(
                identity: FolderObservationStreamIdentity(deviceID: device, streamUUID: uuid),
                startEventID: since,
                subscriptions: bindings.map {
                    FolderObservationApplicableSubscription(
                        subscriptionID: $0.subscriptionID, configGeneration: $0.configGeneration
                    )
                },
                startNewEpoch: !resumable
            )
        } catch {
            report(.storage(deviceID: device))
            return
        }
        let group = Group(deviceID: device, uuid: uuid, bindings: bindings, status: status)
        lock.withLock { groups[device] = group }
        let paths = Array(Set(bindings.map(\.path))).sorted()
        // A fresh epoch already owes a durable full reconciliation. Start native delivery at
        // "now" instead of treating a host-wide current ID as a device-stream cursor; those ID
        // domains can differ and a future numeric cursor can suppress delivery indefinitely.
        let nativeStart = resumable
            ? status.lastCoveredEventID
            : UInt64(kFSEventStreamEventIdSinceNow)
        let stream = platform.startStream(paths, nativeStart) { [weak self] events in
            self?.receive(device, events)
        }
        group.lock.withLock {
            group.stream = stream
            group.disabled = stream == nil
        }
        guard stream != nil else {
            report(.streamUnavailable(deviceID: device))
            return
        }
        // Obligations that survived from an earlier run (or were just opened) still need a
        // reconciliation this process has not been told about.
        let owed = bindings.compactMap { binding in
            ledger.obligation(subscriptionID: binding.subscriptionID).map { (binding.subscriptionID, $0.fullRevision != nil) }
        }
        if !owed.isEmpty {
            dispatch(FolderEventSignal(subscriptionIDs: Set(owed.map(\.0)), requiresFullReconciliation: owed.contains { $0.1 }))
        }
    }

    /// Persists first, signals second. Any ledger failure stops this device's observer.
    func receive(_ device: Int64, _ native: [FolderNativeEvent]) {
        guard let group = lock.withLock({ groups[device] }) else { return }
        var signal: FolderEventSignal?
        var failed = false
        group.lock.withLock {
            guard !group.disabled else { return }
            let translation = FolderEventTranslator.translate(native, bindings: group.bindings)
            // An ID below the cursor without a wrap flag is the same thing: stale history.
            let wrapped = translation.wrapped || translation.events.contains { $0.eventID < group.covered }
            guard wrapped || !translation.events.isEmpty else { return }
            do {
                if wrapped {
                    let status = try ledger.beginStream(
                        identity: group.identity, startEventID: 0,
                        subscriptions: group.applicable, startNewEpoch: true
                    )
                    group.epoch = status.observationEpoch
                    group.covered = status.lastCoveredEventID
                }
                if !translation.events.isEmpty {
                    group.covered = try ledger.ingest(
                        FolderObservationBatch(
                            identity: group.identity, observationEpoch: group.epoch, events: translation.events
                        )
                    )
                }
            } catch {
                group.disabled = true
                group.stream?.stop()
                failed = true
                return
            }
            let affected = wrapped ? group.bindings.map(\.subscriptionID) : translation.events.map(\.subscriptionID)
            signal = FolderEventSignal(
                subscriptionIDs: Set(affected),
                requiresFullReconciliation: wrapped || translation.events.contains {
                    if case .full = $0.reason { return true } else { return false }
                }
            )
        }
        if failed { report(.storage(deviceID: device)) }
        if let signal { dispatch(signal) }
    }

    /// Schedule first, handler second, with no bridge or group lock held: the scheduler takes the
    /// shared publication gate the ledger writes above also take, and the handler reconciles.
    /// A schedule that fails still reaches the handler — this process's reconciliation is the one
    /// thing that can still act on the persisted obligation — but it is reported, never swallowed.
    private func dispatch(_ signal: FolderEventSignal) {
        if let reconciliation {
            do {
                try reconciliation.observe(signal)
            } catch {
                report(.schedule(subscriptionIDs: signal.subscriptionIDs))
            }
        }
        handler(signal)
    }

    private func report(_ value: FolderObservationAttention) {
        lock.withLock { attentionValues.append(value) }
        onAttention(value)
    }
}
