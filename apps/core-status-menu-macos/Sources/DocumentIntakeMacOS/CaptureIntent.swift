import CryptoKit
import Foundation

private struct CaptureIntentAnyKey: CodingKey {
    let stringValue: String
    let intValue: Int? = nil

    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

public enum CaptureIntentError: Error, Equatable {
    case invalidShape
    case invalidIdentifier
    case invalidDigest
    case invalidTimestamp
    case invalidLineage
    case metadataTooLarge
}

public enum CaptureSurface: String, Codable, Sendable {
    case finderService = "finder_service"
    case finderQuickAction = "finder_quick_action"
    case manual
    case menuDrop = "menu_drop"
    case capturePanelDrop = "capture_panel_drop"
    case share
    case shortcut
    case folderImport = "folder_import"
    case automaticFile = "automatic_file"
    case automaticFolder = "automatic_folder"
}

public enum CaptureContentKind: String, Codable, Sendable {
    case text
    case markdown
    case link
    case pdf
    case docx
    case xlsx
    case pptx
    case csv
    case tsv
    case rtf
    case html
    case image
}

public enum CaptureIntentKind: String, Codable, Sendable {
    case newCapture = "new_capture"
    case refresh
    case automaticCapture = "automatic_capture"
    case folderCapture = "folder_capture"
}

public struct CallerContextBinding: Codable, Equatable, Sendable {
    public let clientContextRef: String
    public let principalRef: String
    public let contextRevision: String

    public init(clientContextRef: String, principalRef: String, contextRevision: String) {
        self.clientContextRef = clientContextRef
        self.principalRef = principalRef
        self.contextRevision = contextRevision
    }
}

public struct ProcessingProfileBinding: Codable, Equatable, Sendable {
    public let profileRef: String
    public let revision: String

    public init(profileRef: String, revision: String) {
        self.profileRef = profileRef
        self.revision = revision
    }
}

public struct OriginalBindingIntent: Codable, Equatable, Sendable {
    public let accessRecordId: String
    public let bindingGeneration: UInt64
    public let linkedDocumentId: String?
    public let rootAccessGroupId: String?
    public let rootBindingGeneration: UInt64?
    public let memberId: String?
    public let membershipGeneration: UInt64?

    public init(
        accessRecordId: String,
        bindingGeneration: UInt64,
        linkedDocumentId: String? = nil,
        rootAccessGroupId: String? = nil,
        rootBindingGeneration: UInt64? = nil,
        memberId: String? = nil,
        membershipGeneration: UInt64? = nil
    ) {
        self.accessRecordId = accessRecordId
        self.bindingGeneration = bindingGeneration
        self.linkedDocumentId = linkedDocumentId
        self.rootAccessGroupId = rootAccessGroupId
        self.rootBindingGeneration = rootBindingGeneration
        self.memberId = memberId
        self.membershipGeneration = membershipGeneration
    }
}

public struct ConsentBinding: Codable, Equatable, Sendable {
    public let subscriptionId: String
    public let consentGeneration: UInt64
    public let configGeneration: UInt64
    public let dirtyEpoch: UInt64

    public init(
        subscriptionId: String,
        consentGeneration: UInt64,
        configGeneration: UInt64,
        dirtyEpoch: UInt64
    ) {
        self.subscriptionId = subscriptionId
        self.consentGeneration = consentGeneration
        self.configGeneration = configGeneration
        self.dirtyEpoch = dirtyEpoch
    }
}

public struct BulkOriginBinding: Codable, Equatable, Sendable {
    public let bulkRunId: String
    public let manifestRevision: String
    public let memberId: String

    public init(bulkRunId: String, manifestRevision: String, memberId: String) {
        self.bulkRunId = bulkRunId
        self.manifestRevision = manifestRevision
        self.memberId = memberId
    }
}

/// Distinguishes a creation precondition (field omitted) from an existing-document precondition
/// whose successful head was explicitly observed as either an ID or null.
public enum CaptureBasePrecondition: Equatable, Sendable {
    case create
    case expected(String?)
}

public struct CaptureIntentV3: Codable, Equatable, Sendable {
    public static let schema = "omnivia.macos.capture-intent.v3"
    public static let maximumMetadataBytes = 64 * 1024
    public static let maximumPayloadBytes: UInt64 = 50 * 1024 * 1024

