import CoreServices
import Foundation

public struct FolderObservationRoot: Equatable, Sendable {
    public let subscriptionID: String
    public let url: URL

    public init(subscriptionID: String, url: URL) {
        self.subscriptionID = subscriptionID
        self.url = url
    }
}

public struct FolderEventSignal: Sendable {
    public let subscriptionIDs: Set<String>
    public let requiresFullReconciliation: Bool

    public init(subscriptionIDs: Set<String>, requiresFullReconciliation: Bool) {
        self.subscriptionIDs = subscriptionIDs
        self.requiresFullReconciliation = requiresFullReconciliation
    }
}

private final class FolderEventCallbackBox: @unchecked Sendable {
    let roots: [FolderObservationRoot]
    let handler: @Sendable (FolderEventSignal) -> Void

    init(
        roots: [FolderObservationRoot],
        handler: @escaping @Sendable (FolderEventSignal) -> Void
    ) {
        self.roots = roots
        self.handler = handler
    }
}

/// One process-owned FSEvents hint stream for active watched roots. Correctness never depends on
/// event history: dropped/coalesced/root-change signals become a durable full-scan obligation.
public final class FolderEventObserver: @unchecked Sendable {
    private let stream: FSEventStreamRef
    private let callbackBox: Unmanaged<FolderEventCallbackBox>

    public init?(
        roots: [FolderObservationRoot],
        handler: @escaping @Sendable (FolderEventSignal) -> Void
    ) {
        guard !roots.isEmpty else { return nil }
        let box = FolderEventCallbackBox(roots: roots, handler: handler)
        callbackBox = Unmanaged.passRetained(box)
        var context = FSEventStreamContext(
            version: 0,
            info: callbackBox.toOpaque(),
            retain: nil,
            release: nil,
            copyDescription: nil
        )
        let paths = roots.map { $0.url.path } as CFArray
        let flags = FSEventStreamCreateFlags(
            kFSEventStreamCreateFlagUseCFTypes
                | kFSEventStreamCreateFlagFileEvents
                | kFSEventStreamCreateFlagWatchRoot
        )
        guard let created = FSEventStreamCreate(
            nil,
            Self.callback,
            &context,
            paths,
            FSEventStreamEventId(kFSEventStreamEventIdSinceNow),
            0.25,
            flags
        ) else {
            callbackBox.release()
            return nil
        }
        stream = created
        FSEventStreamSetDispatchQueue(stream, DispatchQueue(label: "com.omnivia.core.folder-events"))
        guard FSEventStreamStart(stream) else {
            FSEventStreamInvalidate(stream)
            FSEventStreamRelease(stream)
            callbackBox.release()
            return nil
        }
    }

    deinit {
        FSEventStreamStop(stream)
        FSEventStreamInvalidate(stream)
        FSEventStreamRelease(stream)
        callbackBox.release()
    }

    private static let callback: FSEventStreamCallback = {
        _, info, eventCount, eventPaths, eventFlags, _ in
        guard let info else { return }
        let box = Unmanaged<FolderEventCallbackBox>.fromOpaque(info).takeUnretainedValue()
        let paths = unsafeBitCast(eventPaths, to: NSArray.self) as? [String] ?? []
        var affected = Set<String>()
        var full = false
        for index in 0..<eventCount {
            let flags = eventFlags[index]
            if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagMustScanSubDirs) != 0
                || flags & FSEventStreamEventFlags(kFSEventStreamEventFlagUserDropped) != 0
                || flags & FSEventStreamEventFlags(kFSEventStreamEventFlagKernelDropped) != 0
                || flags & FSEventStreamEventFlags(kFSEventStreamEventFlagEventIdsWrapped) != 0
                || flags & FSEventStreamEventFlags(kFSEventStreamEventFlagRootChanged) != 0
            {
                full = true
            }
            guard index < paths.count else { continue }
            let eventPath = URL(fileURLWithPath: paths[index]).standardizedFileURL.pathComponents
            for root in box.roots {
                let rootPath = root.url.standardizedFileURL.pathComponents
                if eventPath.count >= rootPath.count,
                   Array(eventPath.prefix(rootPath.count)) == rootPath
                {
                    affected.insert(root.subscriptionID)
                }
            }
        }
        if full && affected.isEmpty {
            affected = Set(box.roots.map(\.subscriptionID))
        }
        guard !affected.isEmpty else { return }
        box.handler(
            FolderEventSignal(
                subscriptionIDs: affected,
                requiresFullReconciliation: full
            )
        )
    }
}
