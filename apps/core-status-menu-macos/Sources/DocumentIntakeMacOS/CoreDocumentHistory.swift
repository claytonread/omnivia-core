import CryptoKit
import Foundation

/// The canonical intake history Core holds for one document, and the one explicit
/// reprocess it authorises.
///
/// Everything in this file is *Core's* record, and it is deliberately a separate
/// vocabulary from `CaptureActivityRecord`. Local activity says what this Mac did with a
/// submission; this says what Core did with the bytes it admitted. They are never merged:
/// a local `processing` receipt is not an extraction, and a Core extraction is not proof
/// that this Mac's queue is empty. The management window labels them separately for the
/// same reason.
///
/// Every vocabulary here is closed and every object refuses a field this build does not
/// define. A state Core adds in a later minor release is a state this build cannot
/// describe, and describing it anyway -- as a default, or as the nearest value it does
/// know -- is exactly how a historical-only extraction would come to read as a success.
/// It fails closed instead.

/// The application contract version this file mirrors. A document from another one is
/// refused rather than read as a subset.
public let supportedCoreHistoryContractVersion = "1.3"

/// The closed set of ways this companion ends up with no Core history to show.
///
/// `refused` carries the service's own frozen error code and nothing else: the code is a
/// closed contract vocabulary the window maps to copy, while the service's message may
/// quote values that are not this surface's to display.
public enum CoreHistoryError: Error, Equatable, Sendable {
    /// No `omnivia` executable could be resolved, or it could not be started.
    case unavailable
    case timedOut
    /// The answer did not decode, did not verify, or named a state this build cannot
    /// describe. The decoder's own complaint is dropped rather than carried.
    case unreadable
    case refused(code: String)

    /// Whether another attempt could plausibly answer. A refusal is Core's decision and
    /// repeating it changes nothing; an unavailable adapter or a timeout might.
    public var mayRetry: Bool {
        switch self {
        case .unavailable, .timedOut: true
        case .unreadable, .refused: false
        }
    }
}

// MARK: - Closed state vocabularies

public enum CoreSourceState: String, Codable, Sendable {
    case active
    case removed
}

/// An aggregate over this document's original routes. Core returns no route reference,
/// path or bookmark; this one word is the whole of what it says about them.
public enum CoreBindingState: String, Codable, Sendable {
    case noRoute = "none"
    case connected
    case disconnected
}

public enum CoreRetrievalState: String, Codable, Sendable {
    case retrievable
    case notRetrievable = "none"
}

public enum CoreRetentionState: String, Codable, Sendable {
    case retained
    case expired
    case deleted
}

public enum CoreCaptureDisposition: String, Codable, Sendable {
    case headEligible = "head_eligible"
    case historicalOnly = "historical_only"
}

public enum CoreDispositionReason: String, Codable, Sendable {
    case eligible
    case removedSource = "removed_source"
    case supersededGeneration = "superseded_generation"
    case disconnectedGeneration = "disconnected_generation"
    case creationConflict = "creation_conflict"
    case staleBase = "stale_base"
}

public enum CoreIntentKind: String, Codable, Sendable {
    case newCapture = "new_capture"
    case refresh
    case automaticCapture = "automatic_capture"
    case folderCapture = "folder_capture"
}

public enum CoreAssociationKind: String, Codable, Sendable {
    case snapshotOnly = "snapshot_only"
    case activeBinding = "active_binding"
    case historicalBinding = "historical_binding"
}

/// A capture's processing obligation. Never a retrieval claim: `settled` says Core no
/// longer owes work for this capture, not that anything became searchable.
public enum CoreProcessingState: String, Codable, Sendable {
    case pending
    case settled
    case notQueued = "not_queued"
}

public enum CoreResultState: String, Codable, Sendable {
    case succeeded
    case partial
    case unsupported
    case failed
}

public enum CorePublicationDisposition: String, Codable, Sendable {
    case promoted
    case superseded
    case historicalOnly = "historical_only"
    case fencedRemoval = "fenced_removal"
    case fencedRetention = "fenced_retention"
    case notPublished = "not_published"
}

public enum CoreProcessingMode: String, Codable, Sendable {
    case textOnly = "text_only"
    case textAndLocalOcr = "text_and_local_ocr"
}

public enum CoreHistoryActionName: String, Codable, Sendable {
    case reprocessCapturedCopy = "reprocess_captured_copy"
    case refreshOriginal = "refresh_original"
}

public enum CoreHistoryUnavailableReason: String, Codable, Sendable {
    case sourceRemoved = "source_removed"
    case sourceNotRetained = "source_not_retained"
    case formatNotQualified = "format_not_qualified"
    case noOriginalBinding = "no_original_binding"
    case originalDisconnected = "original_disconnected"
}