    public let submissionId: String
    public let itemId: String
    public let targetRef: String
    public let workspaceRef: String
    public let targetBindingRevision: String
    public let callerContextBinding: CallerContextBinding
    public let surface: CaptureSurface
    public let contentKind: CaptureContentKind
    public let payloadRef: String
    public let byteLength: UInt64
    public let localIntegrityDigest: String
    public let requestedSourceIdentity: String
    public let operationKey: String
    public let intentKind: CaptureIntentKind
    public let basePrecondition: CaptureBasePrecondition
    public let processingProfile: ProcessingProfileBinding
    public let originalBinding: OriginalBindingIntent?
    public let consent: ConsentBinding?
    public let bulkOrigin: BulkOriginBinding?
    public let createdAt: String
    public let displayName: String?

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schema
        case submissionId
        case itemId
        case targetRef
        case workspaceRef
        case targetBindingRevision
        case callerContextBinding
        case surface
        case contentKind
        case payloadRef
        case byteLength
        case localIntegrityDigest
        case requestedSourceIdentity
        case operationKey
        case intentKind
        case expectedSuccessfulCaptureId
        case processingProfile
        case originalBinding
        case consent
        case bulkOrigin
        case createdAt
        case displayName
    }

    public init(
        submissionId: String,
        itemId: String,
        targetRef: String,
        workspaceRef: String,
        targetBindingRevision: String,
        callerContextBinding: CallerContextBinding,
        surface: CaptureSurface,
        contentKind: CaptureContentKind,
        payloadRef: String,
        byteLength: UInt64,
        localIntegrityDigest: String,
        requestedSourceIdentity: String,
        operationKey: String,
        intentKind: CaptureIntentKind,
        basePrecondition: CaptureBasePrecondition,
        processingProfile: ProcessingProfileBinding,
        originalBinding: OriginalBindingIntent? = nil,
        consent: ConsentBinding? = nil,
        bulkOrigin: BulkOriginBinding? = nil,
        createdAt: String,
        displayName: String? = nil
    ) throws {
        self.submissionId = submissionId
        self.itemId = itemId
        self.targetRef = targetRef
        self.workspaceRef = workspaceRef
        self.targetBindingRevision = targetBindingRevision
        self.callerContextBinding = callerContextBinding
        self.surface = surface
        self.contentKind = contentKind
        self.payloadRef = payloadRef
        self.byteLength = byteLength
        self.localIntegrityDigest = localIntegrityDigest
        self.requestedSourceIdentity = requestedSourceIdentity
        self.operationKey = operationKey
        self.intentKind = intentKind
        self.basePrecondition = basePrecondition
        self.processingProfile = processingProfile
        self.originalBinding = originalBinding
        self.consent = consent
        self.bulkOrigin = bulkOrigin
        self.createdAt = createdAt
        self.displayName = displayName
        try validate()
    }

