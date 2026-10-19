import CoreCaptureMacOS
import CoreGraphics
import Darwin
import DocumentIntakeMacOS
import Foundation

/// What the status-item drop target accepts, and how much of it. Every value
/// here is a declared count or a length: nothing in this policy opens a file,
/// so a drag that only hovers is decided without reading one byte.
enum StatusItemDragPolicy {
    /// Spring-loading delay before the drop affordance appears, so a drag that
    /// merely crosses the menu bar never lights the icon up. The affordance is
    /// a nonactivating floating panel that ignores mouse events, so the drag
    /// the person is still holding is never interrupted.
    static let hoverAffordanceDelay: TimeInterval = 0.4

    /// How long one drag's promised files may take to arrive. AppKit cannot
    /// make a drag source deliver, so a provider that goes quiet is bounded
    /// here rather than left holding a temporary directory for the session.
    /// Expiry abandons the drag whole; nothing is retried.
    static let promisedDeliveryTimeout: TimeInterval = 30

    /// File URLs are reviewed directly, so one drag carries what one review
    /// draft holds.
    static let maximumFileURLs = CaptureIntakePolicy.maximumSelectionItems

    /// Promised files become captured copies, bounded by the owned-copy store.
    static let maximumPromisedFiles = OwnedSnapshotStore.maximumItems

    /// Dropped text and web links arrive one at a time and become one captured
    /// copy each.
    static let maximumTextItems = 1

    /// A dropped string is held in memory before it is written, so it is
    /// bounded to the same megabyte the text boundary admits.
    static let maximumTextBytes = 1_024 * 1_024

    /// True when this drag's declared contents are within bounds and name
    /// something review can take. File URLs win over promises, and promises
    /// over text, so a Finder drag that also offers a filename as a string is
    /// reviewed as the file it is.
    static func admits(fileURLs: Int, promisedFiles: Int, texts: [String]) -> Bool {
        if fileURLs > 0 {
            return fileURLs <= maximumFileURLs
        }
        if promisedFiles > 0 {
            return promisedFiles <= maximumPromisedFiles
        }
        guard !texts.isEmpty, texts.count <= maximumTextItems else { return false }
        return texts.allSatisfy { !$0.isEmpty && $0.utf8.count <= maximumTextBytes }
    }

    /// The files a promise actually delivered, accepted only once their facts
    /// are read from disk: each must be a regular file lying directly in the
    /// directory this process made for the drag, and the lengths must fit the
    /// owned-copy store's per-item and whole-selection bounds. Facts come from
    /// `lstat`, so a symlink is refused rather than followed and a directory,
    /// device or socket is refused rather than copied. One bad delivery refuses
    /// the drag whole: a partly accepted promise is not the selection the
    /// person dragged.
    static func acceptedPromisedFiles(_ urls: [URL], destination: URL) -> [URL]? {
        guard !urls.isEmpty, urls.count <= maximumPromisedFiles else { return nil }
        // Containment is decided on the resolved parent directory, because a
        // provider and this process may spell the same temporary directory
        // differently. The final component is left unresolved so that `lstat`
        // still sees a link as a link.
        let root = destination.resolvingSymlinksInPath().standardizedFileURL
        var total: UInt64 = 0
        var accepted: [URL] = []
        for url in urls {
            let candidate = url.standardizedFileURL
            let parent = candidate.deletingLastPathComponent()
                .resolvingSymlinksInPath()
                .standardizedFileURL
            guard parent == root else { return nil }
            var status = stat()
            guard Darwin.lstat(candidate.path, &status) == 0,
                  status.st_mode & S_IFMT == S_IFREG,
                  status.st_size >= 0
            else { return nil }
            let bytes = UInt64(status.st_size)
            guard bytes <= OwnedSnapshotStore.maximumItemBytes,
                  total + bytes <= OwnedSnapshotStore.maximumSelectionBytes
            else { return nil }
            total += bytes
            accepted.append(candidate)
        }
        return accepted
    }

