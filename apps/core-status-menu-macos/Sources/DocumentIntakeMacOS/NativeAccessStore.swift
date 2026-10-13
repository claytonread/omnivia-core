import Darwin
import Foundation

public enum NativeAccessError: Error, Equatable {
    case invalidIdentifier
    case invalidRoute
    case duplicateConnectedAccess
    case unknownAccess
    case disconnected
    case generationMismatch
    case staleBookmark
    case accessDenied
    case memberOutsideRoot
    case stateUnavailable
    case invalidState
}

public enum NativeAccessState: String, Codable, Sendable {
    case connected
    case disconnected
}

public enum NativeAccessRoute: Codable, Equatable, Sendable {
    case directFile
    case rootMember(rootRef: String, memberRef: String, relativeComponents: [String])

    private enum CodingKeys: String, CodingKey {
        case kind
        case rootRef
        case memberRef
        case relativeComponents
    }

    private enum Kind: String, Codable {
        case directFile = "direct_file"
        case rootMember = "root_member"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        switch try container.decode(Kind.self, forKey: .kind) {
        case .directFile:
            self = .directFile
        case .rootMember:
            self = .rootMember(
                rootRef: try container.decode(String.self, forKey: .rootRef),
                memberRef: try container.decode(String.self, forKey: .memberRef),
                relativeComponents: try container.decode([String].self, forKey: .relativeComponents)
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .directFile:
            try container.encode(Kind.directFile, forKey: .kind)
        case let .rootMember(rootRef, memberRef, relativeComponents):
            try container.encode(Kind.rootMember, forKey: .kind)
            try container.encode(rootRef, forKey: .rootRef)
            try container.encode(memberRef, forKey: .memberRef)
            try container.encode(relativeComponents, forKey: .relativeComponents)
        }
    }
}

public struct NativeAccessDescriptor: Equatable, Sendable {
    public let accessRef: String
    public let linkedDocumentID: String
    public let generation: UInt64
    public let state: NativeAccessState
    public let route: NativeAccessRoute
}

public struct NativeAccessExpectation: Equatable, Sendable {
    public let accessRef: String
    public let generation: UInt64

    public init(accessRef: String, generation: UInt64) {
        self.accessRef = accessRef
        self.generation = generation
    }
}

public protocol SecurityScopedBookmarkCoding: Sendable {
    func makeReadOnlyBookmark(for url: URL) throws -> Data
    func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool)
    func startAccessing(_ url: URL) -> Bool
    func stopAccessing(_ url: URL)
}

public struct SystemSecurityScopedBookmarkCodec: SecurityScopedBookmarkCoding {
    public init() {}

    public func makeReadOnlyBookmark(for url: URL) throws -> Data {
        try url.bookmarkData(
            options: [.withSecurityScope, .securityScopeAllowOnlyReadAccess],
            includingResourceValuesForKeys: [.isRegularFileKey, .isDirectoryKey],
            relativeTo: nil
        )
    }

    public func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
        var stale = false
        let url = try URL(
            resolvingBookmarkData: data,
            options: [.withSecurityScope, .withoutUI],
            relativeTo: nil,
            bookmarkDataIsStale: &stale
        )
        return (url, stale)
    }

    public func startAccessing(_ url: URL) -> Bool {
        url.startAccessingSecurityScopedResource()
    }

    public func stopAccessing(_ url: URL) {
        url.stopAccessingSecurityScopedResource()
    }
}

private struct NativeAccessRecord: Codable, Equatable {
    let accessRef: String
    let linkedDocumentID: String
    let generation: UInt64
    let state: NativeAccessState
    let route: NativeAccessRoute
    let bookmark: Data?
}

private struct NativeAccessDocument: Codable {
    let schema: String
    let records: [NativeAccessRecord]
}

/// The sole native owner of persistent original-file grants.
///
/// Bookmarks and relative member routes stay in this owner-only store. Public descriptors contain opaque
/// references only. Reads are generation checked, balanced, and constrained to the selected root.
public final class NativeAccessStore: @unchecked Sendable {
    private static let schema = "omnivia.native-access.v1"
    private static let identifierCharacters = CharacterSet(
        charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"
    )

    private let lock = NSLock()
    private let directory: URL
    private let stateFile: URL
    private let codec: any SecurityScopedBookmarkCoding
    private var records: [String: NativeAccessRecord]