    public init(from decoder: Decoder) throws {
        let raw = try decoder.container(keyedBy: CaptureIntentAnyKey.self)
        let allowed = Set(CodingKeys.allCases.map(\.rawValue))
        guard Set(raw.allKeys.map(\.stringValue)).isSubset(of: allowed) else {
            throw CaptureIntentError.invalidShape
        }
        let container = try decoder.container(keyedBy: CodingKeys.self)
        guard try container.decode(String.self, forKey: .schema) == Self.schema else {
            throw CaptureIntentError.invalidShape
        }
        submissionId = try container.decode(String.self, forKey: .submissionId)
        itemId = try container.decode(String.self, forKey: .itemId)
        targetRef = try container.decode(String.self, forKey: .targetRef)
        workspaceRef = try container.decode(String.self, forKey: .workspaceRef)
        targetBindingRevision = try container.decode(String.self, forKey: .targetBindingRevision)
        callerContextBinding = try container.decode(CallerContextBinding.self, forKey: .callerContextBinding)
        surface = try container.decode(CaptureSurface.self, forKey: .surface)
        contentKind = try container.decode(CaptureContentKind.self, forKey: .contentKind)
        payloadRef = try container.decode(String.self, forKey: .payloadRef)
        byteLength = try container.decode(UInt64.self, forKey: .byteLength)
        localIntegrityDigest = try container.decode(String.self, forKey: .localIntegrityDigest)
        requestedSourceIdentity = try container.decode(String.self, forKey: .requestedSourceIdentity)
        operationKey = try container.decode(String.self, forKey: .operationKey)
        intentKind = try container.decode(CaptureIntentKind.self, forKey: .intentKind)
        if container.contains(.expectedSuccessfulCaptureId) {
            basePrecondition = .expected(
                try container.decodeIfPresent(String.self, forKey: .expectedSuccessfulCaptureId)
            )
        } else {
            basePrecondition = .create
        }
        processingProfile = try container.decode(ProcessingProfileBinding.self, forKey: .processingProfile)
        originalBinding = try container.decodeIfPresent(OriginalBindingIntent.self, forKey: .originalBinding)
        consent = try container.decodeIfPresent(ConsentBinding.self, forKey: .consent)
        bulkOrigin = try container.decodeIfPresent(BulkOriginBinding.self, forKey: .bulkOrigin)
        createdAt = try container.decode(String.self, forKey: .createdAt)
        displayName = try container.decodeIfPresent(String.self, forKey: .displayName)
        try validate()
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(Self.schema, forKey: .schema)
        try container.encode(submissionId, forKey: .submissionId)
        try container.encode(itemId, forKey: .itemId)
        try container.encode(targetRef, forKey: .targetRef)
        try container.encode(workspaceRef, forKey: .workspaceRef)
        try container.encode(targetBindingRevision, forKey: .targetBindingRevision)
        try container.encode(callerContextBinding, forKey: .callerContextBinding)
        try container.encode(surface, forKey: .surface)
        try container.encode(contentKind, forKey: .contentKind)
        try container.encode(payloadRef, forKey: .payloadRef)
        try container.encode(byteLength, forKey: .byteLength)
        try container.encode(localIntegrityDigest, forKey: .localIntegrityDigest)
        try container.encode(requestedSourceIdentity, forKey: .requestedSourceIdentity)
        try container.encode(operationKey, forKey: .operationKey)
        try container.encode(intentKind, forKey: .intentKind)
        if case let .expected(value) = basePrecondition {
            try container.encode(value, forKey: .expectedSuccessfulCaptureId)
        }
        try container.encode(processingProfile, forKey: .processingProfile)
        try container.encodeIfPresent(originalBinding, forKey: .originalBinding)
        try container.encodeIfPresent(consent, forKey: .consent)
        try container.encodeIfPresent(bulkOrigin, forKey: .bulkOrigin)
        try container.encode(createdAt, forKey: .createdAt)
        try container.encodeIfPresent(displayName, forKey: .displayName)
    }

