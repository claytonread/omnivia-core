import CryptoKit
import Darwin
import Foundation

/// The qualified native identity of one file, as design §21.8 permits it to be
/// used: the volume's own identity plus the file's resource identifier. Nothing
/// here comes from the file's bytes, its digest, its pathname or its name, so a
/// rename keeps it and a byte-identical twin never shares it.
///
/// Returns nil when macOS will not qualify the file — an unidentified volume, an
/// absent resource identifier, anything unreadable. The registry treats that as
/// "unknown", never as "the same".
///
/// ponytail: Apple does not promise a file resource identifier across a *system*
/// restart (design §21.8, M7/M8), so after a reboot one file may be seen as a
/// new source — two routes over it would then capture it twice rather than
/// merging two histories, which is the safe direction to be wrong in. The
/// upgrade is the design's other named evidence: resolve each route's stored
/// bookmark and compare, treating a match as the same original. Do that when a
/// reboot splitting a source is actually observed to matter.
public enum NativeFileIdentity {
    public static func key(for url: URL) -> String? {
        var probe = url
        probe.removeAllCachedResourceValues()
        guard let values = try? probe.resourceValues(
            forKeys: [.volumeUUIDStringKey, .fileResourceIdentifierKey]
        ),
            let volume = values.volumeUUIDString, !volume.isEmpty,
            let resource = values.fileResourceIdentifier,
            let archived = try? NSKeyedArchiver.archivedData(
                withRootObject: resource,
                requiringSecureCoding: true
            )
        else { return nil }
        // Hashed so the durable record is a fixed-width opaque key rather than a
        // few hundred bytes of archived Foundation object.
        var hasher = SHA256()
        hasher.update(data: Data(volume.utf8))
        hasher.update(data: Data([0]))
        hasher.update(data: archived)
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}

/// The logical identities one route is to use for one file.
public struct CanonicalSourceIdentities: Equatable, Sendable {
    public let linkedDocumentID: String
    public let requestedSourceIdentity: String
    /// Nil when the file's native identity could not be qualified, or when an
    /// earlier member of the same submission already claimed it. These
    /// identities are then this route's alone: they are registered nowhere, so
    /// no later route is given them and nothing merges onto them.
    public let nativeKey: String?

    public var isCanonical: Bool { nativeKey != nil }
}

/// One canonical source's durable record. It holds identity and nothing else:
/// what has been captured of this source is the queue's own business, and the
/// queue is already durable.
public struct CanonicalSourceRecord: Codable, Equatable, Sendable {
    public let workspaceRef: String
    public let nativeKey: String
    public let linkedDocumentID: String
    public let requestedSourceIdentity: String
}

public enum CanonicalSourceRegistryError: Error, Equatable {
    case invalidState
    case stateUnavailable
}

private struct CanonicalSourceDocument: Codable {
    let schema: String
    let sources: [CanonicalSourceRecord]
}

/// One logical source and one linked document per workspace per native file.
///
/// Design §21.8.1: inside one workspace, overlapping watched roots and a direct
/// route over the same proven original reuse one verified original and one
/// linked document, with a membership route each. This registry is where that
/// sameness is decided and recorded. It is keyed by workspace plus qualified
/// native identity, so:
///
/// - two files with identical bytes are two sources, because bytes decide
///   nothing here;
/// - the same file in another workspace is another source, because the
///   workspace is part of the key;
/// - a rename keeps the source, because the key does not contain the name;
/// - a process restart keeps the source, because the record is durable (a
///   system restart is the one case the key cannot promise; see
///   `NativeFileIdentity`);
/// - a file macOS will not identify gets fresh identities that are registered
///   nowhere, so an unknown file never merges onto a known one.
///
/// `AutomaticSourceRouteElection` supplies §21.8.1's other half: one eligible
/// automatic reader per identity recorded here. Publication coalescence remains
/// the final race-safe boundary if two reads that began under different
/// elections overlap.
public final class CanonicalSourceRegistry: @unchecked Sendable {
    private static let schema = "omnivia.canonical-sources.v1"
    private static let maximumStateBytes: UInt64 = 8 * 1024 * 1024

    private let gate: PublicationGate
    private let directory: URL
    private let stateFile: URL
    /// How this registry qualifies a file's native identity. Production reads
    /// the volume and resource identifier; a test substitutes one to prove what
    /// happens when macOS gives neither.
    private let identity: @Sendable (URL) -> String?
    private var records: [String: CanonicalSourceRecord]
    /// Reverse index, so the publication boundary can find a source's record
    /// from the identity frozen into an intent.
    private var bySourceIdentity: [String: String]

    public init(
        stateDirectory: URL,
        gate: PublicationGate = .processShared,
        identity: @escaping @Sendable (URL) -> String? = { NativeFileIdentity.key(for: $0) }
    ) throws {
        self.gate = gate
        self.identity = identity
        directory = stateDirectory
        stateFile = stateDirectory.appendingPathComponent("canonical-sources-v1.json")
        try Self.prepareDirectory(stateDirectory)
        records = try Self.load(stateFile)
        bySourceIdentity = Dictionary(
            records.values.map { ($0.requestedSourceIdentity, Self.key($0.workspaceRef, $0.nativeKey)) },
            uniquingKeysWith: { first, _ in first }
        )
    }

    public func all() -> [CanonicalSourceRecord] {
        gate.withLock { records.values.sorted { $0.requestedSourceIdentity < $1.requestedSourceIdentity } }
    }

