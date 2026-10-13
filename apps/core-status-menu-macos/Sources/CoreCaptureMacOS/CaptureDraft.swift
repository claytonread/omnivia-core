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
    public var displayName: String { url.lastPathComponent }
}

public struct CaptureDraft: Equatable {
    public static let maximumOrdinaryItems = 10
    public private(set) var items: [CaptureDraftItem] = []
    public var note = ""

    public init() {}

    public mutating func add(
        _ urls: [URL],
        surface: CaptureSurface = .manual,
        source: ManualCaptureCandidate.Source = .original
    ) throws {
        guard items.count + urls.count <= Self.maximumOrdinaryItems else {
            throw CaptureDraftError.tooManyItems
        }
        var additions: [CaptureDraftItem] = []
        for url in urls {
            guard !items.contains(where: { $0.url.standardizedFileURL == url.standardizedFileURL }),
                  !additions.contains(where: { $0.url.standardizedFileURL == url.standardizedFileURL })
            else { throw CaptureDraftError.duplicateItem }
            let values = try? url.resourceValues(forKeys: [.isDirectoryKey, .isRegularFileKey])
            if values?.isDirectory == true {
                guard source == .original else { throw CaptureDraftError.unsupportedItem }
                additions.append(CaptureDraftItem(
                    url: url,
                    kind: .folder,
                    surface: surface,
                    source: source
                ))
            } else if values?.isRegularFile == true,
                      let kind = ManualCaptureSubmission.contentKind(for: url) {
                additions.append(CaptureDraftItem(
                    url: url,
                    kind: .file(kind),
                    surface: surface,
                    source: source
                ))
            } else {
                throw CaptureDraftError.unsupportedItem
            }
        }
        items.append(contentsOf: additions)
    }

    public mutating func add(_ candidates: [ManualCaptureCandidate]) throws {
        for candidate in candidates {
            try add([candidate.url], surface: candidate.surface, source: candidate.source)
        }
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
                source: item.source
            )
        }
    }

    public var containsFolders: Bool { items.contains { $0.kind == .folder } }
    public var containsSnapshots: Bool { items.contains { $0.source.isSnapshot } }
    public var canSubmitFiles: Bool { !fileCandidates.isEmpty && !containsFolders }
}