    public func canonicalData() throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let data = try encoder.encode(self)
        guard data.count <= Self.maximumMetadataBytes else { throw CaptureIntentError.metadataTooLarge }
        return data
    }

    public func requestDigest() throws -> String {
        let digest = SHA256.hash(data: try canonicalData())
        return "sha256:" + digest.map { String(format: "%02x", $0) }.joined()
    }

    public func validate() throws {
        let identifiers = [
            submissionId,
            itemId,
            targetRef,
            workspaceRef,
            targetBindingRevision,
            payloadRef,
            requestedSourceIdentity,
            operationKey,
            callerContextBinding.clientContextRef,
            callerContextBinding.principalRef,
            callerContextBinding.contextRevision,
            processingProfile.profileRef,
            processingProfile.revision,
        ]
        guard identifiers.allSatisfy(Self.validIdentifier) else {
            throw CaptureIntentError.invalidIdentifier
        }
        guard byteLength > 0, byteLength <= Self.maximumPayloadBytes else {
            throw CaptureIntentError.invalidShape
        }
        guard Self.validDigest(localIntegrityDigest) else { throw CaptureIntentError.invalidDigest }
        guard ISO8601DateFormatter().date(from: createdAt) != nil else {
            throw CaptureIntentError.invalidTimestamp
        }
        if let displayName, displayName.isEmpty || displayName.count > 255 || displayName.contains("\0") {
            throw CaptureIntentError.invalidShape
        }
        switch (intentKind, basePrecondition) {
        case (.newCapture, .create):
            break
        case (.newCapture, _), (_, .create):
            throw CaptureIntentError.invalidLineage
        case (_, let .expected(value)):
            if let value, !Self.validCaptureID(value) { throw CaptureIntentError.invalidLineage }
        }
        if let binding = originalBinding {
            guard Self.validIdentifier(binding.accessRecordId), binding.bindingGeneration > 0 else {
                throw CaptureIntentError.invalidLineage
            }
            if let document = binding.linkedDocumentId, !Self.validIdentifier(document) {
                throw CaptureIntentError.invalidLineage
            }
            let rootValues = [
                binding.rootAccessGroupId != nil,
                binding.rootBindingGeneration != nil,
                binding.memberId != nil,
                binding.membershipGeneration != nil,
            ]
            guard rootValues.allSatisfy({ $0 }) || rootValues.allSatisfy({ !$0 }) else {
                throw CaptureIntentError.invalidLineage
            }
            if let root = binding.rootAccessGroupId,
               !Self.validIdentifier(root)
                || !Self.validIdentifier(binding.memberId!)
                || binding.rootBindingGeneration == 0
                || binding.membershipGeneration == 0
            {
                throw CaptureIntentError.invalidLineage
            }
        }
        let automaticSurface = surface == .automaticFile || surface == .automaticFolder
        guard !automaticSurface || (consent != nil && originalBinding != nil) else {
            throw CaptureIntentError.invalidLineage
        }
        guard automaticSurface || consent == nil else { throw CaptureIntentError.invalidLineage }
        if surface == .automaticFile, intentKind != .automaticCapture {
            throw CaptureIntentError.invalidLineage
        }
        if surface == .automaticFolder, intentKind != .folderCapture {
            throw CaptureIntentError.invalidLineage
        }
        if surface == .folderImport, intentKind != .folderCapture {
            throw CaptureIntentError.invalidLineage
        }
        if let consent {
            guard Self.validIdentifier(consent.subscriptionId),
                  consent.consentGeneration > 0,
                  consent.configGeneration > 0,
                  consent.dirtyEpoch > 0
            else { throw CaptureIntentError.invalidLineage }
        }
        if surface == .folderImport || surface == .automaticFolder {
            guard bulkOrigin != nil, originalBinding?.rootAccessGroupId != nil else {
                throw CaptureIntentError.invalidLineage
            }
        } else if bulkOrigin != nil {
            throw CaptureIntentError.invalidLineage
        }
        if let bulkOrigin {
            guard Self.validIdentifier(bulkOrigin.bulkRunId),
                  Self.validIdentifier(bulkOrigin.manifestRevision),
                  Self.validIdentifier(bulkOrigin.memberId)
            else { throw CaptureIntentError.invalidLineage }
        }
    }

    private static func validIdentifier(_ value: String) -> Bool {
        guard !value.isEmpty, value.utf8.count <= 128,
              let first = value.unicodeScalars.first,
              asciiAlphanumeric(first)
        else { return false }
        return value.unicodeScalars.allSatisfy {
            asciiAlphanumeric($0) || $0 == "." || $0 == "_" || $0 == ":" || $0 == "-"
        }
    }

    private static func asciiAlphanumeric(_ scalar: Unicode.Scalar) -> Bool {
        (65...90).contains(scalar.value)
            || (97...122).contains(scalar.value)
            || (48...57).contains(scalar.value)
    }

    private static func validDigest(_ value: String) -> Bool {
        value.count == 71
            && value.hasPrefix("sha256:")
            && value.dropFirst(7).allSatisfy { $0.isHexDigit && !$0.isUppercase }
    }

    private static func validCaptureID(_ value: String) -> Bool {
        value.count == 69
            && value.hasPrefix("dcap-")
            && value.dropFirst(5).allSatisfy { $0.isHexDigit && !$0.isUppercase }
    }
}