    /// The identities this workspace uses for this file, minting and recording
    /// them the first time it is seen.
    ///
    /// `claimedNativeKeys` names the sources earlier members of the same
    /// submission already took. Two members of one reviewed scope must carry
    /// distinct identities — one durable row, grant and queued item each — so a
    /// second path to one native file inside one submission (a hard link) is
    /// given its own identities rather than the canonical ones.
    /// ponytail: hard links inside one reviewed scope stay separate sources.
    /// Merge them only if a real folder ever needs it.
    public func resolve(
        url: URL,
        workspaceRef: String,
        claimedNativeKeys: Set<String> = []
    ) throws -> CanonicalSourceIdentities {
        guard let nativeKey = identity(url), !claimedNativeKeys.contains(nativeKey) else {
            return CanonicalSourceIdentities(
                linkedDocumentID: Self.identifier(prefix: "doc"),
                requestedSourceIdentity: Self.identifier(prefix: "source"),
                nativeKey: nil
            )
        }
        return try gate.withLock {
            let key = Self.key(workspaceRef, nativeKey)
            if let existing = records[key] {
                return CanonicalSourceIdentities(
                    linkedDocumentID: existing.linkedDocumentID,
                    requestedSourceIdentity: existing.requestedSourceIdentity,
                    nativeKey: nativeKey
                )
            }
            let record = CanonicalSourceRecord(
                workspaceRef: workspaceRef,
                nativeKey: nativeKey,
                linkedDocumentID: Self.identifier(prefix: "doc"),
                requestedSourceIdentity: Self.identifier(prefix: "source")
            )
            var replacement = records
            replacement[key] = record
            try persist(replacement)
            records = replacement
            bySourceIdentity[record.requestedSourceIdentity] = key
            return CanonicalSourceIdentities(
                linkedDocumentID: record.linkedDocumentID,
                requestedSourceIdentity: record.requestedSourceIdentity,
                nativeKey: nativeKey
            )
        }
    }

    public func record(sourceIdentity: String) -> CanonicalSourceRecord? {
        gate.withLock { bySourceIdentity[sourceIdentity].flatMap { records[$0] } }
    }

    private static func key(_ workspaceRef: String, _ nativeKey: String) -> String {
        workspaceRef + "\u{1f}" + nativeKey
    }

    private static func identifier(prefix: String) -> String {
        "\(prefix)-\(UUID().uuidString.lowercased())"
    }

    private func persist(_ values: [String: CanonicalSourceRecord]) throws {
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
            let data = try encoder.encode(
                CanonicalSourceDocument(
                    schema: Self.schema,
                    sources: values.values.sorted {
                        $0.requestedSourceIdentity < $1.requestedSourceIdentity
                    }
                )
            )
            guard UInt64(data.count) <= Self.maximumStateBytes else {
                throw CanonicalSourceRegistryError.stateUnavailable
            }
            let temporary = directory.appendingPathComponent(
                ".canonical-sources-\(UUID().uuidString).tmp"
            )
            try data.write(to: temporary, options: [.atomic])
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o600], ofItemAtPath: temporary.path
            )
            if FileManager.default.fileExists(atPath: stateFile.path) {
                _ = try FileManager.default.replaceItemAt(stateFile, withItemAt: temporary)
            } else {
                try FileManager.default.moveItem(at: temporary, to: stateFile)
            }
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o600], ofItemAtPath: stateFile.path
            )
            let descriptor = Darwin.open(directory.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
            if descriptor >= 0 {
                _ = Darwin.fsync(descriptor)
                Darwin.close(descriptor)
            }
        } catch let error as CanonicalSourceRegistryError {
            throw error
        } catch {
            throw CanonicalSourceRegistryError.stateUnavailable
        }
    }

    private static func load(_ stateFile: URL) throws -> [String: CanonicalSourceRecord] {
        guard FileManager.default.fileExists(atPath: stateFile.path) else { return [:] }
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: stateFile.path)
            guard (attributes[.type] as? FileAttributeType) == .typeRegular,
                  let size = attributes[.size] as? NSNumber,
                  size.uint64Value <= maximumStateBytes,
                  let owner = attributes[.ownerAccountID] as? NSNumber,
                  owner.uint32Value == Darwin.getuid(),
                  let permissions = attributes[.posixPermissions] as? NSNumber,
                  permissions.intValue & 0o077 == 0
            else { throw CanonicalSourceRegistryError.invalidState }
            let document = try JSONDecoder().decode(
                CanonicalSourceDocument.self,
                from: Data(contentsOf: stateFile, options: [.mappedIfSafe])
            )
            guard document.schema == schema else { throw CanonicalSourceRegistryError.invalidState }
            var result: [String: CanonicalSourceRecord] = [:]
            for value in document.sources {
                try validate(value)
                let key = key(value.workspaceRef, value.nativeKey)
                guard result[key] == nil else { throw CanonicalSourceRegistryError.invalidState }
                result[key] = value
            }
            return result
        } catch let error as CanonicalSourceRegistryError {
            throw error
        } catch {
            throw CanonicalSourceRegistryError.invalidState
        }
    }

    private static func validate(_ value: CanonicalSourceRecord) throws {
        guard validIdentifier(value.linkedDocumentID),
              validIdentifier(value.requestedSourceIdentity),
              validIdentifier(value.workspaceRef),
              !value.nativeKey.isEmpty, value.nativeKey.utf8.count <= 128
        else { throw CanonicalSourceRegistryError.invalidState }
    }

    private static func validIdentifier(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= 128
            && value.unicodeScalars.allSatisfy {
                CharacterSet(
                    charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"
                ).contains($0)
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
                throw CanonicalSourceRegistryError.stateUnavailable
            }
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o700], ofItemAtPath: directory.path
            )
        } catch let error as CanonicalSourceRegistryError {
            throw error
        } catch {
            throw CanonicalSourceRegistryError.stateUnavailable
        }
    }
}