    public init(
        stateDirectory: URL,
        codec: any SecurityScopedBookmarkCoding = SystemSecurityScopedBookmarkCodec()
    ) throws {
        self.directory = stateDirectory
        self.stateFile = stateDirectory.appendingPathComponent("native-access-v1.json", isDirectory: false)
        self.codec = codec
        self.records = [:]
        try Self.prepareDirectory(stateDirectory)
        self.records = try Self.load(from: stateFile)
    }

    public func descriptors() -> [NativeAccessDescriptor] {
        lock.withLock {
            records.values.sorted { $0.accessRef < $1.accessRef }.map(Self.descriptor)
        }
    }

    @discardableResult
    public func adopt(
        accessRef: String,
        linkedDocumentID: String,
        selectedURL: URL,
        route: NativeAccessRoute
    ) throws -> NativeAccessDescriptor {
        try Self.validateIdentifier(accessRef)
        try Self.validateIdentifier(linkedDocumentID)
        try Self.validate(route)
        try Self.validateSelection(selectedURL, for: route)
        let bookmark: Data
        do {
            bookmark = try codec.makeReadOnlyBookmark(for: selectedURL)
        } catch {
            throw NativeAccessError.accessDenied
        }
        return try lock.withLock {
            let existing = records[accessRef]
            if existing?.state == .connected {
                throw NativeAccessError.duplicateConnectedAccess
            }
            if let existing, existing.linkedDocumentID != linkedDocumentID || existing.route != route {
                throw NativeAccessError.invalidRoute
            }
            let generation = (existing?.generation ?? 0) + 1
            let record = NativeAccessRecord(
                accessRef: accessRef,
                linkedDocumentID: linkedDocumentID,
                generation: generation,
                state: .connected,
                route: route,
                bookmark: bookmark
            )
            var replacement = records
            replacement[accessRef] = record
            try persist(replacement)
            records = replacement
            return Self.descriptor(record)
        }
    }

    @discardableResult
    public func disconnect(accessRef: String) throws -> NativeAccessDescriptor {
        try lock.withLock {
            guard let current = records[accessRef] else { throw NativeAccessError.unknownAccess }
            if current.state == .disconnected { return Self.descriptor(current) }
            let record = NativeAccessRecord(
                accessRef: current.accessRef,
                linkedDocumentID: current.linkedDocumentID,
                generation: current.generation,
                state: .disconnected,
                route: current.route,
                bookmark: nil
            )
            var replacement = records
            replacement[accessRef] = record
            try persist(replacement)
            records = replacement
            return Self.descriptor(record)
        }
    }

    public func disconnect(linkedDocumentID: String) throws -> [NativeAccessDescriptor] {
        try Self.validateIdentifier(linkedDocumentID)
        return try lock.withLock {
            let matches = records.values.filter {
                $0.linkedDocumentID == linkedDocumentID && $0.state == .connected
            }
            if matches.isEmpty { return [] }
            var replacement = records
            for current in matches {
                replacement[current.accessRef] = NativeAccessRecord(
                    accessRef: current.accessRef,
                    linkedDocumentID: current.linkedDocumentID,
                    generation: current.generation,
                    state: .disconnected,
                    route: current.route,
                    bookmark: nil
                )
            }
            try persist(replacement)
            records = replacement
            return matches.map { Self.descriptor(replacement[$0.accessRef]!) }
                .sorted { $0.accessRef < $1.accessRef }
        }
    }

    public func withReadAccess<T>(
        accessRef: String,
        expectedGeneration: UInt64,
        _ body: (URL) throws -> T
    ) throws -> T {
        let record = try lock.withLock { () throws -> NativeAccessRecord in
            guard let record = records[accessRef] else { throw NativeAccessError.unknownAccess }
            guard record.state == .connected, let bookmark = record.bookmark else {
                throw NativeAccessError.disconnected
            }
            guard record.generation == expectedGeneration else {
                throw NativeAccessError.generationMismatch
            }
            return NativeAccessRecord(
                accessRef: record.accessRef,
                linkedDocumentID: record.linkedDocumentID,
                generation: record.generation,
                state: record.state,
                route: record.route,
                bookmark: bookmark
            )
        }
        guard let bookmark = record.bookmark else { throw NativeAccessError.disconnected }
        let resolved: (url: URL, stale: Bool)
        do {
            resolved = try codec.resolveReadOnlyBookmark(bookmark)
        } catch {
            throw NativeAccessError.accessDenied
        }
        guard !resolved.stale else { throw NativeAccessError.staleBookmark }
        guard codec.startAccessing(resolved.url) else { throw NativeAccessError.accessDenied }
        defer { codec.stopAccessing(resolved.url) }
        let target = try Self.targetURL(root: resolved.url, route: record.route)
        return try body(target)
    }