    /// What the drop affordance says for one hovering drag: a fixed invitation
    /// and the count the pasteboard declared. The count is pasteboard metadata,
    /// so these words never name a file and no item is loaded to produce them.
    static func affordanceLabel(declaredItems: Int) -> String {
        let count = max(1, declaredItems)
        return count == 1
            ? "Drop to add to Core — 1 item"
            : "Drop to add to Core — \(count) items"
    }

    /// Places the affordance just under the status item, centred on it and kept
    /// inside the part of the screen the person can see.
    static func affordanceFrame(size: CGSize, anchor: CGRect, visible: CGRect) -> CGRect {
        let gap: CGFloat = 6
        let maximumX = max(visible.minX, visible.maxX - size.width)
        let maximumY = max(visible.minY, visible.maxY - size.height)
        return CGRect(
            origin: CGPoint(
                x: min(max(anchor.midX - size.width / 2, visible.minX), maximumX),
                y: min(max(anchor.minY - gap - size.height, visible.minY), maximumY)
            ),
            size: size
        )
    }
}

/// One drag's promised deliveries, held as plain state so the order and the
/// exactly-once reporting are decided without opening a file. The collector
/// reports the flattened delivery once every promise has settled, and once
/// only: a late callback, or one arriving after the timeout gave up, is
/// ignored rather than reported as a second selection.
struct PromisedFileCollector {
    /// How many files each receiver promised, in the order the pasteboard
    /// offered them.
    private let expected: [Int]
    /// What each receiver delivered, in the order that receiver delivered it.
    /// AppKit reads one receiver's promises in its own `fileNames` order, so
    /// flattening these by receiver reproduces the promised source order rather
    /// than the order the providers happened to finish in.
    private var delivered: [[URL]]
    /// How many of each receiver's promises have settled, delivered or failed.
    private var settledPerReceiver: [Int]
    private var settled = 0
    private var failed = false
    private var reported = false

    init(expectedPerReceiver: [Int]) {
        expected = expectedPerReceiver
        delivered = Array(repeating: [], count: expectedPerReceiver.count)
        settledPerReceiver = Array(repeating: 0, count: expectedPerReceiver.count)
    }

    /// How many files this drag's promises say they will deliver.
    var total: Int { expected.reduce(0, +) }

    /// Records one delivery, or one failure when `url` is nil. Returns the
    /// flattened delivery when this call settles the last outstanding promise,
    /// and nil while any is outstanding or once the drag has been reported.
    mutating func receive(receiver: Int, url: URL?) -> [URL]? {
        guard !reported, expected.indices.contains(receiver) else { return nil }
        // A provider that calls back more often than it promised is ignored
        // past its own count, so one receiver can never settle another's.
        guard settledPerReceiver[receiver] < expected[receiver] else { return nil }
        settledPerReceiver[receiver] += 1
        settled += 1
        if let url {
            delivered[receiver].append(url)
        } else {
            failed = true
        }
        guard settled == total else { return nil }
        reported = true
        // A partial promise is not the selection the person dragged. Return an
        // empty completed result so the owner rejects it and cleans its private
        // directory immediately rather than reviewing the successful subset.
        return failed ? [] : delivered.flatMap { $0 }
    }

    /// Gives up on whatever has not arrived. True when this call ends the drag,
    /// false when it was already reported — so the caller cleans up once.
    mutating func abandon() -> Bool {
        guard !reported else { return false }
        reported = true
        return true
    }
}

/// Admits each drag session's drop exactly once. AppKit can hand one session's
/// conclusion to more than one callback, and a redelivered drop must not reach
/// review as a second selection.
struct DragSessionGate {
    private var admitted: Int?

    /// True the first time this drag session is offered, false for every repeat.
    mutating func admit(session: Int) -> Bool {
        guard admitted != session else { return false }
        admitted = session
        return true
    }
}
