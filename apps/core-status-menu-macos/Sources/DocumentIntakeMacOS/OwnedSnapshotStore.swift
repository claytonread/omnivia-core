import CryptoKit
import Darwin
import Foundation

public enum OwnedSnapshotStoreError: Error, Equatable {
    case emptySelection
    case tooManyItems
    case itemTooLarge
    case selectionTooLarge
    case unreadableInput
    case invalidFilename
    case writeFailed
    /// An owned copy a record names is not there.
    case missing
    /// An owned copy is there but is not the bytes the record bound: wrong
    /// type, length or digest. It is never reused.
    case integrityMismatch
}

public enum OwnedSnapshotInput: Sendable, Equatable {
    case file(URL, suggestedFilename: String? = nil)
    case data(Data, suggestedFilename: String)
}

/// What staging wrote: where, and the length and `sha256:<hex>` digest of the
/// bytes it wrote. A caller that records these can later prove a copy is still
/// the same bytes.
public struct OwnedSnapshotReceipt: Equatable, Sendable {
    public let url: URL
    public let byteLength: UInt64
    public let digest: String
}

/// The binding a durable record holds for one owned copy.
public struct OwnedSnapshotExpectation: Equatable, Sendable {
    public let name: String
    public let byteLength: UInt64
    public let digest: String

    public init(name: String, byteLength: UInt64, digest: String) {
        self.name = name
        self.byteLength = byteLength
        self.digest = digest
    }
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

    /// Stages one selection under a fresh, randomly named request directory.
    public func stage(_ inputs: [OwnedSnapshotInput]) throws -> [URL] {
        try stageAll(inputs, requestID: UUID().uuidString.lowercased(), deterministic: false)
            .map(\.url)
    }

    /// Stages one request's snapshots under a caller-chosen request directory
    /// with deterministic filenames, so a durable record can name them before
    /// they exist. Every call writes the bytes afresh: a file already at a
    /// destination was not written under a recorded binding and is replaced,
    /// never trusted. Reuse is `verifiedURLs`, against the receipts returned
    /// here. The caller owns rollback of the request directory on failure.
    public func stage(
        _ inputs: [OwnedSnapshotInput],
        requestID: String
    ) throws -> [OwnedSnapshotReceipt] {
        try stageAll(inputs, requestID: requestID, deterministic: true)
    }

    /// Where `stage(_:requestID:)` places one deterministically named item.
    public func stagedURL(requestID: String, name: String) throws -> URL {
        guard Self.isSafeComponent(requestID), Self.isSafeComponent(name) else {
            throw OwnedSnapshotStoreError.invalidFilename
        }
        return root
            .appendingPathComponent(requestID, isDirectory: true)
            .appendingPathComponent(name, isDirectory: false)
    }

    /// The deterministic name `stage(_:requestID:)` gives the item at `index`.
    public static func stagedFilename(_ proposed: String, index: Int) throws -> String {
        try safeFilename(proposed, index: index, salt: nil)
    }

    /// The owned copies a durable record bound, proven still to be those bytes.
    /// Each must be a regular file (never a link) of the recorded length whose
    /// SHA-256 matches, and the recorded lengths must still fit the item and
    /// selection quotas. Anything else throws: a copy that is missing,
    /// truncated, replaced or modified is not reused.
    public func verifiedURLs(
        requestID: String,
        expecting: [OwnedSnapshotExpectation]
    ) throws -> [URL] {
        guard Self.isSafeComponent(requestID) else { throw OwnedSnapshotStoreError.invalidFilename }
        guard !expecting.isEmpty else { throw OwnedSnapshotStoreError.emptySelection }
        guard expecting.count <= Self.maximumItems else { throw OwnedSnapshotStoreError.tooManyItems }
        var total: UInt64 = 0
        return try expecting.map { expected in
            guard Self.isSafeComponent(expected.name), Self.isDigest(expected.digest) else {
                throw OwnedSnapshotStoreError.invalidFilename
            }
            try Self.check(bytes: expected.byteLength, aggregate: total)
            total += expected.byteLength
            let url = try stagedURL(requestID: requestID, name: expected.name)
            var status = stat()
            guard Darwin.lstat(url.path, &status) == 0 else {
                throw errno == ENOENT
                    ? OwnedSnapshotStoreError.missing
                    : OwnedSnapshotStoreError.unreadableInput
            }
            guard status.st_mode & S_IFMT == S_IFREG,
                  UInt64(status.st_size) == expected.byteLength,
                  try Self.digestOfFile(at: url, length: expected.byteLength) == expected.digest
            else { throw OwnedSnapshotStoreError.integrityMismatch }
            return url
        }
    }