    /// Resolves the selected root behind an existing root-member route. This is available only to
    /// the native monitoring owner; callers still have to enforce the persisted subscription scope.
    public func withRootReadAccess<T>(
        rootRef: String,
        expectedGeneration: UInt64,
        _ body: (URL) throws -> T
    ) throws -> T {
        let record = try lock.withLock { () throws -> NativeAccessRecord in
            let candidates = records.values.filter { record in
                guard record.state == .connected,
                      record.bookmark != nil,
                      case let .rootMember(candidateRoot, _, _) = record.route
                else { return false }
                return candidateRoot == rootRef && record.generation == expectedGeneration
            }
            guard let record = candidates.sorted(by: { $0.accessRef < $1.accessRef }).first else {
                throw NativeAccessError.unknownAccess
            }
            return record
        }
        guard let bookmark = record.bookmark else { throw NativeAccessError.disconnected }
        let resolved: (url: URL, stale: Bool)
        do {
            resolved = try codec.resolveReadOnlyBookmark(bookmark)
        } catch {
            throw NativeAccessError.accessDenied
        }
        guard !resolved.stale else { throw NativeAccessError.staleBookmark }
        guard codec.startAccessing(resolved.url) else { throw NativeAccessError.accessDenied }
        defer { codec.stopAccessing(resolved.url) }
        let values = try resolved.url.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
        guard values.isDirectory == true, values.isSymbolicLink != true else {
            throw NativeAccessError.invalidRoute
        }
        return try body(resolved.url.resolvingSymlinksInPath().standardizedFileURL)
    }

    /// Adds a newly discovered child while the already-authorised root scope is active. The caller
    /// supplies only opaque root/member identities and a validated root-relative locator.
    @discardableResult
    public func adoptRootMember(
        accessRef: String,
        linkedDocumentID: String,
        rootRef: String,
        memberRef: String,
        relativeComponents: [String],
        expectedRootGeneration: UInt64
    ) throws -> NativeAccessDescriptor {
        try withRootReadAccess(rootRef: rootRef, expectedGeneration: expectedRootGeneration) { root in
            try adopt(
                accessRef: accessRef,
                linkedDocumentID: linkedDocumentID,
                selectedURL: root,
                route: .rootMember(
                    rootRef: rootRef,
                    memberRef: memberRef,
                    relativeComponents: relativeComponents
                )
            )
        }
    }

    /// Rechecks a complete prepared batch while preventing relink/disconnect from interleaving with
    /// its publication. The body must only perform the bounded local queue commit.
    public func withValidatedGenerations<T>(
        _ expectations: [NativeAccessExpectation],
        _ body: () throws -> T
    ) throws -> T {
        try lock.withLock {
            guard !expectations.isEmpty,
                  Set(expectations.map(\.accessRef)).count == expectations.count
            else { throw NativeAccessError.invalidRoute }
            for expectation in expectations {
                guard let record = records[expectation.accessRef] else {
                    throw NativeAccessError.unknownAccess
                }
                guard record.state == .connected, record.bookmark != nil else {
                    throw NativeAccessError.disconnected
                }
                guard record.generation == expectation.generation else {
                    throw NativeAccessError.generationMismatch
                }
            }
            return try body()
        }
    }

