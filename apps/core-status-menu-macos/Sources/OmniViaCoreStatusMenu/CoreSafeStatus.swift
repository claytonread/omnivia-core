import Foundation

/// The Swift mirror of the canonical `CoreTargetV1` and `CoreSafeStatusV1`
/// contracts (ADR-038), and the only shape of Core this companion may see.
///
/// The companion authenticates nothing, so every vocabulary here is closed and
/// every scalar is bounded: an unknown enum value, an out-of-bounds scalar or a
/// contract version this build was not written against fails closed rather than
/// reaching the menu. The validation is a small local one — the canonical
/// schemas are JSON Schema and the generated types are Python, and neither is a
/// dependency this package may take — so it mirrors the declared closed domains,
/// the declared scalar patterns and bounds, the closed object shapes
/// (`unevaluatedProperties: false`), and the two cross-field invariants
/// (`contract_version` agreement, and process-lifecycle actions only for a
/// locally-managed local target).

/// The one Application Contract version this mirror was written against. A
/// document at any other version — minor or major — is refused: this build
/// validates the shape it was written against and cannot vouch for another.
let supportedApplicationContractVersion = "1.3"

/// The single, wordless refusal. Nothing about *why* a document was refused may
/// reach the user, so nothing here carries a reason to leak.
struct CoreContractViolation: Error, Equatable {}

enum CoreTargetKind: String, Decodable, Sendable {
    case local
    case privateRemote = "private_remote"
    case cloud
}

enum CoreTargetManagement: String, Decodable, Sendable {
    case locallyManaged = "locally_managed"
    case externallyManaged = "externally_managed"
}

enum CoreLifecycleState: String, Decodable, Sendable {
    case starting, running, stopping, stopped, failed, unknown
}

enum CoreReadinessState: String, Decodable, Sendable {
    case ready
    case notReady = "not_ready"
    case unknown
}

enum CoreCompatibilityState: String, Decodable, Sendable {
    case compatible
    case compatibleWithDeprecations = "compatible_with_deprecations"
    case upgradeRequired = "upgrade_required"
    case incompatible
    case unknown
}

enum CoreConnectionState: String, Decodable, Sendable {
    case connected, connecting, disconnected, unreachable
    case authenticationRequired = "authentication_required"
    case unknown
}

/// Declared most-actionable first: the menu shows the first of these a status
/// carries, so the order is this build's own and never the document's.
enum CoreSafeWarningCode: String, Decodable, CaseIterable, Sendable {
    case authenticationRequired = "authentication_required"
    case versionIncompatible = "version_incompatible"
    case upgradeRequired = "upgrade_required"
    case workspaceFormatIncompatible = "workspace_format_incompatible"
    case endpointUnreachable = "endpoint_unreachable"
    case degraded
}

enum CoreSafeAction: String, Decodable, Sendable {
    case start, stop, restart, reconnect, open
}

/// Process-lifecycle actions: safe only for a target this client both owns and
/// can reach on this host.
private let localOnlyActions: Set<CoreSafeAction> = [.start, .stop, .restart]

private let maxWarningCodes = 32
private let maxPermittedActions = 16
private let displayNameBounds = 1...256

struct CoreTargetV1: Decodable, Equatable, Sendable {
    let contractVersion: String
    let targetRef: String
    let displayName: String
    let kind: CoreTargetKind
    let workspaceRef: String
    let management: CoreTargetManagement
    let endpointProfileRef: String

    enum CodingKeys: String, CodingKey, CaseIterable {
        case contractVersion, targetRef, displayName, kind
        case workspaceRef, management, endpointProfileRef
    }