// MARK: - Closed objects

private struct CoreHistoryAnyKey: CodingKey {
    let stringValue: String
    let intValue: Int? = nil

    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

/// Refuses any field this build does not define. `Decodable` would otherwise ignore a key
/// it has no property for, so a newer document would read as a silent subset.
private func rejectUnknownHistoryKeys<Keys: CodingKey & CaseIterable>(
    _ decoder: Decoder,
    _ keys: Keys.Type
) throws {
    let allowed = Set(Keys.allCases.map(\.stringValue))
    let raw = try decoder.container(keyedBy: CoreHistoryAnyKey.self)
    guard Set(raw.allKeys.map(\.stringValue)).isSubset(of: allowed) else {
        throw CoreHistoryError.unreadable
    }
}

/// One action Core states the availability of, with the exact reason when it is not
/// available. A control is offered only for an available action; otherwise the window
/// shows the reason, never a button that cannot work.
public struct CoreHistoryAction: Decodable, Equatable, Sendable {
    public let action: CoreHistoryActionName
    public let available: Bool
    public let unavailableReason: CoreHistoryUnavailableReason?

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case action
        case available
        case unavailableReason
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownHistoryKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        action = try container.decode(CoreHistoryActionName.self, forKey: .action)
        available = try container.decode(Bool.self, forKey: .available)
        unavailableReason = try container.decodeIfPresent(
            CoreHistoryUnavailableReason.self, forKey: .unavailableReason
        )
        guard available == (unavailableReason == nil) else { throw CoreHistoryError.unreadable }
    }
}

/// One profile selection Core reports as approved for reprocessing one exact capture.
/// A caller sends exactly these two strings and never composes a selection of its own.
public struct CoreReprocessProfile: Decodable, Equatable, Sendable {
    public let profileRef: String
    public let revision: String
    public let processingMode: CoreProcessingMode
    public let profileDigest: String

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case profileRef
        case revision
        case processingMode
        case profileDigest
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownHistoryKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        profileRef = try container.decode(String.self, forKey: .profileRef)
        revision = try container.decode(String.self, forKey: .revision)
        processingMode = try container.decode(CoreProcessingMode.self, forKey: .processingMode)
        profileDigest = try container.decode(String.self, forKey: .profileDigest)
        guard isCoreIdentifier(profileRef), isCoreIdentifier(revision),
              isCoreDigest(profileDigest)
        else { throw CoreHistoryError.unreadable }
    }

    /// What the processing choice means, in the product's own words.
    public var title: String {
        switch processingMode {
        case .textOnly: "Text only"
        case .textAndLocalOcr: "Text and local OCR"
        }
    }
}

/// The immutable profile one extraction revision ran under. The profile body is not
/// exposed by Core and is not reconstructed here.
public struct CoreProfileSummary: Decodable, Equatable, Sendable {
    public let digest: String
    public let decoderBuild: String
    public let processingMode: CoreProcessingMode
    public let embeddedImageOcr: Bool
    public let ocrLanguages: [String]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case digest
        case decoderBuild
        case processingMode
        case embeddedImageOcr
        case ocrLanguages
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownHistoryKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        digest = try container.decode(String.self, forKey: .digest)
        decoderBuild = try container.decode(String.self, forKey: .decoderBuild)
        processingMode = try container.decode(CoreProcessingMode.self, forKey: .processingMode)
        embeddedImageOcr = try container.decode(Bool.self, forKey: .embeddedImageOcr)
        ocrLanguages = try container.decode([String].self, forKey: .ocrLanguages)
        // Three is the normative maximum a recognition-language selection may hold, so a
        // summary naming a fourth describes a selection Core would itself have refused.
        guard isCoreDigest(digest), (1...512).contains(decoderBuild.unicodeScalars.count),
              ocrLanguages.count <= 3
        else { throw CoreHistoryError.unreadable }
    }
}

/// The profile a capture *requested* when it was admitted. Its extractions are reported
/// separately and may name a different profile.
public struct CoreRequestedProfile: Decodable, Equatable, Sendable {
    public let profileRef: String
    public let revision: String

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case profileRef
        case revision
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownHistoryKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        profileRef = try container.decode(String.self, forKey: .profileRef)
        revision = try container.decode(String.self, forKey: .revision)
        guard isCoreIdentifier(profileRef), isCoreIdentifier(revision) else {
            throw CoreHistoryError.unreadable
        }
    }
}