    /// Removes the request directories that hold these files.
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

    public func removeRequest(requestID: String) {
        guard Self.isSafeComponent(requestID) else { return }
        try? fileManager.removeItem(at: root.appendingPathComponent(requestID, isDirectory: true))
    }

    /// Removes request directories nothing has touched for `age`. A directory
    /// named in `keeping` backs a live candidate, which is bounded by its own
    /// request and review draft rather than by age, and is never removed here.
    @discardableResult
    public func removeExpired(
        olderThan age: TimeInterval = 24 * 60 * 60,
        now: Date = Date(),
        keeping: Set<String> = []
    ) -> Int {
        guard let children = try? fileManager.contentsOfDirectory(
            at: root,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles]
        ) else { return 0 }
        var removed = 0
        for child in children where !keeping.contains(child.lastPathComponent) {
            let modified = (try? child.resourceValues(forKeys: [.contentModificationDateKey]))?
                .contentModificationDate ?? .distantPast
            guard now.timeIntervalSince(modified) >= age else { continue }
            if (try? fileManager.removeItem(at: child)) != nil { removed += 1 }
        }
        return removed
    }

    private func stageAll(
        _ inputs: [OwnedSnapshotInput],
        requestID: String,
        deterministic: Bool
    ) throws -> [OwnedSnapshotReceipt] {
        guard !inputs.isEmpty else { throw OwnedSnapshotStoreError.emptySelection }
        guard inputs.count <= Self.maximumItems else { throw OwnedSnapshotStoreError.tooManyItems }
        guard Self.isSafeComponent(requestID) else { throw OwnedSnapshotStoreError.invalidFilename }

        let request = root.appendingPathComponent(requestID, isDirectory: true)
        try fileManager.createDirectory(
            at: request,
            withIntermediateDirectories: deterministic,
            attributes: [.posixPermissions: 0o700]
        )
        if deterministic {
            try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: request.path)
        }
        var total: UInt64 = 0
        var receipts: [OwnedSnapshotReceipt] = []
        do {
            for (index, input) in inputs.enumerated() {
                let salt = deterministic ? nil : UUID().uuidString.lowercased()
                let destination: URL
                let bytes: UInt64
                let digest: String
                switch input {
                case let .data(data, suggestedFilename):
                    destination = request.appendingPathComponent(
                        try Self.safeFilename(suggestedFilename, index: index, salt: salt),
                        isDirectory: false
                    )
                    bytes = UInt64(data.count)
                    try Self.check(bytes: bytes, aggregate: total)
                    try data.write(to: destination, options: [.atomic])
                    digest = Self.digest(of: data)
                case let .file(source, suggestedFilename):
                    destination = request.appendingPathComponent(
                        try Self.safeFilename(
                            suggestedFilename ?? source.lastPathComponent,
                            index: index,
                            salt: salt
                        ),
                        isDirectory: false
                    )
                    (bytes, digest) = try copyBounded(from: source, to: destination, aggregate: total)
                }
                total += bytes
                try fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
                receipts.append(OwnedSnapshotReceipt(url: destination, byteLength: bytes, digest: digest))
            }
            return receipts
        } catch {
            if !deterministic { try? fileManager.removeItem(at: request) }
            throw error
        }
    }

    /// Copies into a sibling temporary file and renames it over the destination,
    /// so an interrupted copy never leaves a short file at the final name, and
    /// hashes the bytes as it writes them.
    private func copyBounded(
        from source: URL,
        to destination: URL,
        aggregate: UInt64
    ) throws -> (bytes: UInt64, digest: String) {
        let accessed = source.startAccessingSecurityScopedResource()
        defer { if accessed { source.stopAccessingSecurityScopedResource() } }
        guard fileManager.fileExists(atPath: source.path),
              let input = try? FileHandle(forReadingFrom: source)
        else { throw OwnedSnapshotStoreError.unreadableInput }
        let temporary = destination.deletingLastPathComponent().appendingPathComponent(
            ".\(UUID().uuidString.lowercased()).partial",
            isDirectory: false
        )
        fileManager.createFile(
            atPath: temporary.path,
            contents: nil,
            attributes: [.posixPermissions: 0o600]
        )
        guard let output = try? FileHandle(forWritingTo: temporary) else {
            try? input.close()
            throw OwnedSnapshotStoreError.unreadableInput
        }
        do {
            defer { try? input.close() }
            var hasher = SHA256()
            var copied: UInt64 = 0
            while true {
                let chunk = try input.read(upToCount: 1_024 * 1_024) ?? Data()
                if chunk.isEmpty { break }
                copied += UInt64(chunk.count)
                try Self.check(bytes: copied, aggregate: aggregate)
                hasher.update(data: chunk)
                try output.write(contentsOf: chunk)
            }
            try output.close()
            guard Darwin.rename(temporary.path, destination.path) == 0 else {
                throw OwnedSnapshotStoreError.writeFailed
            }
            return (copied, Self.hex(hasher.finalize()))
        } catch {
            try? output.close()
            try? fileManager.removeItem(at: temporary)
            throw error
        }
    }

    private static func check(bytes: UInt64, aggregate: UInt64) throws {
        guard bytes <= maximumItemBytes else { throw OwnedSnapshotStoreError.itemTooLarge }
        guard aggregate + bytes <= maximumSelectionBytes else {
            throw OwnedSnapshotStoreError.selectionTooLarge
        }
    }

    private static func digest(of data: Data) -> String {
        hex(SHA256.hash(data: data))
    }

    /// Hashes a file that must be exactly `length` bytes, reading no further,
    /// so a file that grew after the length check cannot be streamed unbounded.
    private static func digestOfFile(at url: URL, length: UInt64) throws -> String {
        guard let input = try? FileHandle(forReadingFrom: url) else {
            throw OwnedSnapshotStoreError.unreadableInput
        }
        defer { try? input.close() }
        var hasher = SHA256()
        var read: UInt64 = 0
        while read <= length {
            let wanted = Int(min(UInt64(1_024 * 1_024), length - read + 1))
            guard let chunk = try input.read(upToCount: wanted), !chunk.isEmpty else { break }
            read += UInt64(chunk.count)
            guard read <= length else { throw OwnedSnapshotStoreError.integrityMismatch }
            hasher.update(data: chunk)
        }
        guard read == length else { throw OwnedSnapshotStoreError.integrityMismatch }
        return hex(hasher.finalize())
    }

    private static func hex(_ digest: SHA256Digest) -> String {
        "sha256:" + digest.map { String(format: "%02x", $0) }.joined()
    }

    static func isDigest(_ value: String) -> Bool {
        value.hasPrefix("sha256:")
            && value.utf8.count == "sha256:".utf8.count + 64
            && value.utf8.dropFirst("sha256:".utf8.count).allSatisfy {
                ($0 >= 0x30 && $0 <= 0x39) || ($0 >= 0x61 && $0 <= 0x66)
            }
    }

    private static func safeFilename(_ proposed: String, index: Int, salt: String?) throws -> String {
        let leaf = (proposed as NSString).lastPathComponent
            .replacingOccurrences(of: "\0", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !leaf.isEmpty, leaf != ".", leaf != "..", !leaf.contains("/") else {
            throw OwnedSnapshotStoreError.invalidFilename
        }
        let stem = (leaf as NSString).deletingPathExtension
        let suffix = (leaf as NSString).pathExtension
        let unique = salt.map { "\(index + 1)-\($0)-\(stem)" } ?? "\(index + 1)-\(stem)"
        return suffix.isEmpty ? unique : "\(unique).\(suffix)"
    }

    static func isSafeComponent(_ value: String) -> Bool {
        !value.isEmpty
            && value == (value as NSString).lastPathComponent
            && value != "."
            && value != ".."
            && !value.hasPrefix(".")
            && !value.contains("/")
            && !value.contains("\0")
    }
}
