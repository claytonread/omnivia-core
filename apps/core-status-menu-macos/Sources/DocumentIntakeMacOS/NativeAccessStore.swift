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
    public let rootBindingGeneration: UInt64?
    public let state: NativeAccessState
    public let route: NativeAccessRoute
}

public struct NativeRootAccessDescriptor: Equatable, Sendable {
    public let rootRef: String
    public let generation: UInt64
    public let state: NativeAccessState
    public let ownerRefs: [String]
}

public struct NativeAccessExpectation: Equatable, Sendable {
    public let accessRef: String
    public let generation: UInt64
    public let rootBindingGeneration: UInt64?

    public init(
        accessRef: String,
        generation: UInt64,
        rootBindingGeneration: UInt64? = nil
    ) {
        self.accessRef = accessRef
        self.generation = generation
        self.rootBindingGeneration = rootBindingGeneration
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
    let rootBindingGeneration: UInt64?
    let state: NativeAccessState
    let route: NativeAccessRoute
    let bookmark: Data?
}

private struct NativeRootAccessRecord: Codable, Equatable {
    let rootRef: String
    let generation: UInt64
    let state: NativeAccessState
    let ownerRefs: [String]
    let bookmark: Data?
}

private struct NativeAccessDocument: Codable {
    let schema: String
    let records: [NativeAccessRecord]
    let roots: [NativeRootAccessRecord]
}

private struct LegacyNativeAccessRecord: Codable {
    let accessRef: String
    let linkedDocumentID: String
    let generation: UInt64
    let state: NativeAccessState
    let route: NativeAccessRoute
    let bookmark: Data?
}

private struct LegacyNativeAccessDocument: Codable {
    let schema: String
    let records: [LegacyNativeAccessRecord]
}

private struct NativeAccessSchemaEnvelope: Codable {
    let schema: String
}

/// The sole native owner of persistent original-file grants.
///
/// Bookmarks and relative member routes stay in this owner-only store. Public descriptors contain opaque
/// references only. Reads are generation checked, balanced, and constrained to the selected root.
public final class NativeAccessStore: @unchecked Sendable {
    private static let schema = "omnivia.native-access.v2"
    private static let identifierCharacters = CharacterSet(
        charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"
    )

    private let lock = NSLock()
    private let directory: URL
    private let stateFile: URL
    private let codec: any SecurityScopedBookmarkCoding
    private var records: [String: NativeAccessRecord]
    private var roots: [String: NativeRootAccessRecord]

    public init(
        stateDirectory: URL,
        codec: any SecurityScopedBookmarkCoding = SystemSecurityScopedBookmarkCodec()
    ) throws {
        self.directory = stateDirectory
        self.stateFile = stateDirectory.appendingPathComponent("native-access-v1.json", isDirectory: false)
        self.codec = codec
        self.records = [:]
        self.roots = [:]
        try Self.prepareDirectory(stateDirectory)
        let loaded = try Self.load(from: stateFile, codec: codec)
        self.records = loaded.records
        self.roots = loaded.roots
        if loaded.migrated {
            try persist(records: loaded.records, roots: loaded.roots)
        }
    }

    public func descriptors() -> [NativeAccessDescriptor] {
        lock.withLock {
            records.values.sorted { $0.accessRef < $1.accessRef }.map(Self.descriptor)
        }
    }

    public func rootDescriptors() -> [NativeRootAccessDescriptor] {
        lock.withLock {
            roots.values.sorted { $0.rootRef < $1.rootRef }.map(Self.rootDescriptor)
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
        guard route == .directFile else { throw NativeAccessError.invalidRoute }
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
                rootBindingGeneration: nil,
                state: .connected,
                route: route,
                bookmark: bookmark
            )
            var replacement = records
            replacement[accessRef] = record
            try persist(records: replacement, roots: roots)
            records = replacement
            return Self.descriptor(record)
        }
    }