/// One immutable source capture of this document.
///
/// `isCurrentSource` is Core's statement, read from the document's own source head. It is
/// never inferred from `captureSequence`, from the order of this list or from a timestamp.
public struct CoreSourceCapture: Decodable, Equatable, Sendable {
    public let captureID: String
    public let captureSequence: UInt64
    public let evidenceID: String
    public let sourceDigest: String
    public let sourceLengthBytes: UInt64
    public let mediaType: String
    public let requestedSourceIdentity: String
    public let intentKind: CoreIntentKind
    public let associationKind: CoreAssociationKind
    public let captureDisposition: CoreCaptureDisposition
    public let dispositionReason: CoreDispositionReason
    public let isCurrentSource: Bool
    public let retentionState: CoreRetentionState
    public let captureRetentionGeneration: UInt64
    public let sourceRemovalGeneration: UInt64
    public let requestedProfile: CoreRequestedProfile
    public let processingState: CoreProcessingState
    public let expectedBaseCaptureID: String?
    public let capturedAt: String
    public let retentionChangedAt: String?
    public let actions: [CoreHistoryAction]
    public let reprocessProfiles: [CoreReprocessProfile]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case captureID = "captureId"
        case captureSequence
        case evidenceID = "evidenceId"
        case sourceDigest
        case sourceLengthBytes
        case mediaType
        case requestedSourceIdentity
        case intentKind
        case associationKind
        case captureDisposition
        case dispositionReason
        case isCurrentSource
        case retentionState
        case captureRetentionGeneration
        case sourceRemovalGeneration
        case requestedProfile
        case processingState
        case expectedBaseCaptureID = "expectedBaseCaptureId"
        case capturedAt
        case retentionChangedAt
        case actions
        case reprocessProfiles
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownHistoryKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        captureID = try container.decode(String.self, forKey: .captureID)
        captureSequence = try container.decode(UInt64.self, forKey: .captureSequence)
        evidenceID = try container.decode(String.self, forKey: .evidenceID)
        sourceDigest = try container.decode(String.self, forKey: .sourceDigest)
        sourceLengthBytes = try container.decode(UInt64.self, forKey: .sourceLengthBytes)
        mediaType = try container.decode(String.self, forKey: .mediaType)
        requestedSourceIdentity = try container.decode(
            String.self, forKey: .requestedSourceIdentity
        )
        intentKind = try container.decode(CoreIntentKind.self, forKey: .intentKind)
        associationKind = try container.decode(CoreAssociationKind.self, forKey: .associationKind)
        captureDisposition = try container.decode(
            CoreCaptureDisposition.self, forKey: .captureDisposition
        )
        dispositionReason = try container.decode(
            CoreDispositionReason.self, forKey: .dispositionReason
        )
        isCurrentSource = try container.decode(Bool.self, forKey: .isCurrentSource)
        retentionState = try container.decode(CoreRetentionState.self, forKey: .retentionState)
        captureRetentionGeneration = try container.decode(
            UInt64.self, forKey: .captureRetentionGeneration
        )
        sourceRemovalGeneration = try container.decode(
            UInt64.self, forKey: .sourceRemovalGeneration
        )
        requestedProfile = try container.decode(CoreRequestedProfile.self, forKey: .requestedProfile)
        processingState = try container.decode(CoreProcessingState.self, forKey: .processingState)
        expectedBaseCaptureID = try container.decodeIfPresent(
            String.self, forKey: .expectedBaseCaptureID
        )
        capturedAt = try container.decode(String.self, forKey: .capturedAt)
        retentionChangedAt = try container.decodeIfPresent(String.self, forKey: .retentionChangedAt)
        actions = try container.decode([CoreHistoryAction].self, forKey: .actions)
        reprocessProfiles = try container.decode(
            [CoreReprocessProfile].self, forKey: .reprocessProfiles
        )
        guard isCoreIdentifier(captureID), isCoreIdentifier(evidenceID),
              isCoreDigest(sourceDigest), captureSequence >= 1,
              isCoreIdentifier(requestedSourceIdentity),
              expectedBaseCaptureID.map(isCoreIdentifier) ?? true,
              actions.count <= 8, reprocessProfiles.count <= 8
        else { throw CoreHistoryError.unreadable }
        // An offered profile for an action Core reports unavailable would be a control
        // that cannot work; an available action with no profile would be a control with
        // nothing to send.
        guard reprocessProfiles.isEmpty != reprocessAvailable else {
            throw CoreHistoryError.unreadable
        }
        // Historical-only is never current, whatever the order of the list says.
        guard !isCurrentSource || captureDisposition == .headEligible else {
            throw CoreHistoryError.unreadable
        }
    }

    /// Whether Core reports this exact capture as reprocessable now.
    public var reprocessAvailable: Bool {
        actions.contains { $0.action == .reprocessCapturedCopy && $0.available }
    }

    /// Why not, when it is not. `nil` when Core reports the action available, or reports
    /// no such action at all -- which is itself not a claim that it would be accepted.
    public var reprocessUnavailableReason: CoreHistoryUnavailableReason? {
        actions.first { $0.action == .reprocessCapturedCopy }?.unavailableReason
    }
}

