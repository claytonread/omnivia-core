import Foundation
import DocumentIntakeMacOS

public enum CaptureDraftError: Error, Equatable {
    case tooManyItems
    case duplicateItem
    case unsupportedItem
    case folderNeedsReview
}

public enum CaptureDraftItemKind: Equatable {
    case file(CaptureContentKind)
    case folder
    /// Chosen, but not a document this build can capture. It stays in the list
    /// so the person sees what was refused, and leaves only when removed.
    case unsupported
}

public struct CaptureDraftItem: Equatable {
    public let url: URL
    public let kind: CaptureDraftItemKind
    public let surface: CaptureSurface
    public let source: ManualCaptureCandidate.Source
    /// The durable native-intake record that owns this item, when it came from
    /// an authenticated native handoff.
    public let native: NativeCandidateKey?
    public var displayName: String { url.lastPathComponent }

    /// The candidate this row publishes as, when it is an ordinary file. A
    /// folder or a refused item publishes nothing, so it has none.
    public func candidate(keepUpdated: Bool = false) -> ManualCaptureCandidate? {
        guard case .file = kind else { return nil }
        return ManualCaptureCandidate(
            url: url,
            displayName: displayName,
            surface: surface,
            keepUpdated: keepUpdated,
            source: source,
            native: native
        )
    }

    /// Whatever this row still owns, whichever kind it is: a file grant, owned
    /// bytes, or the provisional root grant a native folder row holds — plus
    /// the durable record that names it. Every ownership cleanup path (Remove,
    /// discard, quit) releases these, so a folder row cannot outlive review
    /// still holding a grant. A plainly picked row owns nothing of its own and
    /// has none.
    public var ownedCandidate: ManualCaptureCandidate? {
        guard native != nil || source != .original else { return nil }
        return ManualCaptureCandidate(
            url: url,
            displayName: displayName,
            surface: surface,
            source: source,
            native: native
        )
    }
}

public struct CaptureDraft: Equatable {
    public static let maximumOrdinaryItems = 10
    public private(set) var items: [CaptureDraftItem] = []
    public var note = ""

    public init() {}

    private struct Entry {
        let url: URL
        let surface: CaptureSurface
        let source: ManualCaptureCandidate.Source
        let native: NativeCandidateKey?
    }

    public mutating func add(
        _ urls: [URL],
        surface: CaptureSurface = .manual,
        source: ManualCaptureCandidate.Source = .original
    ) throws {
        try add(entries: urls.map {
            Entry(url: $0, surface: surface, source: source, native: nil)
        })
    }

    /// Adds every candidate or none: a refused selection never leaves some of
    /// its candidates in the draft while the caller releases all of them.
    public mutating func add(_ candidates: [ManualCaptureCandidate]) throws {
        try add(entries: candidates.map {
            Entry(url: $0.url, surface: $0.surface, source: $0.source, native: $0.native)
        })
    }

    /// Classifies a selection in the order it was given and appends it whole.
    /// An item that is not a supported document is kept as an `.unsupported`
    /// row rather than refused — but only when nothing in the selection holds a
    /// grant or owned bytes. A native or snapshot selection stays all-or-
    /// nothing, because its caller releases the whole selection on a throw.
    private mutating func add(entries: [Entry]) throws {
        guard items.count + entries.count <= Self.maximumOrdinaryItems else {
            throw CaptureDraftError.tooManyItems
        }
        let retainsRefused = entries.allSatisfy { $0.native == nil && $0.source == .original }
        var additions: [CaptureDraftItem] = []
        for entry in entries {
            let url = entry.url
            guard !items.contains(where: { $0.url.standardizedFileURL == url.standardizedFileURL }),
                  !additions.contains(where: { $0.url.standardizedFileURL == url.standardizedFileURL })
            else { throw CaptureDraftError.duplicateItem }
            let values = try? url.resourceValues(forKeys: [.isDirectoryKey, .isRegularFileKey])
            let kind: CaptureDraftItemKind
            if values?.isDirectory == true {
                // A folder row is either a plainly picked folder or one adopted
                // from a native handoff as a root grant. A file grant or owned
                // bytes never name a folder.
                guard entry.source == .original || entry.source.isAdoptedFolderRoot else {
                    throw CaptureDraftError.unsupportedItem
                }
                kind = .folder
            } else if values?.isRegularFile == true,
                      !entry.source.isAdoptedFolderRoot,
                      let contentKind = ManualCaptureSubmission.contentKind(for: url) {
                kind = .file(contentKind)
            } else if retainsRefused {
                kind = .unsupported
            } else {
                throw CaptureDraftError.unsupportedItem
            }
            additions.append(CaptureDraftItem(
                url: url,
                kind: kind,
                surface: entry.surface,
                source: entry.source,
                native: entry.native
            ))
        }
        items.append(contentsOf: additions)
    }

    /// The native candidates currently in this draft, by candidate reference.
    public var nativeCandidateRefs: Set<String> {
        Set(items.compactMap { $0.native?.candidateRef })
    }

    /// Takes one row out of review and hands it back, so its caller can release
    /// whatever that row still holds.
    @discardableResult
    public mutating func remove(at index: Int) -> CaptureDraftItem? {
        guard items.indices.contains(index) else { return nil }
        return items.remove(at: index)
    }

    public var fileCandidates: [ManualCaptureCandidate] {
        fileCandidates(keepUpdated: false)
    }

    public func fileCandidates(keepUpdated: Bool) -> [ManualCaptureCandidate] {
        items.compactMap { $0.candidate(keepUpdated: keepUpdated) }
    }

    /// Everything this draft still owns, folder rows included. This is what
    /// ownership cleanup releases; publication uses `fileCandidates`.
    public var ownedCandidates: [ManualCaptureCandidate] {
        items.compactMap(\.ownedCandidate)
    }

    public var containsFolders: Bool { items.contains { $0.kind == .folder } }
    public var containsUnsupported: Bool { items.contains { $0.kind == .unsupported } }
    public var containsSnapshots: Bool { items.contains { $0.source.isSnapshot } }
    /// The ordinary-file group is confirmed on its own. A folder row never
    /// blocks it and is never published with it — each folder is its own group
    /// with its own scope review — but a refused item still needs removing.
    public var canSubmitFiles: Bool {
        !fileCandidates.isEmpty && !containsUnsupported
    }

    /// Takes the ordinary files that were just published out of review and
    /// leaves every other row, in its place and order. Rows are matched by
    /// path, so a file added while publication was in flight stays.
    public mutating func removeSubmitted(_ candidates: [ManualCaptureCandidate]) {
        let published = Set(candidates.map { $0.url.standardizedFileURL })
        items.removeAll {
            guard case .file = $0.kind else { return false }
            return published.contains($0.url.standardizedFileURL)
        }
    }
}
