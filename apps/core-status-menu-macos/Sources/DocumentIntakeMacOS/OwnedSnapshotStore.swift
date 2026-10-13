import Foundation

public enum OwnedSnapshotStoreError: Error, Equatable {
    case emptySelection
    case tooManyItems
    case itemTooLarge
    case selectionTooLarge
    case unreadableInput
    case invalidFilename
}

public enum OwnedSnapshotInput: Sendable, Equatable {
    case file(URL, suggestedFilename: String? = nil)
    case data(Data, suggestedFilename: String)
}

/// Owns short-lived Share and Shortcuts representations before their provider
/// lifetime ends. These files are captured copies, never persistent originals.
public final class OwnedSnapshotStore: @unchecked Sendable {
    public static let maximumItems = 10
    public static let maximumItemBytes: UInt64 = 50 * 1_024 * 1_024
    public static let maximumSelectionBytes: UInt64 = 200 * 1_024 * 1_024

    private let root: URL
    private let fileManager: FileManager

    public init(root: URL, fileManager: FileManager = .default) throws {
        self.root = root.standardizedFileURL
        self.fileManager = fileManager
        try fileManager.createDirectory(
            at: self.root,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: self.root.path)
    }

    public func stage(_ inputs: [OwnedSnapshotInput]) throws -> [URL] {
        guard !inputs.isEmpty else { throw OwnedSnapshotStoreError.emptySelection }
        guard inputs.count <= Self.maximumItems else { throw OwnedSnapshotStoreError.tooManyItems }

        let request = root.appendingPathComponent(UUID().uuidString.lowercased(), isDirectory: true)
        try fileManager.createDirectory(
            at: request,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )
        var total: UInt64 = 0
        var outputs: [URL] = []
        do {
            for (index, input) in inputs.enumerated() {
                let name: String
                let bytes: UInt64
                let destination: URL
                switch input {
                case let .data(data, suggestedFilename):
                    name = try Self.safeFilename(suggestedFilename, index: index)
                    bytes = UInt64(data.count)
                    try Self.check(bytes: bytes, aggregate: total)
                    destination = request.appendingPathComponent(name, isDirectory: false)
                    try data.write(to: destination, options: [.atomic])
                case let .file(source, suggestedFilename):
                    name = try Self.safeFilename(
                        suggestedFilename ?? source.lastPathComponent,
                        index: index
                    )
                    destination = request.appendingPathComponent(name, isDirectory: false)
                    bytes = try copyBounded(from: source, to: destination, aggregate: total)
                }
                total += bytes
                try fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
                outputs.append(destination)
            }
            return outputs
        } catch {
            try? fileManager.removeItem(at: request)
            throw error
        }
    }

    public func remove(_ urls: [URL]) {
        let rootPath = root.path + "/"
        let requestDirectories = Set(urls.compactMap { url -> URL? in
            let candidate = url.standardizedFileURL
            guard candidate.path.hasPrefix(rootPath) else { return nil }
            return candidate.deletingLastPathComponent()
        })
        for directory in requestDirectories where directory.deletingLastPathComponent() == root {
            try? fileManager.removeItem(at: directory)
        }
    }

    @discardableResult
    public func removeExpired(olderThan age: TimeInterval = 24 * 60 * 60, now: Date = Date()) -> Int {
        guard let children = try? fileManager.contentsOfDirectory(
            at: root,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles]
        ) else { return 0 }
        var removed = 0
        for child in children {
            let modified = (try? child.resourceValues(forKeys: [.contentModificationDateKey]))?
                .contentModificationDate ?? .distantPast
            guard now.timeIntervalSince(modified) >= age else { continue }
            if (try? fileManager.removeItem(at: child)) != nil { removed += 1 }
        }
        return removed
    }

    private func copyBounded(from source: URL, to destination: URL, aggregate: UInt64) throws -> UInt64 {
        let accessed = source.startAccessingSecurityScopedResource()
        defer { if accessed { source.stopAccessingSecurityScopedResource() } }
        guard fileManager.fileExists(atPath: source.path),
              let input = try? FileHandle(forReadingFrom: source)
        else { throw OwnedSnapshotStoreError.unreadableInput }
        fileManager.createFile(atPath: destination.path, contents: nil)
        guard let output = try? FileHandle(forWritingTo: destination) else {
            try? input.close()
            throw OwnedSnapshotStoreError.unreadableInput
        }
        defer {
            try? input.close()
            try? output.close()
        }
        var copied: UInt64 = 0
        while true {
            let chunk = try input.read(upToCount: 1_024 * 1_024) ?? Data()
            if chunk.isEmpty { break }
            copied += UInt64(chunk.count)
            try Self.check(bytes: copied, aggregate: aggregate)
            try output.write(contentsOf: chunk)
        }
        return copied
    }

    private static func check(bytes: UInt64, aggregate: UInt64) throws {
        guard bytes <= maximumItemBytes else { throw OwnedSnapshotStoreError.itemTooLarge }
        guard aggregate + bytes <= maximumSelectionBytes else {
            throw OwnedSnapshotStoreError.selectionTooLarge
        }
    }

    private static func safeFilename(_ proposed: String, index: Int) throws -> String {
        let leaf = (proposed as NSString).lastPathComponent
            .replacingOccurrences(of: "\0", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !leaf.isEmpty, leaf != ".", leaf != "..", !leaf.contains("/") else {
            throw OwnedSnapshotStoreError.invalidFilename
        }
        let stem = (leaf as NSString).deletingPathExtension
        let suffix = (leaf as NSString).pathExtension
        let unique = "\(index + 1)-\(UUID().uuidString.lowercased())-\(stem)"
        return suffix.isEmpty ? unique : "\(unique).\(suffix)"
    }
}