/// One immutable extraction revision over one of this document's captures.
///
/// `isCurrentExtraction` is Core's statement, read from the document's own extraction
/// head, which is independent of the source head. A revision over an older capture cannot
/// make that capture current, and a revision that completed last is not current merely
/// for having completed last.
public struct CoreExtractionRevision: Decodable, Equatable, Sendable {
    public let extractionRevisionID: String
    public let captureID: String
    public let documentOrdinal: UInt64
    public let extractionSequence: UInt64
    public let profile: CoreProfileSummary
    public let expectedBaseExtractionID: String?
    public let headEligible: Bool
    public let isCurrentExtraction: Bool
    public let resultState: CoreResultState?
    public let publicationDisposition: CorePublicationDisposition?
    public let outputDigest: String?
    public let workerBuild: String?
    public let admittedAt: String
    public let completedAt: String?

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case extractionRevisionID = "extractionRevisionId"
        case captureID = "captureId"
        case documentOrdinal
        case extractionSequence
        case profile
        case expectedBaseExtractionID = "expectedBaseExtractionId"
        case headEligible
        case isCurrentExtraction
        case resultState
        case publicationDisposition
        case outputDigest
        case workerBuild
        case admittedAt
        case completedAt
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownHistoryKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        extractionRevisionID = try container.decode(String.self, forKey: .extractionRevisionID)
        captureID = try container.decode(String.self, forKey: .captureID)
        documentOrdinal = try container.decode(UInt64.self, forKey: .documentOrdinal)
        extractionSequence = try container.decode(UInt64.self, forKey: .extractionSequence)
        profile = try container.decode(CoreProfileSummary.self, forKey: .profile)
        expectedBaseExtractionID = try container.decodeIfPresent(
            String.self, forKey: .expectedBaseExtractionID
        )
        headEligible = try container.decode(Bool.self, forKey: .headEligible)
        isCurrentExtraction = try container.decode(Bool.self, forKey: .isCurrentExtraction)
        resultState = try container.decodeIfPresent(CoreResultState.self, forKey: .resultState)
        publicationDisposition = try container.decodeIfPresent(
            CorePublicationDisposition.self, forKey: .publicationDisposition
        )
        outputDigest = try container.decodeIfPresent(String.self, forKey: .outputDigest)
        workerBuild = try container.decodeIfPresent(String.self, forKey: .workerBuild)
        admittedAt = try container.decode(String.self, forKey: .admittedAt)
        completedAt = try container.decodeIfPresent(String.self, forKey: .completedAt)
        guard isCoreIdentifier(extractionRevisionID), isCoreIdentifier(captureID),
              documentOrdinal >= 1, extractionSequence >= 1,
              expectedBaseExtractionID.map(isCoreIdentifier) ?? true,
              outputDigest.map(isCoreDigest) ?? true
        else { throw CoreHistoryError.unreadable }
        // A terminal result states both its outcome and what that outcome did to
        // retrieval; a pending one states neither, and claims nothing else either.
        guard (resultState == nil) == (publicationDisposition == nil) else {
            throw CoreHistoryError.unreadable
        }
        if resultState == nil {
            guard outputDigest == nil, workerBuild == nil, completedAt == nil,
                  !isCurrentExtraction
            else { throw CoreHistoryError.unreadable }
        }
        // Only the promoted revision is the head. Anything else reading as current is
        // exactly the lie this decoder exists to refuse.
        guard !isCurrentExtraction || publicationDisposition == .promoted else {
            throw CoreHistoryError.unreadable
        }
    }

    /// Whether Core currently serves this revision's output. A revision can have
    /// succeeded and still not be the one retrieval holds.
    public var isPending: Bool { resultState == nil }
}