    init(from decoder: Decoder) throws {
        try decoder.rejectUnknownKeys(CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        contractVersion = try container.decode(String.self, forKey: .contractVersion)
        targetRef = try container.decode(String.self, forKey: .targetRef)
        displayName = try container.decode(String.self, forKey: .displayName)
        kind = try container.decode(CoreTargetKind.self, forKey: .kind)
        workspaceRef = try container.decode(String.self, forKey: .workspaceRef)
        management = try container.decode(CoreTargetManagement.self, forKey: .management)
        endpointProfileRef = try container.decode(String.self, forKey: .endpointProfileRef)
    }

    /// `display_name` is validated but never rendered: it is bounded free text,
    /// and the menu's words are this build's own fixed strings.
    func validate() throws {
        guard isContractVersion(contractVersion),
              isIdentifier(targetRef),
              displayNameBounds.contains(displayName.unicodeScalars.count),
              isWorkspaceID(workspaceRef),
              isIdentifier(endpointProfileRef)
        else { throw CoreContractViolation() }
    }
}

struct CoreSafeStatusV1: Decodable, Equatable, Sendable {
    let contractVersion: String
    let target: CoreTargetV1
    let lifecycleState: CoreLifecycleState
    let readinessState: CoreReadinessState
    let compatibilityState: CoreCompatibilityState
    let connectionState: CoreConnectionState
    let serverVersion: String?
    let protocolVersion: String?
    let warningCodes: [CoreSafeWarningCode]
    let permittedActions: [CoreSafeAction]

    enum CodingKeys: String, CodingKey, CaseIterable {
        case contractVersion, target, lifecycleState, readinessState, compatibilityState
        case connectionState, serverVersion, protocolVersion, warningCodes, permittedActions
    }