    /// Adopts one selected folder grant and retains it for a durable owner such
    /// as a bulk run or subscription. Repeating adoption for another owner of
    /// the same root reuses the single bookmark and binding generation.
    @discardableResult
    public func adoptRoot(
        rootRef: String,
        ownerRef: String,
        selectedURL: URL
    ) throws -> NativeRootAccessDescriptor {
        try Self.validateIdentifier(rootRef)
        try Self.validateIdentifier(ownerRef)
        try Self.validateSelection(
            selectedURL,
            for: .rootMember(rootRef: rootRef, memberRef: "member", relativeComponents: ["member"])
        )
        return try lock.withLock {
            if let current = roots[rootRef], current.state == .connected {
                guard let bookmark = current.bookmark else { throw NativeAccessError.invalidState }
                let resolved: (url: URL, stale: Bool)
                do {
                    resolved = try codec.resolveReadOnlyBookmark(bookmark)
                } catch {
                    throw NativeAccessError.accessDenied
                }
                guard !resolved.stale else { throw NativeAccessError.staleBookmark }
                let existing = resolved.url.resolvingSymlinksInPath().standardizedFileURL
                let selected = selectedURL.resolvingSymlinksInPath().standardizedFileURL
                guard existing == selected else { throw NativeAccessError.invalidRoute }
                guard !current.ownerRefs.contains(ownerRef) else {
                    return Self.rootDescriptor(current)
                }
                let updated = NativeRootAccessRecord(
                    rootRef: current.rootRef,
                    generation: current.generation,
                    state: current.state,
                    ownerRefs: (current.ownerRefs + [ownerRef]).sorted(),
                    bookmark: bookmark
                )
                var replacement = roots
                replacement[rootRef] = updated
                try persist(records: records, roots: replacement)
                roots = replacement
                return Self.rootDescriptor(updated)
            }
            let bookmark: Data
            do {
                bookmark = try codec.makeReadOnlyBookmark(for: selectedURL)
            } catch {
                throw NativeAccessError.accessDenied
            }
            let record = NativeRootAccessRecord(
                rootRef: rootRef,
                generation: (roots[rootRef]?.generation ?? 0) + 1,
                state: .connected,
                ownerRefs: [ownerRef],
                bookmark: bookmark
            )
            var replacement = roots
            replacement[rootRef] = record
            try persist(records: records, roots: replacement)
            roots = replacement
            return Self.rootDescriptor(record)
        }
    }