/// The document's source side. Independent of the extraction head.
public struct CoreSourceHead: Decodable, Equatable, Sendable {
    public let sourceState: CoreSourceState
    public let sourceRemovalGeneration: UInt64
    public let currentCaptureID: String?
    public let captureHeadSequence: UInt64
    public let originalBindingState: CoreBindingState
    public let updatedAt: String

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case sourceState
        case sourceRemovalGeneration
        case currentCaptureID = "currentCaptureId"
        case captureHeadSequence
        case originalBindingState
        case updatedAt
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownHistoryKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        sourceState = try container.decode(CoreSourceState.self, forKey: .sourceState)
        sourceRemovalGeneration = try container.decode(
            UInt64.self, forKey: .sourceRemovalGeneration
        )
        currentCaptureID = try container.decodeIfPresent(String.self, forKey: .currentCaptureID)
        captureHeadSequence = try container.decode(UInt64.self, forKey: .captureHeadSequence)
        originalBindingState = try container.decode(
            CoreBindingState.self, forKey: .originalBindingState
        )
        updatedAt = try container.decode(String.self, forKey: .updatedAt)
        guard (currentCaptureID == nil) == (captureHeadSequence == 0),
              currentCaptureID.map(isCoreIdentifier) ?? true
        else { throw CoreHistoryError.unreadable }
    }
}

/// The document's extraction side. Independent of the source head.
public struct CoreExtractionHead: Decodable, Equatable, Sendable {
    public let currentExtractionID: String?
    public let extractionHeadOrdinal: UInt64
    public let retrievalState: CoreRetrievalState
    public let publishedAt: String?

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case currentExtractionID = "currentExtractionId"
        case extractionHeadOrdinal
        case retrievalState
        case publishedAt
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownHistoryKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        currentExtractionID = try container.decodeIfPresent(
            String.self, forKey: .currentExtractionID
        )
        extractionHeadOrdinal = try container.decode(UInt64.self, forKey: .extractionHeadOrdinal)
        retrievalState = try container.decode(CoreRetrievalState.self, forKey: .retrievalState)
        publishedAt = try container.decodeIfPresent(String.self, forKey: .publishedAt)
        guard (currentExtractionID == nil) == (extractionHeadOrdinal == 0),
              currentExtractionID.map(isCoreIdentifier) ?? true,
              (retrievalState == .retrievable) == (publishedAt != nil)
        else { throw CoreHistoryError.unreadable }
    }
}

/// One page of the two histories, exactly as Core answered it.
public struct CoreDocumentHistoryPage: Decodable, Equatable, Sendable {
    public let documentID: String
    public let createdAt: String
    public let source: CoreSourceHead
    public let extraction: CoreExtractionHead
    public let captures: [CoreSourceCapture]
    public let captureCount: UInt64
    public let extractions: [CoreExtractionRevision]
    public let extractionCount: UInt64
    public let actions: [CoreHistoryAction]
    public let continuationToken: String?

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case documentID = "documentId"
        case createdAt
        case source
        case extraction
        case captures
        case captureCount
        case extractions
        case extractionCount
        case actions
        case page
    }

    private struct Page: Decodable {
        let continuationToken: String?

        private enum CodingKeys: String, CodingKey, CaseIterable {
            case continuationToken
        }

        init(from decoder: Decoder) throws {
            try rejectUnknownHistoryKeys(decoder, CodingKeys.self)
            let container = try decoder.container(keyedBy: CodingKeys.self)
            continuationToken = try container.decodeIfPresent(
                String.self, forKey: .continuationToken
            )
        }
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownHistoryKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        documentID = try container.decode(String.self, forKey: .documentID)
        createdAt = try container.decode(String.self, forKey: .createdAt)
        source = try container.decode(CoreSourceHead.self, forKey: .source)
        extraction = try container.decode(CoreExtractionHead.self, forKey: .extraction)
        captures = try container.decode([CoreSourceCapture].self, forKey: .captures)
        captureCount = try container.decode(UInt64.self, forKey: .captureCount)
        extractions = try container.decode([CoreExtractionRevision].self, forKey: .extractions)
        extractionCount = try container.decode(UInt64.self, forKey: .extractionCount)
        actions = try container.decode([CoreHistoryAction].self, forKey: .actions)
        continuationToken = try container.decode(Page.self, forKey: .page).continuationToken
        guard isCoreIdentifier(documentID), actions.count <= 8,
              UInt64(captures.count) <= captureCount,
              UInt64(extractions.count) <= extractionCount,
              captures.count <= CoreDocumentHistoryReader.pageLimit,
              extractions.count <= CoreDocumentHistoryReader.pageLimit
        else { throw CoreHistoryError.unreadable }
        // Whichever member of this page the heads name must say so, and no other may.
        for capture in captures where capture.isCurrentSource != (capture.captureID == source.currentCaptureID) {
            throw CoreHistoryError.unreadable
        }
        for revision in extractions
        where revision.isCurrentExtraction != (revision.extractionRevisionID == extraction.currentExtractionID) {
            throw CoreHistoryError.unreadable
        }
    }
}

