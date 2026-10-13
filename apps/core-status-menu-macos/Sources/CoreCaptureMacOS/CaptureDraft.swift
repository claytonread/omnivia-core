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
    public var displayName: String { url.lastPathComponent }
}

public struct CaptureDraft: Equatable {
    public static let maximumOrdinaryItems = 10
    public private(set) var items: [CaptureDraftItem] = []
    public var note = ""

    public init() {}

    public mutating func add(_ urls: [URL]) throws {
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
                additions.append(CaptureDraftItem(url: url, kind: .folder))
            } else if values?.isRegularFile == true,
                      let kind = ManualCaptureSubmission.contentKind(for: url) {
                additions.append(CaptureDraftItem(url: url, kind: .file(kind)))
            } else {
                throw CaptureDraftError.unsupportedItem
            }
        }
        items.append(contentsOf: additions)
    }

    public mutating func remove(at index: Int) {
        guard items.indices.contains(index) else { return }
        items.remove(at: index)
    }

    public var fileCandidates: [ManualCaptureCandidate] {
        items.compactMap { item in
            guard case .file = item.kind else { return nil }
            return ManualCaptureCandidate(url: item.url, displayName: item.displayName)
        }
    }

    public var containsFolders: Bool { items.contains { $0.kind == .folder } }
    public var canSubmitFiles: Bool { !fileCandidates.isEmpty && !containsFolders }
}