    /// Releases one durable root owner. Member records independently retain the
    /// shared grant; bookmark bytes are removed only after the last owner and
    /// last connected member are both gone.
    @discardableResult
    public func releaseRoot(rootRef: String, ownerRef: String) throws -> NativeRootAccessDescriptor {
        try Self.validateIdentifier(rootRef)
        try Self.validateIdentifier(ownerRef)
        return try lock.withLock {
            guard let current = roots[rootRef] else { throw NativeAccessError.unknownAccess }
            let owners = current.ownerRefs.filter { $0 != ownerRef }
            let hasMember = records.values.contains {
                guard $0.state == .connected,
                      case let .rootMember(candidate, _, _) = $0.route
                else { return false }
                return candidate == rootRef
            }
            let connected = !owners.isEmpty || hasMember
            let updated = NativeRootAccessRecord(
                rootRef: current.rootRef,
                generation: current.generation,
                state: connected ? .connected : .disconnected,
                ownerRefs: owners,
                bookmark: connected ? current.bookmark : nil
            )
            var replacement = roots
            replacement[rootRef] = updated
            try persist(records: records, roots: replacement)
            roots = replacement
            return Self.rootDescriptor(updated)
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
                rootBindingGeneration: current.rootBindingGeneration,
                state: .disconnected,
                route: current.route,
                bookmark: nil
            )
            var replacement = records
            replacement[accessRef] = record
            var replacementRoots = roots
            Self.pruneUnusedRoot(for: current.route, records: replacement, roots: &replacementRoots)
            try persist(records: replacement, roots: replacementRoots)
            records = replacement
            roots = replacementRoots
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
                    rootBindingGeneration: current.rootBindingGeneration,
                    state: .disconnected,
                    route: current.route,
                    bookmark: nil
                )
            }
            var replacementRoots = roots
            for current in matches {
                Self.pruneUnusedRoot(for: current.route, records: replacement, roots: &replacementRoots)
            }
            try persist(records: replacement, roots: replacementRoots)
            records = replacement
            roots = replacementRoots
            return matches.map { Self.descriptor(replacement[$0.accessRef]!) }
                .sorted { $0.accessRef < $1.accessRef }
        }
    }

    public func withReadAccess<T>(
        accessRef: String,
        expectedGeneration: UInt64,
        expectedRootBindingGeneration: UInt64? = nil,
        _ body: (URL) throws -> T
    ) throws -> T {
        let resolvedAccess = try lock.withLock { () throws -> (NativeAccessRecord, Data) in
            guard let record = records[accessRef] else { throw NativeAccessError.unknownAccess }
            guard record.state == .connected else { throw NativeAccessError.disconnected }
            guard record.generation == expectedGeneration else {
                throw NativeAccessError.generationMismatch
            }
            switch record.route {
            case .directFile:
                guard expectedRootBindingGeneration == nil, let bookmark = record.bookmark else {
                    throw NativeAccessError.generationMismatch
                }
                return (record, bookmark)
            case let .rootMember(rootRef, _, _):
                guard record.bookmark == nil,
                      let frozenRootGeneration = record.rootBindingGeneration,
                      let expectedRootBindingGeneration,
                      frozenRootGeneration == expectedRootBindingGeneration,
                      let root = roots[rootRef],
                      root.state == .connected,
                      root.generation == expectedRootBindingGeneration,
                      let bookmark = root.bookmark
                else { throw NativeAccessError.generationMismatch }
                return (record, bookmark)
            }
        }
        let record = resolvedAccess.0
        let bookmark = resolvedAccess.1
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
        let root = try lock.withLock { () throws -> NativeRootAccessRecord in
            guard let root = roots[rootRef] else { throw NativeAccessError.unknownAccess }
            guard root.state == .connected, root.bookmark != nil else {
                throw NativeAccessError.disconnected
            }
            guard root.generation == expectedGeneration else {
                throw NativeAccessError.generationMismatch
            }
            return root
        }
        guard let bookmark = root.bookmark else { throw NativeAccessError.disconnected }
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
        try Self.validateIdentifier(accessRef)
        try Self.validateIdentifier(linkedDocumentID)
        let route = NativeAccessRoute.rootMember(
            rootRef: rootRef,
            memberRef: memberRef,
            relativeComponents: relativeComponents
        )
        try Self.validate(route)
        return try lock.withLock {
            guard let root = roots[rootRef], root.state == .connected, root.bookmark != nil else {
                throw NativeAccessError.unknownAccess
            }
            guard root.generation == expectedRootGeneration else {
                throw NativeAccessError.generationMismatch
            }
            let existing = records[accessRef]
            if existing?.state == .connected { throw NativeAccessError.duplicateConnectedAccess }
            if let existing,
               existing.linkedDocumentID != linkedDocumentID || existing.route != route
            {
                throw NativeAccessError.invalidRoute
            }
            let record = NativeAccessRecord(
                accessRef: accessRef,
                linkedDocumentID: linkedDocumentID,
                generation: (existing?.generation ?? 0) + 1,
                rootBindingGeneration: root.generation,
                state: .connected,
                route: route,
                bookmark: nil
            )
            var replacement = records
            replacement[accessRef] = record
            try persist(records: replacement, roots: roots)
            records = replacement
            return Self.descriptor(record)
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
                guard record.state == .connected else { throw NativeAccessError.disconnected }
                guard record.generation == expectation.generation else {
                    throw NativeAccessError.generationMismatch
                }
                switch record.route {
                case .directFile:
                    guard record.bookmark != nil, expectation.rootBindingGeneration == nil else {
                        throw NativeAccessError.generationMismatch
                    }
                case let .rootMember(rootRef, _, _):
                    guard record.bookmark == nil,
                          let frozen = record.rootBindingGeneration,
                          let expected = expectation.rootBindingGeneration,
                          frozen == expected,
                          let root = roots[rootRef],
                          root.state == .connected,
                          root.generation == expected,
                          root.bookmark != nil
                    else { throw NativeAccessError.generationMismatch }
                }
            }
            return try body()
        }
    }

    private func persist(
        records: [String: NativeAccessRecord],
        roots: [String: NativeRootAccessRecord]
    ) throws {
        let document = NativeAccessDocument(
            schema: Self.schema,
            records: records.values.sorted { $0.accessRef < $1.accessRef },
            roots: roots.values.sorted { $0.rootRef < $1.rootRef }
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

    private static func load(
        from stateFile: URL,
        codec: any SecurityScopedBookmarkCoding
    ) throws -> (
        records: [String: NativeAccessRecord],
        roots: [String: NativeRootAccessRecord],
        migrated: Bool
    ) {
        guard FileManager.default.fileExists(atPath: stateFile.path) else { return ([:], [:], false) }
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
            let data = try Data(contentsOf: stateFile, options: [.mappedIfSafe])
            let envelope = try JSONDecoder().decode(NativeAccessSchemaEnvelope.self, from: data)
            if envelope.schema == "omnivia.native-access.v1" {
                return try migrateLegacy(data, codec: codec)
            }
            let document = try JSONDecoder().decode(NativeAccessDocument.self, from: data)
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
                switch record.route {
                case .directFile:
                    guard record.rootBindingGeneration == nil,
                          (record.state == .connected) == (record.bookmark != nil)
                    else { throw NativeAccessError.invalidState }
                case .rootMember:
                    guard record.rootBindingGeneration != nil, record.bookmark == nil else {
                        throw NativeAccessError.invalidState
                    }
                }
                result[record.accessRef] = record
            }
            guard document.roots.count <= 100_000 else { throw NativeAccessError.invalidState }
            var loadedRoots: [String: NativeRootAccessRecord] = [:]
            for root in document.roots {
                try validateIdentifier(root.rootRef)
                guard root.generation > 0,
                      loadedRoots[root.rootRef] == nil,
                      root.ownerRefs == root.ownerRefs.sorted(),
                      Set(root.ownerRefs).count == root.ownerRefs.count,
                      root.ownerRefs.allSatisfy({ (try? validateIdentifier($0)) != nil }),
                      (root.state == .connected) == (root.bookmark != nil)
                else { throw NativeAccessError.invalidState }
                loadedRoots[root.rootRef] = root
            }
            for record in result.values {
                guard case let .rootMember(rootRef, _, _) = record.route else { continue }
                guard let root = loadedRoots[rootRef] else { throw NativeAccessError.invalidState }
                if record.state == .connected {
                    guard root.state == .connected,
                          root.generation == record.rootBindingGeneration
                    else { throw NativeAccessError.invalidState }
                }
            }
            return (result, loadedRoots, false)
        } catch let error as NativeAccessError {
            throw error
        } catch {
            throw NativeAccessError.invalidState
        }
    }

    private static func migrateLegacy(
        _ data: Data,
        codec: any SecurityScopedBookmarkCoding
    ) throws -> (
        records: [String: NativeAccessRecord],
        roots: [String: NativeRootAccessRecord],
        migrated: Bool
    ) {
        let legacy = try JSONDecoder().decode(LegacyNativeAccessDocument.self, from: data)
        guard legacy.schema == "omnivia.native-access.v1", legacy.records.count <= 100_000 else {
            throw NativeAccessError.invalidState
        }
        var records: [String: NativeAccessRecord] = [:]
        var roots: [String: NativeRootAccessRecord] = [:]
        var canonicalRoots: [String: URL] = [:]
        for old in legacy.records {
            try validateIdentifier(old.accessRef)
            try validateIdentifier(old.linkedDocumentID)
            try validate(old.route)
            guard old.generation > 0, records[old.accessRef] == nil else {
                throw NativeAccessError.invalidState
            }
            switch old.route {
            case .directFile:
                guard (old.state == .connected) == (old.bookmark != nil) else {
                    throw NativeAccessError.invalidState
                }
                records[old.accessRef] = NativeAccessRecord(
                    accessRef: old.accessRef,
                    linkedDocumentID: old.linkedDocumentID,
                    generation: old.generation,
                    rootBindingGeneration: nil,
                    state: old.state,
                    route: old.route,
                    bookmark: old.bookmark
                )
            case let .rootMember(rootRef, _, _):
                guard (old.state == .connected) == (old.bookmark != nil) else {
                    throw NativeAccessError.invalidState
                }
                if let bookmark = old.bookmark {
                    let resolved = try codec.resolveReadOnlyBookmark(bookmark)
                    guard !resolved.stale else { throw NativeAccessError.staleBookmark }
                    let canonical = resolved.url.resolvingSymlinksInPath().standardizedFileURL
                    if let existing = canonicalRoots[rootRef], existing != canonical {
                        throw NativeAccessError.invalidState
                    }
                    canonicalRoots[rootRef] = canonical
                    if let root = roots[rootRef] {
                        if root.bookmark == nil {
                            roots[rootRef] = NativeRootAccessRecord(
                                rootRef: rootRef,
                                generation: root.generation,
                                state: .connected,
                                ownerRefs: root.ownerRefs,
                                bookmark: bookmark
                            )
                        }
                    } else {
                        roots[rootRef] = NativeRootAccessRecord(
                            rootRef: rootRef,
                            generation: 1,
                            state: .connected,
                            ownerRefs: [],
                            bookmark: bookmark
                        )
                    }
                } else if roots[rootRef] == nil {
                    roots[rootRef] = NativeRootAccessRecord(
                        rootRef: rootRef,
                        generation: 1,
                        state: .disconnected,
                        ownerRefs: [],
                        bookmark: nil
                    )
                }
                records[old.accessRef] = NativeAccessRecord(
                    accessRef: old.accessRef,
                    linkedDocumentID: old.linkedDocumentID,
                    generation: old.generation,
                    rootBindingGeneration: 1,
                    state: old.state,
                    route: old.route,
                    bookmark: nil
                )
            }
        }
        for record in records.values where record.state == .connected {
            guard case let .rootMember(rootRef, _, _) = record.route else { continue }
            guard roots[rootRef]?.state == .connected else { throw NativeAccessError.invalidState }
        }
        return (records, roots, true)
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
            rootBindingGeneration: record.rootBindingGeneration,
            state: record.state,
            route: record.route
        )
    }

    private static func rootDescriptor(_ record: NativeRootAccessRecord) -> NativeRootAccessDescriptor {
        NativeRootAccessDescriptor(
            rootRef: record.rootRef,
            generation: record.generation,
            state: record.state,
            ownerRefs: record.ownerRefs
        )
    }

    private static func pruneUnusedRoot(
        for route: NativeAccessRoute,
        records: [String: NativeAccessRecord],
        roots: inout [String: NativeRootAccessRecord]
    ) {
        guard case let .rootMember(rootRef, _, _) = route,
              let root = roots[rootRef],
              root.ownerRefs.isEmpty
        else { return }
        let hasMember = records.values.contains {
            guard $0.state == .connected,
                  case let .rootMember(candidate, _, _) = $0.route
            else { return false }
            return candidate == rootRef
        }
        guard !hasMember else { return }
        roots[rootRef] = NativeRootAccessRecord(
            rootRef: root.rootRef,
            generation: root.generation,
            state: .disconnected,
            ownerRefs: [],
            bookmark: nil
        )
    }
}