/// One document's complete history as this companion read it.
///
/// `complete` is false when the read stopped at its own page bound rather than at Core's
/// last page. A bounded view is said to be bounded; it is not presented as the whole.
public struct CoreDocumentHistory: Equatable, Sendable {
    public let documentID: String
    public let createdAt: String
    public let source: CoreSourceHead
    public let extraction: CoreExtractionHead
    public let captures: [CoreSourceCapture]
    public let captureCount: UInt64
    public let extractions: [CoreExtractionRevision]
    public let extractionCount: UInt64
    public let actions: [CoreHistoryAction]
    public let complete: Bool

    public init(
        documentID: String,
        createdAt: String,
        source: CoreSourceHead,
        extraction: CoreExtractionHead,
        captures: [CoreSourceCapture],
        captureCount: UInt64,
        extractions: [CoreExtractionRevision],
        extractionCount: UInt64,
        actions: [CoreHistoryAction],
        complete: Bool
    ) {
        self.documentID = documentID
        self.createdAt = createdAt
        self.source = source
        self.extraction = extraction
        self.captures = captures
        self.captureCount = captureCount
        self.extractions = extractions
        self.extractionCount = extractionCount
        self.actions = actions
        self.complete = complete
    }

    /// The retained captures a reprocess may name, newest first: the choice the user is
    /// offered is exactly the set Core reports as available.
    public var reprocessableCaptures: [CoreSourceCapture] {
        captures.filter(\.reprocessAvailable).sorted { $0.captureSequence > $1.captureSequence }
    }

    /// Why no original can be read again, when none can.
    public var refreshUnavailableReason: CoreHistoryUnavailableReason? {
        actions.first { $0.action == .refreshOriginal }?.unavailableReason
    }
}

/// One reprocess receipt, decoded strictly.
public struct CoreReprocessReceipt: Decodable, Equatable, Sendable {
    public let extractionRevisionID: String
    public let documentID: String
    public let captureID: String
    public let profileDigest: String
    public let resultState: CoreResultState
    public let publicationDisposition: CorePublicationDisposition
    public let becameHead: Bool
    public let outputDigest: String?

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case extractionRevisionID = "extractionRevisionId"
        case documentID = "documentId"
        case captureID = "captureId"
        case profileDigest
        case resultState
        case publicationDisposition
        case becameHead
        case outputDigest
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownHistoryKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        extractionRevisionID = try container.decode(String.self, forKey: .extractionRevisionID)
        documentID = try container.decode(String.self, forKey: .documentID)
        captureID = try container.decode(String.self, forKey: .captureID)
        profileDigest = try container.decode(String.self, forKey: .profileDigest)
        resultState = try container.decode(CoreResultState.self, forKey: .resultState)
        publicationDisposition = try container.decode(
            CorePublicationDisposition.self, forKey: .publicationDisposition
        )
        becameHead = try container.decode(Bool.self, forKey: .becameHead)
        outputDigest = try container.decodeIfPresent(String.self, forKey: .outputDigest)
        guard isCoreIdentifier(extractionRevisionID), isCoreIdentifier(documentID),
              isCoreIdentifier(captureID), isCoreDigest(profileDigest),
              outputDigest.map(isCoreDigest) ?? true,
              becameHead == (publicationDisposition == .promoted)
        else { throw CoreHistoryError.unreadable }
    }

    /// What this receipt actually proves, in copy that never promotes a historical-only
    /// result to a success. A reverse-order completion and a stale base both land here.
    public var outcomeSentence: String {
        switch publicationDisposition {
        case .promoted:
            return "Reprocessed. This is now the current searchable version."
        case .superseded:
            return "Reprocessed, kept as history only. A newer extraction is current."
        case .historicalOnly:
            return "Reprocessed, kept as history only. The version it was based on is no "
                + "longer current, so it did not replace the current one."
        case .fencedRemoval:
            return "Reprocessed, kept as history only. This source was removed before the "
                + "result could be published."
        case .fencedRetention:
            return "Reprocessed, kept as history only. The captured copy stopped being "
                + "retained before the result could be published."
        case .notPublished:
            return resultState == .unsupported
                ? "This captured copy is not supported by the selected processing. Nothing "
                    + "was replaced."
                : "Processing failed. The previous searchable version is unchanged."
        }
    }
}

// MARK: - Scalar domains

