import Darwin
import Foundation

/// Minimal owner-only tombstone for bytes Core says were removed or are no
/// longer retained. It deliberately stores no source contents or local path.
public struct AutomaticCaptureSuppression: Codable, Equatable, Sendable {
    public let suppressionRef: String
    public let workspaceRef: String
    public let requestedSourceIdentity: String
    public let observedDigest: String
    public let reason: CurrentCaptureShortfall
    public let recordedAt: String
}

public enum AutomaticCaptureSuppressionStoreError: Error, Equatable {
    case invalidState
    case stateUnavailable
}

private struct AutomaticCaptureSuppressionDocument: Codable {
    let schema: String
    let suppressions: [AutomaticCaptureSuppression]
}

/// Shared by every automatic route so a retention/removal decision learned by
/// one route also governs overlapping roots, direct watches and later renames.
public final class AutomaticCaptureSuppressionStore: @unchecked Sendable {
    private static let schema = "omnivia.automatic-capture-suppressions.v1"
    private let gate: PublicationGate
    private let directory: URL
    private let stateFile: URL
    private var values: [String: AutomaticCaptureSuppression]

    public init(stateDirectory: URL, gate: PublicationGate = .processShared) throws {
        self.gate = gate
        directory = stateDirectory
        stateFile = stateDirectory.appendingPathComponent("automatic-capture-suppressions-v1.json")
        try Self.prepareDirectory(stateDirectory)
        values = try Self.load(stateFile)
    }

    public func all() -> [AutomaticCaptureSuppression] {
        gate.withLock {
            values.values.sorted {
                ($0.workspaceRef, $0.requestedSourceIdentity, $0.observedDigest)
                    < ($1.workspaceRef, $1.requestedSourceIdentity, $1.observedDigest)
            }
        }
    }

    public func suppression(
        workspaceRef: String,
        requestedSourceIdentity: String,
        observedDigest: String
    ) -> AutomaticCaptureSuppression? {
        gate.withLock {
            values[Self.key(
                workspaceRef: workspaceRef,
                requestedSourceIdentity: requestedSourceIdentity,
                observedDigest: observedDigest
            )]
        }
    }

    public func hasSuppression(
        workspaceRef: String,
        requestedSourceIdentity: String
    ) -> Bool {
        gate.withLock {
            values.values.contains {
                $0.workspaceRef == workspaceRef
                    && $0.requestedSourceIdentity == requestedSourceIdentity
            }
        }
    }

    /// Clears only the exact tombstone after Core authoritatively reports that
    /// this capture is current again (for example after governed restoration).
    /// Merely changing a local path or seeing the bytes again cannot call this.
    public func clear(
        workspaceRef: String,
        requestedSourceIdentity: String,
        observedDigest: String
    ) throws {
        let key = Self.key(
            workspaceRef: workspaceRef,
            requestedSourceIdentity: requestedSourceIdentity,
            observedDigest: observedDigest
        )
        try gate.withLock {
            guard values[key] != nil else { return }
            var replacement = values
            replacement.removeValue(forKey: key)
            try persist(replacement)
            values = replacement
        }
    }

    /// Records only governed removal/retention outcomes. Other freshness
    /// failures are not deletion tombstones and must not become suppression.
    @discardableResult
    public func record(
        workspaceRef: String,
        requestedSourceIdentity: String,
        observedDigest: String,
        reason: CurrentCaptureShortfall,
        recordedAt: String
    ) throws -> AutomaticCaptureSuppression? {
        guard reason == .sourceRemoved || reason == .captureNotRetained else { return nil }
        let key = Self.key(
            workspaceRef: workspaceRef,
            requestedSourceIdentity: requestedSourceIdentity,
            observedDigest: observedDigest
        )
        return try gate.withLock {
            if let existing = values[key] { return existing }
            let value = AutomaticCaptureSuppression(
                suppressionRef: "suppression-" + UUID().uuidString.lowercased(),
                workspaceRef: workspaceRef,
                requestedSourceIdentity: requestedSourceIdentity,
                observedDigest: observedDigest,
                reason: reason,
                recordedAt: recordedAt
            )
            try Self.validate(value)
            var replacement = values
            replacement[key] = value
            try persist(replacement)
            values = replacement
            return value
        }
    }

    private func persist(_ values: [String: AutomaticCaptureSuppression]) throws {
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
            let data = try encoder.encode(
                AutomaticCaptureSuppressionDocument(
                    schema: Self.schema,
                    suppressions: values.values.sorted { $0.suppressionRef < $1.suppressionRef }
                )
            )
            guard data.count <= 4 * 1024 * 1024 else {
                throw AutomaticCaptureSuppressionStoreError.stateUnavailable
            }
            let temporary = directory.appendingPathComponent(".automatic-suppressions-\(UUID().uuidString).tmp")
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
        } catch let error as AutomaticCaptureSuppressionStoreError {
            throw error
        } catch {
            throw AutomaticCaptureSuppressionStoreError.stateUnavailable
        }
    }

    private static func load(_ stateFile: URL) throws -> [String: AutomaticCaptureSuppression] {
        guard FileManager.default.fileExists(atPath: stateFile.path) else { return [:] }
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: stateFile.path)
            guard (attributes[.type] as? FileAttributeType) == .typeRegular,
                  let size = attributes[.size] as? NSNumber,
                  size.uint64Value <= 4 * 1024 * 1024,
                  let owner = attributes[.ownerAccountID] as? NSNumber,
                  owner.uint32Value == Darwin.getuid(),
                  let permissions = attributes[.posixPermissions] as? NSNumber,
                  permissions.intValue & 0o077 == 0
            else { throw AutomaticCaptureSuppressionStoreError.invalidState }
            let document = try JSONDecoder().decode(
                AutomaticCaptureSuppressionDocument.self,
                from: Data(contentsOf: stateFile, options: [.mappedIfSafe])
            )
            guard document.schema == schema else {
                throw AutomaticCaptureSuppressionStoreError.invalidState
            }
            var result: [String: AutomaticCaptureSuppression] = [:]
            for value in document.suppressions {
                try validate(value)
                let key = key(
                    workspaceRef: value.workspaceRef,
                    requestedSourceIdentity: value.requestedSourceIdentity,
                    observedDigest: value.observedDigest
                )
                guard result[key] == nil else {
                    throw AutomaticCaptureSuppressionStoreError.invalidState
                }
                result[key] = value
            }
            return result
        } catch let error as AutomaticCaptureSuppressionStoreError {
            throw error
        } catch {
            throw AutomaticCaptureSuppressionStoreError.invalidState
        }
    }

    private static func validate(_ value: AutomaticCaptureSuppression) throws {
        guard validIdentifier(value.suppressionRef),
              validIdentifier(value.workspaceRef),
              validIdentifier(value.requestedSourceIdentity),
              value.observedDigest.hasPrefix("sha256:"),
              value.observedDigest.count == 71,
              value.observedDigest.dropFirst(7).allSatisfy({ $0.isHexDigit }),
              ISO8601DateFormatter().date(from: value.recordedAt) != nil
        else { throw AutomaticCaptureSuppressionStoreError.invalidState }
    }

    private static func key(
        workspaceRef: String,
        requestedSourceIdentity: String,
        observedDigest: String
    ) -> String {
        workspaceRef + "\u{1f}" + requestedSourceIdentity + "\u{1f}" + observedDigest
    }

    private static func validIdentifier(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= 128
            && value.unicodeScalars.allSatisfy {
                CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
                    .contains($0)
            }
    }

    private static func prepareDirectory(_ directory: URL) throws {
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
    }
}
