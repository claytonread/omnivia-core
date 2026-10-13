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

    private mutating func add(entries: [Entry]) throws {
        guard items.count + entries.count <= Self.maximumOrdinaryItems else {
            throw CaptureDraftError.tooManyItems
        }
        var additions: [CaptureDraftItem] = []
        for entry in entries {
            let url = entry.url
            guard !items.contains(where: { $0.url.standardizedFileURL == url.standardizedFileURL }),
                  !additions.contains(where: { $0.url.standardizedFileURL == url.standardizedFileURL })
            else { throw CaptureDraftError.duplicateItem }
            let values = try? url.resourceValues(forKeys: [.isDirectoryKey, .isRegularFileKey])
            if values?.isDirectory == true {
                guard entry.source == .original else { throw CaptureDraftError.unsupportedItem }
                additions.append(CaptureDraftItem(
                    url: url,
                    kind: .folder,
                    surface: entry.surface,
                    source: entry.source,
                    native: entry.native
                ))
            } else if values?.isRegularFile == true,
                      let kind = ManualCaptureSubmission.contentKind(for: url) {
                additions.append(CaptureDraftItem(
                    url: url,
                    kind: .file(kind),
                    surface: entry.surface,
                    source: entry.source,
                    native: entry.native
                ))
            } else {
                throw CaptureDraftError.unsupportedItem
            }
        }
        items.append(contentsOf: additions)
    }

    /// The native candidates currently in this draft, by candidate reference.
    public var nativeCandidateRefs: Set<String> {
        Set(items.compactMap { $0.native?.candidateRef })
    }

    public mutating func remove(at index: Int) {
        guard items.indices.contains(index) else { return }
        items.remove(at: index)
    }

    public var fileCandidates: [ManualCaptureCandidate] {
        fileCandidates(keepUpdated: false)
    }

    public func fileCandidates(keepUpdated: Bool) -> [ManualCaptureCandidate] {
        items.compactMap { item in
            guard case .file = item.kind else { return nil }
            return ManualCaptureCandidate(
                url: item.url,
                displayName: item.displayName,
                surface: item.surface,
                keepUpdated: keepUpdated,
                source: item.source,
                native: item.native
            )
        }
    }

    public var containsFolders: Bool { items.contains { $0.kind == .folder } }
    public var containsSnapshots: Bool { items.contains { $0.source.isSnapshot } }
    public var canSubmitFiles: Bool { !fileCandidates.isEmpty && !containsFolders }
}