/// `Identifier`: 1…128 characters, ASCII alphanumeric first, then alphanumerics and
/// `. _ : -`. Mirrored here because this side decodes the contract without generating it.
public func isCoreIdentifier(_ value: String) -> Bool {
    guard (1...128).contains(value.unicodeScalars.count) else { return false }
    let bytes = Array(value.utf8)
    guard let first = bytes.first, isCoreAlphanumeric(first) else { return false }
    return bytes.allSatisfy { byte in
        isCoreAlphanumeric(byte) || byte == UInt8(ascii: ".") || byte == UInt8(ascii: "_")
            || byte == UInt8(ascii: ":") || byte == UInt8(ascii: "-")
    }
}

/// `EvidenceChecksum`: `algorithm:digest`, bounded, with a non-empty algorithm.
public func isCoreDigest(_ value: String) -> Bool {
    guard (3...256).contains(value.unicodeScalars.count) else { return false }
    guard let colon = value.firstIndex(of: ":"), colon != value.startIndex else { return false }
    let digest = value[value.index(after: colon)...]
    guard !digest.isEmpty else { return false }
    return value[..<colon].utf8.allSatisfy { isCoreAlphanumeric($0) || $0 == UInt8(ascii: "_") }
        && digest.utf8.allSatisfy { byte in
            isCoreAlphanumeric(byte) || byte == UInt8(ascii: "+") || byte == UInt8(ascii: "/")
                || byte == UInt8(ascii: "=") || byte == UInt8(ascii: "_")
                || byte == UInt8(ascii: "-")
        }
}

private func isCoreAlphanumeric(_ byte: UInt8) -> Bool {
    (UInt8(ascii: "A")...UInt8(ascii: "Z")).contains(byte)
        || (UInt8(ascii: "a")...UInt8(ascii: "z")).contains(byte)
        || (UInt8(ascii: "0")...UInt8(ascii: "9")).contains(byte)
}

// MARK: - The transport and the bounded reader

/// The two Core operations this surface calls, and nothing else. Both are the governed
/// application operations: no shell query, no database file and no second authority.
public protocol CoreDocumentHistoryTransport: Sendable {
    func history(
        documentID: String,
        limit: Int,
        continuationToken: String?
    ) -> Result<CoreDocumentHistoryPage, CoreHistoryError>

    func reprocess(
        captureID: String,
        profileRef: String,
        revision: String,
        expectedProfileDigest: String,
        expectedBaseExtractionID: String?,
        operationKey: String
    ) -> Result<CoreReprocessReceipt, CoreHistoryError>
}

/// Reads one document's whole history through bounded pages.
public struct CoreDocumentHistoryReader: Sendable {
    /// What one request asks for. Well under the operation's declared ceiling, because a
    /// detail pane is read by a person and a page is a round trip either way.
    public static let pageLimit = 50
    /// How many pages one read will follow. The product's own source and extraction bounds
    /// are far below this; a document beyond it is reported as a bounded view rather than
    /// walked until something gives way.
    public static let maximumPages = 20

    private let transport: CoreDocumentHistoryTransport

    public init(transport: CoreDocumentHistoryTransport) {
        self.transport = transport
    }

    /// Every page, or the first failure.
    ///
    /// A multi-page read is only one history if every page describes the same state of the
    /// same document, so that is checked rather than assumed. Core binds a continuation
    /// series to a server-computed snapshot and refuses a continuation whose document moved,
    /// which is where the guarantee comes from; these checks are this side's independent
    /// hold on the same property, because an aggregate assembled from two states is a lie
    /// this surface would be the one telling.
    ///
    /// Four things must hold across the pages. The document, its creation instant, both
    /// heads, both totals and the document-level actions are one statement, so every page
    /// must repeat the first page's -- a later page that disagrees is a second state, not
    /// more of the first. Identities and ordering numbers must be unique, or one member
    /// would be rendered twice, possibly with contradictory flags. The current flags must be
    /// globally consistent: at most one member may claim to be current, a complete read must
    /// actually contain the member its head names, and no member may claim it when the head
    /// names none. And a token must never be followed twice, so a server that returned a
    /// token it had already issued cannot be walked in a circle until the page bound stops
    /// it.
    public func read(documentID: String) -> Result<CoreDocumentHistory, CoreHistoryError> {
        var captures: [CoreSourceCapture] = []
        var extractions: [CoreExtractionRevision] = []
        var token: String?
        var seenTokens: Set<String> = []
        var pages = 0
        var first: CoreDocumentHistoryPage?
        while pages < Self.maximumPages {
            let outcome = transport.history(
                documentID: documentID, limit: Self.pageLimit, continuationToken: token
            )
            guard case let .success(page) = outcome else {
                if case let .failure(error) = outcome { return .failure(error) }
                return .failure(.unreadable)
            }
            guard page.documentID == documentID else { return .failure(.unreadable) }
            if let first {
                guard Self.describesTheSameState(first, page) else {
                    return .failure(.unreadable)
                }
            } else {
                first = page
            }
            captures += page.captures
            extractions += page.extractions
            pages += 1
            guard let next = page.continuationToken else { token = nil; break }
            // A token this read has already followed cannot be a position further on.
            guard seenTokens.insert(next).inserted else { return .failure(.unreadable) }
            token = next
        }
        guard let page = first else { return .failure(.unreadable) }
        let complete = token == nil
        guard Self.membersAreDistinct(captures: captures, extractions: extractions),
              Self.currentClaimsAgree(
                  page: page, captures: captures, extractions: extractions,
                  complete: complete
              )
        else { return .failure(.unreadable) }
        return .success(
            CoreDocumentHistory(
                documentID: page.documentID,
                createdAt: page.createdAt,
                source: page.source,
                extraction: page.extraction,
                captures: captures,
                captureCount: page.captureCount,
                extractions: extractions,
                extractionCount: page.extractionCount,
                actions: page.actions,
                complete: complete
            )
        )
    }