    init(from decoder: Decoder) throws {
        try decoder.rejectUnknownKeys(CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        contractVersion = try container.decode(String.self, forKey: .contractVersion)
        target = try container.decode(CoreTargetV1.self, forKey: .target)
        lifecycleState = try container.decode(CoreLifecycleState.self, forKey: .lifecycleState)
        readinessState = try container.decode(CoreReadinessState.self, forKey: .readinessState)
        compatibilityState = try container.decode(
            CoreCompatibilityState.self, forKey: .compatibilityState
        )
        connectionState = try container.decode(CoreConnectionState.self, forKey: .connectionState)
        serverVersion = try container.decodeIfPresent(String.self, forKey: .serverVersion)
        protocolVersion = try container.decodeIfPresent(String.self, forKey: .protocolVersion)
        warningCodes = try container.decode([CoreSafeWarningCode].self, forKey: .warningCodes)
        permittedActions = try container.decode([CoreSafeAction].self, forKey: .permittedActions)
    }

    func validate() throws {
        try target.validate()
        guard isContractVersion(contractVersion),
              contractVersion == target.contractVersion,
              isSupportedContractVersion(contractVersion),
              serverVersion.map(isReleaseVersion) ?? true,
              protocolVersion.map(isContractVersion) ?? true,
              warningCodes.count <= maxWarningCodes,
              Set(warningCodes).count == warningCodes.count,
              permittedActions.count <= maxPermittedActions,
              Set(permittedActions).count == permittedActions.count
        else { throw CoreContractViolation() }

        let ownsTheProcess = target.kind == .local && target.management == .locallyManaged
        guard ownsTheProcess || Set(permittedActions).isDisjoint(with: localOnlyActions) else {
            throw CoreContractViolation()
        }
    }
}

// MARK: - Closed objects

/// Every canonical object here declares `unevaluatedProperties: false`, but
/// `Decodable` ignores keys it has no property for. A key this build does not
/// know is a document this build does not understand, so it fails closed with
/// the same wordless refusal as any other violation.
extension Decoder {
    func rejectUnknownKeys<Key: CodingKey & CaseIterable>(_: Key.Type) throws {
        let known = Set(Key.allCases.map(\.stringValue))
        let present = try container(keyedBy: AnyCodingKey.self).allKeys
        guard present.allSatisfy({ known.contains($0.stringValue) }) else {
            throw CoreContractViolation()
        }
    }
}

/// Accepts any key, so `allKeys` reports what the document actually carries.
/// The decoder's snake_case conversion has already run by this point, so these
/// are the converted names the `CodingKeys` above are written in.
private struct AnyCodingKey: CodingKey {
    let stringValue: String
    var intValue: Int? { nil }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { nil }
}

// MARK: - Scalar value domains

/// `Identifier` / `WorkspaceId`: 1..128 characters, ASCII alphanumeric first,
/// then ASCII alphanumerics and `. _ : -`.
func isIdentifier(_ value: String) -> Bool {
    guard (1...128).contains(value.unicodeScalars.count) else { return false }
    let bytes = Array(value.utf8)
    guard let first = bytes.first, isASCIIAlphanumeric(first) else { return false }
    return bytes.allSatisfy { byte in
        isASCIIAlphanumeric(byte) || byte == UInt8(ascii: ".") || byte == UInt8(ascii: "_")
            || byte == UInt8(ascii: ":") || byte == UInt8(ascii: "-")
    }
}

/// `WorkspaceId` declares the same domain as `Identifier`; named separately so
/// a future divergence has one place to land.
func isWorkspaceID(_ value: String) -> Bool { isIdentifier(value) }

/// `ContractVersion`: `major.minor`, no leading zeros, at most 32 characters.
func isContractVersion(_ value: String) -> Bool {
    guard value.unicodeScalars.count <= 32 else { return false }
    let parts = value.split(separator: ".", omittingEmptySubsequences: false)
    return parts.count == 2 && parts.allSatisfy(isNumericIdentifier)
}

/// `ReleaseVersion`: SemVer 2.0.0, at most 128 characters.
func isReleaseVersion(_ value: String) -> Bool {
    guard value.unicodeScalars.count <= 128 else { return false }
    var core = Substring(value)
    if let plus = core.firstIndex(of: "+") {
        guard isDotSeparatedIdentifiers(core[core.index(after: plus)...], numericMayLeadWithZero: true)
        else { return false }
        core = core[..<plus]
    }
    if let dash = core.firstIndex(of: "-") {
        guard isDotSeparatedIdentifiers(core[core.index(after: dash)...], numericMayLeadWithZero: false)
        else { return false }
        core = core[..<dash]
    }
    let numbers = core.split(separator: ".", omittingEmptySubsequences: false)
    return numbers.count == 3 && numbers.allSatisfy(isNumericIdentifier)
}

/// Exactly the version this build mirrors, and nothing else.
func isSupportedContractVersion(_ value: String) -> Bool {
    value == supportedApplicationContractVersion
}

private func isASCIIAlphanumeric(_ byte: UInt8) -> Bool {
    (UInt8(ascii: "A")...UInt8(ascii: "Z")).contains(byte)
        || (UInt8(ascii: "a")...UInt8(ascii: "z")).contains(byte)
        || isASCIIDigit(byte)
}

private func isASCIIDigit(_ byte: UInt8) -> Bool {
    (UInt8(ascii: "0")...UInt8(ascii: "9")).contains(byte)
}

private func isNumericIdentifier(_ part: Substring) -> Bool {
    guard !part.isEmpty, part.utf8.allSatisfy(isASCIIDigit) else { return false }
    return part.count == 1 || part.first != "0"
}

private func isDotSeparatedIdentifiers(
    _ value: Substring,
    numericMayLeadWithZero: Bool
) -> Bool {
    let parts = value.split(separator: ".", omittingEmptySubsequences: false)
    guard !parts.isEmpty else { return false }
    return parts.allSatisfy { part in
        guard !part.isEmpty else { return false }
        let allowed = part.utf8.allSatisfy { byte in
            isASCIIAlphanumeric(byte) || byte == UInt8(ascii: "-")
        }
        guard allowed else { return false }
        if numericMayLeadWithZero || !part.utf8.allSatisfy(isASCIIDigit) { return true }
        return isNumericIdentifier(part)
    }
}