    private func persist(_ value: [String: NativeAccessRecord]) throws {
        let document = NativeAccessDocument(
            schema: Self.schema,
            records: value.values.sorted { $0.accessRef < $1.accessRef }
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        do {
            let data = try encoder.encode(document)
            let temporary = directory.appendingPathComponent(".native-access-(UUID().uuidString).tmp")
            try data.write(to: temporary, options: [.atomic])
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: temporary.path)
            if FileManager.default.fileExists(atPath: stateFile.path) {
                _ = try FileManager.default.replaceItemAt(stateFile, withItemAt: temporary)
            } else {
                try FileManager.default.moveItem(at: temporary, to: stateFile)
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateFile.path)
            let descriptor = Darwin.open(directory.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
            if descriptor >= 0 {
                _ = Darwin.fsync(descriptor)
                Darwin.close(descriptor)
            }
        } catch {
            throw NativeAccessError.stateUnavailable
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
                throw NativeAccessError.stateUnavailable
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        } catch {
            throw NativeAccessError.stateUnavailable
        }
    }

    private static func load(from stateFile: URL) throws -> [String: NativeAccessRecord] {
        guard FileManager.default.fileExists(atPath: stateFile.path) else { return [:] }
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: stateFile.path)
            guard (attributes[.type] as? FileAttributeType) == .typeRegular else {
                throw NativeAccessError.invalidState
            }
            guard let size = attributes[.size] as? NSNumber,
                  size.uint64Value <= 16 * 1024 * 1024,
                  let owner = attributes[.ownerAccountID] as? NSNumber,
                  owner.uint32Value == Darwin.getuid(),
                  let permissions = attributes[.posixPermissions] as? NSNumber,
                  permissions.intValue & 0o077 == 0
            else { throw NativeAccessError.invalidState }
            let document = try JSONDecoder().decode(
                NativeAccessDocument.self,
                from: Data(contentsOf: stateFile, options: [.mappedIfSafe])
            )
            guard document.schema == schema, document.records.count <= 100_000 else {
                throw NativeAccessError.invalidState
            }
            var result: [String: NativeAccessRecord] = [:]
            for record in document.records {
                try validateIdentifier(record.accessRef)
                try validateIdentifier(record.linkedDocumentID)
                try validate(record.route)
                guard record.generation > 0, result[record.accessRef] == nil else {
                    throw NativeAccessError.invalidState
                }
                guard (record.state == .connected) == (record.bookmark != nil) else {
                    throw NativeAccessError.invalidState
                }
                result[record.accessRef] = record
            }
            return result
        } catch let error as NativeAccessError {
            throw error
        } catch {
            throw NativeAccessError.invalidState
        }
    }

    private static func validateIdentifier(_ value: String) throws {
        guard !value.isEmpty,
              value.utf8.count <= 128,
              value.first?.isASCII == true,
              value.first?.isLetter == true || value.first?.isNumber == true,
              value.unicodeScalars.allSatisfy({ identifierCharacters.contains($0) })
        else { throw NativeAccessError.invalidIdentifier }
    }

    private static func validate(_ route: NativeAccessRoute) throws {
        switch route {
        case .directFile:
            return
        case let .rootMember(rootRef, memberRef, components):
            try validateIdentifier(rootRef)
            try validateIdentifier(memberRef)
            guard !components.isEmpty, components.count <= 256 else { throw NativeAccessError.invalidRoute }
            for component in components {
                guard !component.isEmpty,
                      component != ".",
                      component != "..",
                      !component.contains("/"),
                      !component.contains("\0")
                else { throw NativeAccessError.invalidRoute }
            }
        }
    }

    private static func validateSelection(_ url: URL, for route: NativeAccessRoute) throws {
        guard url.isFileURL,
              let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .isDirectoryKey])
        else { throw NativeAccessError.accessDenied }
        switch route {
        case .directFile:
            guard values.isRegularFile == true else { throw NativeAccessError.accessDenied }
        case .rootMember:
            guard values.isDirectory == true else { throw NativeAccessError.accessDenied }
        }
    }

    private static func targetURL(root: URL, route: NativeAccessRoute) throws -> URL {
        switch route {
        case .directFile:
            return root
        case let .rootMember(_, _, components):
            let canonicalRoot = root.resolvingSymlinksInPath().standardizedFileURL
            var candidate = canonicalRoot
            for component in components {
                candidate.appendPathComponent(component, isDirectory: false)
                guard let values = try? candidate.resourceValues(forKeys: [.isSymbolicLinkKey]),
                      values.isSymbolicLink != true
                else {
                    throw NativeAccessError.memberOutsideRoot
                }
            }
            candidate = candidate.resolvingSymlinksInPath().standardizedFileURL
            let rootPath = canonicalRoot.path.hasSuffix("/") ? canonicalRoot.path : canonicalRoot.path + "/"
            guard candidate.path.hasPrefix(rootPath), candidate.path != canonicalRoot.path else {
                throw NativeAccessError.memberOutsideRoot
            }
            return candidate
        }
    }

    private static func descriptor(_ record: NativeAccessRecord) -> NativeAccessDescriptor {
        NativeAccessDescriptor(
            accessRef: record.accessRef,
            linkedDocumentID: record.linkedDocumentID,
            generation: record.generation,
            state: record.state,
            route: record.route
        )
    }
}