    /// Whether two pages are two pages of one history rather than two histories.
    private static func describesTheSameState(
        _ first: CoreDocumentHistoryPage,
        _ later: CoreDocumentHistoryPage
    ) -> Bool {
        first.createdAt == later.createdAt && first.source == later.source
            && first.extraction == later.extraction
            && first.captureCount == later.captureCount
            && first.extractionCount == later.extractionCount
            && first.actions == later.actions
    }

    /// Whether no member, and no ordering number, was served twice.
    private static func membersAreDistinct(
        captures: [CoreSourceCapture],
        extractions: [CoreExtractionRevision]
    ) -> Bool {
        Set(captures.map(\.captureID)).count == captures.count
            && Set(captures.map(\.captureSequence)).count == captures.count
            && Set(extractions.map(\.extractionRevisionID)).count == extractions.count
            && Set(extractions.map(\.documentOrdinal)).count == extractions.count
    }

    /// Whether the assembled pages' currentness claims are consistent with the heads.
    ///
    /// Each page's own decoder already held its members to the head it arrived with. What
    /// only the aggregate can see is a second member claiming currentness from another page,
    /// and a complete read that names a current member it never actually returned.
    private static func currentClaimsAgree(
        page: CoreDocumentHistoryPage,
        captures: [CoreSourceCapture],
        extractions: [CoreExtractionRevision],
        complete: Bool
    ) -> Bool {
        let currentCaptures = captures.filter(\.isCurrentSource)
        let currentExtractions = extractions.filter(\.isCurrentExtraction)
        guard currentCaptures.count <= 1, currentExtractions.count <= 1 else { return false }
        guard currentCaptures.first.map({ $0.captureID == page.source.currentCaptureID })
                ?? true,
              currentExtractions.first.map({
                  $0.extractionRevisionID == page.extraction.currentExtractionID
              }) ?? true
        else { return false }
        // A bounded read may legitimately not have reached the current member yet. A
        // complete one has read every member, so the head's own name must be among them.
        guard complete else { return true }
        if page.source.currentCaptureID != nil, currentCaptures.isEmpty { return false }
        if page.extraction.currentExtractionID != nil, currentExtractions.isEmpty {
            return false
        }
        return true
    }

    /// The operation key one reprocess intent gets.
    ///
    /// Derived from the exact request rather than from a fresh random value, and that is
    /// the point: a double-tap or a repeated attempt after a lost answer replays the same
    /// canonical request and returns the same receipt, instead of admitting a second
    /// extraction. A genuinely different intent -- another capture, another profile, the
    /// exact profile identity that profile resolved to, or the next base after this one
    /// succeeded -- is a different key by construction.
    ///
    /// The expected profile digest is one of those fields, not an afterthought: it is part
    /// of the request Core settles the key under, so a key derived without it would claim
    /// two requests Core treats as different are the same intent.
    public static func reprocessOperationKey(
        captureID: String,
        profileRef: String,
        revision: String,
        expectedProfileDigest: String,
        expectedBaseExtractionID: String?
    ) -> String {
        let fields = [
            captureID,
            profileRef,
            revision,
            expectedProfileDigest,
            expectedBaseExtractionID ?? "",
        ]
        let digest = SHA256.hash(data: Data(fields.joined(separator: "\u{0}").utf8))
        return "dhr-" + digest.map { String(format: "%02x", $0) }.joined()
    }
}
